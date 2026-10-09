# 项目接续指南

开始工作时先读 `README.md`、`docs/CLOUD_HANDOFF.md` 和 `reports/README.md`。报告是历史操作证据，其中旧方案、阶段性失败和当时的文件路径不能直接替代当前源码或用户最新要求。

## 结构与验证

- `app/`：FastAPI、数据库、采集、资料包和 Excel 导出。
- `templates/`、`static/`：Jinja2 和原生 HTML/CSS/JavaScript。
- `tests/`：隔离数据的回归测试；不需要真实淘宝或生产密钥。
- `scripts/`：连接检查、Windows 文件夹组件和界面验收。
- `reports/`：可公开的操作报告、变更记录和测试证据；`docs/superpowers/` 保存既有设计和计划。

安装依赖：`python -m pip install -r requirements-dev.txt`。

验证与改动相应的测试，必要时运行全量 `python -m pytest -q`。前端脚本验证：`node --check static/js/collection.js` 和 `node --check static/js/preparation.js`。Python 命令应使用当前环境的解释器；本机使用 `.venv/Scripts/python.exe`，Linux 虚拟环境使用 `.venv/bin/python`。

## 保留现有行为

商品采集和资料准备是当前范围。原图按原始响应字节保存；未知数据保持空白；人工修订、原始证据、旧资料包以及暂停/恢复检查点需要保留。每商品独立 Excel，多商品 ZIP；字段和固定填写规则以 README 和测试为准。

使用隔离临时数据库验证。Git 不含本机生产数据、密钥或浏览器状态，不能假定云端已有这些内容。真实采集、可见 Chrome、Windows 原生文件夹窗口需要相应环境验收；Linux 跳过的 Windows 测试应在结果中说明。

完成用户要求的改动后，在 `reports/` 更新相关操作记录，写明改动、运行命令、实际结果和未验证范围。不要把密钥、Cookie、生产数据库或运行时敏感输出加入记录。
