# 任务列表删除功能

日期：2026-09-29，项目：`D:\AI_Ecommerce`。

## 功能

- 每个顶层商品或店铺任务提供“删除任务”。
- 工具栏提供“删除本页任务”，仅作用于当前页显示的任务。
- 删除前显示确认，可取消。删除后更新任务数量；当前页删除后为空时返回有效分页。
- 使用持久化的列表删除标记，重启后任务仍不显示。停止该任务及其独占的未完成子任务，并释放相应人工浏览器占用；其它店铺或独立商品仍需要的共享任务继续。
- 保留商品资料、所有原始文件、数据库任务记录、采集快照和检查点。成功商品重新提交仍复用结果；未完成商品重新提交继续未完成部分。
- “已保存商品”仍可查看、下载与导出。不自动删除用户现有任务；由用户点击按钮选择清理。

## 验证

- 新增 15 个删除回归测试。最初 12 个均因缺失删除接口失败；独立代码复核又发现已删除店铺影响共享子商品重试/暂停的边界，新增 3 个用例先失败，修复后相关测试共 38 个通过。
- 完整测试：**101 passed**，78.10 秒，退出码 0。唯一警告是已有 Starlette/AnyIO 弃用提示。输出：`reports/task-delete-pytest.txt`。
- Chrome 界面验证：取消单个删除、确认单个删除、确认本页删除通过；列表为空、按钮禁用；4 个商品和 5 条历史记录保留，4 个原图端点响应字节一致；无页面脚本错误。输出：`reports/task-delete-ui-validation.txt`。
- 截图：`artifacts/collection-only-ui.png`（删除按钮）、`artifacts/collection-tasks-deleted-ui.png`（删除后列表）。
- `node --check static/js/collection.js` 退出码 0。
- 再次检查改造前记录的 329 个原始文件，SHA-256 均未改变。
- 独立复核指出的已删除父店铺重新入队、影响共享子任务暂停的两项问题，现统一通过只读取仍保留的父店铺判断；新增回归用例覆盖两店铺共享商品时的商品重试、店铺重试后暂停及运行中检查。
- 独立复审确认上述两项 Important 已修复，限定范围内未发现其它 Critical/Important。
- 测试均使用临时数据库和模拟采集结果，不访问真实淘宝或使用生产 ScraperAPI 凭据。

## 涉及文件

后端：`app/main.py`、`app/collection_service.py`。前端：`templates/collection.html`、`static/js/collection.js`、`static/css/collection.css`。测试与验收：`tests/test_collection_delete.py`、`scripts/collection_ui_acceptance.py`。使用说明：`README.md`。

## 运行

在当前服务窗口按 Ctrl+C 停止旧进程，再在 CMD 中执行：

```bat
cd /d D:\AI_Ecommerce
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

刷新网页即可看到删除按钮。已发出的网络请求仍需完成或超时，随后在任务检查点停止；共享工作属于其它未删除请求时继续。

## Web 技术结构

后端为 Python/FastAPI，页面为 Jinja2 模板、HTML/CSS 与原生 JavaScript，数据库为 SQLite/SQLAlchemy，采集使用 Playwright 与 ScraperAPI，服务由 Uvicorn 运行。
