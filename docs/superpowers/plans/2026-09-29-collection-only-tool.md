# 采集下载工具收敛实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 将现有应用收敛为批量采集商品资料、下载原始图片、追踪任务并导出的工具，同时保留已有数据与可恢复任务。

**Architecture:** 沿用 FastAPI、SQLite、任务 Worker、淘宝 parser、商品/原图存储和导出。统一采集器按页面与错误类型协调 ScraperAPI 和持久化 Playwright；所有浏览器自动操作由可暂停的 0.5–1.2 秒随机节奏器执行。页面和接口收敛到采集工作台，历史数据库 schema 保留。

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy, Playwright sync API, httpx, BeautifulSoup, Jinja2, vanilla JS, pytest.

**Spec:** `docs/superpowers/specs/2026-09-29-collection-only-redesign-design.md`

## Global Constraints

- 每次 Playwright 自动页面操作前独立随机等待 0.5–1.2 秒，等待期间检查暂停/取消状态。
- 页面读取、元素定位、解析和本地文件操作不增加等待。
- ScraperAPI 密钥仅从服务器配置读取，不进入页面、响应、任务结果或日志。
- 登录/验证码及解析、本地存储错误不得导致重复网络回退；未解决的人机验证转为人工暂停。
- 商品和原始图片成功后跳过；原图字节不加工、不覆盖；保留已有商品、任务、图片和历史表数据。
- 仅保留批量采集下载工作流及其必要页面、接口、依赖和服务。
- 真实淘宝、登录和 ScraperAPI 访问能力不能由离线测试替代。

## Review Focus

- 暂停恰在浏览器操作等待期间发生：该操作必须被取消且之后可继续。（Task 1）
- ScraperAPI 返回公开商品页后不应启动或导航浏览器；动态/访问受限错误按类型只回退一次。（Task 2）
- 人工登录完成后再次继续必须重访原始目标；若仍受限，应暂停并显示原因，不得循环。（Task 2）
- 重试与应用重启后不得重复创建成功商品或覆盖已有原图字节。（Task 3）
- 应用路由及导航中不得残留已移除业务页面/API，旧数据库和采集历史仍可读取。（Task 4）

---

### Task 1: 封装可暂停的逐次浏览器操作节奏

**Files:**
- Modify: `app/taobao/session.py`
- Modify: `app/collection_service.py`
- Test: `tests/test_browser_actions.py`
- Test: `tests/test_collection_browser_regressions.py`

**Interfaces:**
- `TaobaoSession` 接收任务操作前置回调，并用同一内部操作方法包裹自动 `goto`、点击、滚动、翻页和刷新。
- `CollectionRunner` 提供按 task id 检查自身及父任务暂停/取消状态的操作前置回调。

- [x] **Step 1: 写失败测试** — 覆盖每个被包装的自动操作均单独调用随机采样（边界 3.0 和 5.0 秒），暂停检查在等待中取消下一操作，元素查询/读取不触发等待。
- [x] **Step 2: 运行测试确认失败** — `.\.venv\Scripts\python.exe -m pytest tests/test_browser_actions.py tests/test_collection_browser_regressions.py -q`；预期新断言因现有固定 `wait_for_timeout` 和无逐操作封装失败。
- [x] **Step 3: 实现最小封装** — 在 `app/taobao/session.py` 注入随机等待与中断检查回调；每个自动 UI 动作调用回调后才执行动作；在 `app/collection_service.py` 将现有商品前等待替换为可取消的统一 3–5 秒等待，等待结束仍检查父子暂停状态。
- [x] **Step 4: 运行定向测试确认通过** — 重跑 Step 2 命令；预期新增浏览器动作和既有暂停测试全部通过。

### Task 2: 统一 ScraperAPI/Playwright 采集和登录恢复

**Files:**
- Modify: `app/scraperapi_service.py`
- Modify: `app/taobao/collector.py`
- Modify: `app/taobao/session.py`
- Modify: `app/collection_service.py`
- Test: `tests/test_scraperapi_collection.py`
- Test: `tests/test_collection_browser_regressions.py`
- Test: `tests/test_shop_detail_collection.py`

**Interfaces:**
- 保留 `TaobaoCollector.collect_product(task_id, url)` 与 `discover_shop(task_id, url, on_progress=None)` 作为统一入口。
- `TaobaoSession.open(url, refresh=False)` 必须对指定目标执行访问、最终 URL/验证检查并返回可解析页面；人工继续复用持久化 profile。

- [x] **Step 1: 写失败测试** — 加入公开 ScraperAPI 成功时无 browser open、公开店铺有后续分页/交互需求时转浏览器、明确 403/429/网络访问失败只回退一次、登录/CAPTCHA/parser/磁盘错误不回退，以及继续时重访原 URL 并在未登录时回到 NEEDS_MANUAL 的用例。
- [x] **Step 2: 运行测试确认失败** — `.\.venv\Scripts\python.exe -m pytest tests/test_scraperapi_collection.py tests/test_collection_browser_regressions.py tests/test_shop_detail_collection.py -q`；预期登录恢复和浏览器操作节奏用例失败。
- [x] **Step 3: 实现统一策略** — 只对识别出的页面获取失败或需要浏览器交互的页面转 Playwright；成功公开 HTML 交由同一 parser 处理；禁止解析、人工验证及本地写入错误触发网络回退。页面仍为登录/验证时保留具体 `manual_reason` 并暂停。
- [x] **Step 4: 运行定向测试确认通过** — 重跑 Step 2 命令；预期 ScraperAPI/Playwright 页面解析、登录暂停和恢复回归通过。

### Task 3: 支持多链接提交并巩固续跑、原图下载和导出

**Files:**
- Modify: `app/collection_service.py`
- Modify: `app/collection_export.py`
- Modify: `app/main.py`
- Modify: `templates/collection.html`
- Modify: `static/js/collection.js`
- Modify: `tests/test_url_collection.py`
- Test: `tests/test_shop_detail_collection.py`

**Interfaces:**
- `POST /api/collection/jobs/batch` 接收多个商品或店铺链接并返回新建/复用任务标识。
- 商品与店铺现有任务状态、暂停、继续、重试失败、单商品原图 ZIP/JSON/CSV 导出响应保持可用。

- [x] **Step 1: 写失败测试** — 覆盖多行链接逐条校验并入队、重复商品 URL 复用已成功任务、店铺恢复复用既有子任务、失败商品仅补缺图、下载字节与原图完全一致、JSON/CSV/ZIP 导出包含采集资料和原始图片。
- [x] **Step 2: 运行测试确认失败** — `.\.venv\Scripts\python.exe -m pytest tests/test_url_collection.py tests/test_shop_detail_collection.py -q`；预期多链接批量请求和成功任务复用测试失败。
- [x] **Step 3: 实现最小提交与恢复逻辑** — 更新采集表单使用多行 URL；沿用商品 canonical ID、CollectionJob、Task 和 SHA256/有效文件检查；只对缺失原图执行下载，对每个商品或店铺链接保存可独立查看与重试的任务。
- [x] **Step 4: 运行定向测试确认通过** — 重跑 Step 2 命令；预期多链接、任务恢复、字节保留和导出均通过。

### Task 4: 收敛应用入口并移除非采集业务

**Files:**
- Modify: `app/main.py`
- Modify: `app/products.py`
- Modify: `templates/base.html`
- Modify: `templates/collection.html`
- Remove: `templates/{index,tasks,products,product_detail,media,ai_workspace,reviews,trash,components}.html` 中仅属非采集工作流的页面；保留采集使用的组件或抽取必要片段。
- Remove: `app/ai_*.py`, `app/media_*.py`, `app/review_gate3.py`, `app/publish_*.py`, `app/trash_service.py`, `app/selected_region*.py` 及仅服务这些功能的前端/脚本文件。
- Test: `tests/test_url_collection.py`

**Interfaces:**
- `/` 与 `/collection` 呈现同一个采集工作台；其余页面路由不存在。
- 只保留 `/health`、采集任务、商品/图片下载和采集资料导出所需 API。
- 保留现有 ORM 历史模型与数据库表迁移，移除只在非采集业务使用的启动迁移及 Worker handler。

- [x] **Step 1: 写失败测试** — 增加首页采集工作台和路由清单测试，验证非采集页面及 `/api/ai`、`/api/media`、`/api/reviews`、`/api/publish`、回收站路由不可访问，同时旧 SQLite 中商品、Original、CollectionJob 与 Task 可读取。
- [x] **Step 2: 运行测试确认失败** — `.\.venv\Scripts\python.exe -m pytest tests/test_url_collection.py -q`；预期旧首页和其它业务路由断言失败。
- [x] **Step 3: 收敛后端、导航和模板** — 删除非采集启动依赖/路由/handler/导航及专属代码；保留历史 schema；清理服务端 ScraperAPI 状态响应，不向 UI 返回密钥；首页直接进入采集工作台。
- [x] **Step 4: 运行采集路由测试** — 重跑 Step 2 命令；预期采集接口与旧数据库兼容测试通过。

### Task 5: 清理依赖与文档并完成回归

**Files:**
- Modify: `README.md`
- Modify: `requirements.txt`
- Modify: `requirements-dev.txt`
- Modify: `package.json`, `package-lock.json`（如无保留前端用途）
- Remove: 非采集验收脚本、阶段文档、静态文件和已删除功能测试
- Test: `tests/test_browser_actions.py`
- Test: `tests/test_collection_browser_regressions.py`
- Test: `tests/test_scraperapi_collection.py`
- Test: `tests/test_url_collection.py`
- Test: `tests/test_shop_detail_collection.py`

**Interfaces:**
- README 只说明 Windows 本地启动、数据目录、ScraperAPI 服务器端设置、持久化登录、任务恢复、原图目录和下载/导出。
- 依赖清单只保留应用与定向测试实际需要的库。

- [x] **Step 1: 审核依赖引用并清理文档** — 基于仓库保留文件和测试的 imports 删除仅属非采集功能的 Python/Node 依赖，替换 README 启动及配置说明，清理过时验收命令。
- [x] **Step 2: 执行采集定向测试** — `.\.venv\Scripts\python.exe -m pytest tests/test_browser_actions.py tests/test_collection_browser_regressions.py tests/test_scraperapi_collection.py tests/test_url_collection.py tests/test_shop_detail_collection.py -q`；预期所有采集定向用例通过。
- [x] **Step 3: 执行前端与项目检查** — 运行 `node --check static/js/collection.js`、`python -m scripts.collection_ui_acceptance`；再运行完整 pytest suite，并记录所有失败及原因。
- [x] **Step 4: 核验清理边界** — 检查最终导航、OpenAPI 路由列表和依赖导入，确认旧数据库可打开、ScraperAPI 密钥不出现在界面/日志、Original 字节不变；真实淘宝登录环境与有效 ScraperAPI key 仍需部署环境现场验收。

实施说明：本项目无 Git 元数据，改造前源码已保存 ZIP。实际使用同步 Worker 加可中断轮询；历史 SQLite 表保留，退役业务 ORM 与服务移除。原图涵盖商品主图、详情图与 SKU 图片。

最终独立审查发现的问题已补充回归测试并修复，包括动态详情完整性、按任务恢复访问方式、人工浏览器占用、重采资料更新、分页检查点及子商品访问策略。完整测试最终 86 passed，真实 Chrome 隔离界面验收通过，329 个旧原始文件 SHA-256 不变，19 个历史表在兼容副本打开后行数不变。生成的 node_modules 缓存删除被自动审批策略阻止，应用不再引用；纯客户端分页在进程退出后需重建必要状态。详情见 `reports/collection-only-validation.md`。
