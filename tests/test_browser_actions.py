import pytest

from app.tasks import TaskDeferred


def test_every_action_draws_an_independent_delay_and_reads_do_not_wait(monkeypatch):
    from app.taobao.actions import BrowserActions
    sampled, waited, executed, stages = [], [], [], []
    values = iter([0.6, 1.1, 0.8])
    def sample(low, high):
        sampled.append((low, high))
        return next(values)
    monkeypatch.setattr('app.taobao.actions.uniform', sample)
    monkeypatch.setattr('app.taobao.actions.sleep', waited.append)
    actions = BrowserActions(stage=stages.append)
    for name in ('打开页面', '点击', '滚动'):
        actions.perform(name, lambda: executed.append(sum(waited)))
    assert sampled == [(0.5, 1.2)] * 3
    assert executed == pytest.approx([0.6, 1.7, 2.5])
    assert max(waited) <= 0.2
    assert stages == [
        '等待 0.6 秒后打开页面', '正在打开页面',
        '等待 1.1 秒后点击', '正在点击',
        '等待 0.8 秒后滚动', '正在滚动',
    ]


def test_pause_during_wait_prevents_pending_browser_action(monkeypatch):
    from app.taobao.actions import BrowserActions
    paused, executed = [], []
    def check():
        if paused:
            raise TaskDeferred()
    monkeypatch.setattr('app.taobao.actions.uniform', lambda a, b: 1.2)
    monkeypatch.setattr('app.taobao.actions.sleep', lambda seconds: paused.append(seconds))
    with pytest.raises(TaskDeferred):
        BrowserActions(check=check).perform('下一页', lambda: executed.append(True))
    assert executed == []
    assert sum(paused) <= 0.2


def test_product_detail_actions_draw_independent_short_delays(monkeypatch):
    from app.taobao.actions import BrowserActions

    sampled, waited, executed = [], [], []
    values = iter([0.6, 1.1, 0.8])
    def sample(low, high):
        sampled.append((low, high))
        return next(values)
    monkeypatch.setattr('app.taobao.actions.uniform', sample)
    monkeypatch.setattr('app.taobao.actions.sleep', waited.append)
    actions = BrowserActions()
    for name in ('点击详情入口', '滚动页面', '移动鼠标至详情'):
        actions.perform_detail(name, lambda: executed.append(sum(waited)))
    assert sampled == [(0.5, 1.2)] * 3
    assert executed == pytest.approx([0.6, 1.7, 2.5])
    assert max(waited) <= 0.2


def test_product_detail_wait_still_pauses_before_action(monkeypatch):
    from app.taobao.actions import BrowserActions

    paused, executed = [], []
    def check():
        if paused:
            raise TaskDeferred()
    monkeypatch.setattr('app.taobao.actions.uniform', lambda low, high: 1.2)
    monkeypatch.setattr('app.taobao.actions.sleep', lambda seconds: paused.append(seconds))
    with pytest.raises(TaskDeferred):
        BrowserActions(check=check).perform_detail('滚动详情', lambda: executed.append(True))
    assert executed == []
    assert sum(paused) <= 0.2


def test_product_detail_scroll_uses_short_waits(tmp_path, monkeypatch):
    from app.taobao.actions import BrowserActions
    from app.taobao.session import TaobaoSession

    sampled, scrolled = [], []
    def sample(low, high):
        sampled.append((low, high))
        return low
    class Tabs:
        def count(self):
            return 0
    class Mouse:
        def wheel(self, x, y):
            scrolled.append((x, y))
    class Page:
        frames = []
        url = 'https://item.taobao.com/item.htm?id=123'
        mouse = Mouse()
        def get_by_text(self, pattern):
            return Tabs()
        def wait_for_timeout(self, ms):
            pass
        def content(self):
            return '<h1>商品</h1>'
    monkeypatch.setattr('app.taobao.actions.uniform', sample)
    monkeypatch.setattr('app.taobao.actions.sleep', lambda seconds: None)
    session = TaobaoSession(tmp_path)
    session.page = Page()
    session.actions = BrowserActions()
    monkeypatch.setattr(session, '_check_manual', lambda: None)
    session.load_product_details()
    assert len(scrolled) == 12
    assert sampled == [(0.5, 1.2)] * 12


def test_manual_resume_revisits_original_target_with_same_page(tmp_path, monkeypatch):
    from app.taobao.session import TaobaoSession
    target = 'https://item.taobao.com/item.htm?id=123'
    class Page:
        url = 'https://login.taobao.com/member/login.jhtml'
        def goto(self, url, **kwargs):
            self.url = url
            return None
        def wait_for_timeout(self, ms):
            pass
        def content(self):
            return '<h1>已登录的商品</h1>'
    session = TaobaoSession(tmp_path)
    session.page = Page()
    session.manual_url, session.manual_pending = target, True
    monkeypatch.setattr(session, '_start', lambda: None)
    monkeypatch.setattr(session, '_check_manual', lambda: None)
    assert session.open(target)[1] == target


def test_real_random_wait_occurs_before_the_action(monkeypatch):
    import time
    from app.taobao.actions import BrowserActions
    monkeypatch.setattr('app.taobao.actions.uniform', lambda low, high: 0.5)
    monkeypatch.setattr('app.taobao.actions.sleep', time.sleep)
    started = time.monotonic()
    observed = BrowserActions().perform('打开页面', time.monotonic)
    assert 0.5 <= observed - started < 2.0


def test_redirect_to_another_product_is_not_collected_as_original_target(tmp_path, monkeypatch):
    from app.taobao.session import TaobaoSession, ManualRequired
    class Page:
        url = 'about:blank'
        def goto(self, url, **kwargs):
            self.url = 'https://item.taobao.com/item.htm?id=999'
        def wait_for_timeout(self, ms):
            pass
        def content(self):
            return '<h1>另一商品</h1>'
    session = TaobaoSession(tmp_path)
    session.page = Page()
    monkeypatch.setattr(session, '_start', lambda: None)
    monkeypatch.setattr(session, '_check_manual', lambda: None)
    with pytest.raises(ManualRequired, match='目标商品'):
        session.open('https://item.taobao.com/item.htm?id=123')
