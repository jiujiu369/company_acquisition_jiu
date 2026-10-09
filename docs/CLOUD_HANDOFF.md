# 云端工作接续说明

记录日期：2026-10-09（Asia/Taipei）。仓库：`jiujiu369/company_acquisition_jiu`，分支：`main`。

## 先读什么

1. `README.md`：当前功能、字段规则、运行与验证方式。
2. `reports/README.md`：历史操作记录索引。历史报告反映当时状态，最新行为以当前源码、测试和 README 为准。
3. `reports/github-upload-2026-10-09.md`：此次整理和上传记录。
4. `docs/superpowers/specs/`、`docs/superpowers/plans/`：既有设计和实施计划。

本仓库提供项目代码、模板、测试和可公开的操作记录。本机采集数据库、原始图片、导出文件、浏览器登录状态、密钥、截图和历史源码/数据库备份仍保存在本机，未随 Git 上传。报告中的 `D:\AI_Ecommerce` 和 `data/` 路径是历史本机路径，不表示云端已有这些文件。GitHub 不会自动携带本次聊天上下文；请让新任务先阅读本说明和记录索引。

## 当前项目状态

- Python/FastAPI、Jinja2、原生 JavaScript、SQLite/SQLAlchemy；采集使用 ScraperAPI 和 Playwright。
- 仅提供商品采集、资料准备和下载，不执行店铺上架或修改商品。
- 每个商品独立导出 Excel，多商品或整店导出 ZIP；Excel 按最终商品名称命名。
- 默认仅 Excel 模式允许取得一张明确主图，其余图片按完整原图模式处理；原图字节保持原样。
- SKU 编号使用明确型号文字；整包价格与包装数量分别保存；未知字段留空，不推测真实库存。现有固定填写规则见 README。
- 暂停、继续、重试、版本化资料包、人工修订保留和任务删除已有实现与回归测试。

## 云端安装和验证

本次 Windows 本机使用 Python 3.14。云端请安装与 `requirements.txt` 中固定版本兼容的 Python，并实际运行测试，不要为安装方便未经验证地改动依赖版本。

在仓库根目录执行以下 Bash 命令：

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
node --check static/js/collection.js
node --check static/js/preparation.js
```

测试使用临时数据库和模拟页面，不需要生产 ScraperAPI 凭据或真实淘宝登录。Linux 会跳过明确标注为 Windows 专用的原生窗口测试，因此通过数量可能与本机不同。JavaScript 语法检查需要 Node.js。

需要运行应用时：

```bash
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

默认 ScraperAPI 关闭。配置项见 `.env.example`；真实密钥须通过运行环境的安全配置提供，不能写入 Git、聊天记录或报告。只使用一个服务进程访问同一数据目录。

## 云端验收边界

Windows 原生文件夹选择器使用 PowerShell/C#，采集浏览器使用可见 Google Chrome 和持久化会话。普通 Linux 云端不能直接复用本机选择窗口或淘宝登录状态；相关端到端流程需要具备这些条件的环境验收。`scripts/collection_ui_acceptance.py` 的浏览器验收也依赖可用浏览器环境，不能把 Python 单元测试通过等同于真实采集验收。

云端可继续修改解析、资料包、Excel、API 和页面代码并运行隔离测试。后续有界面或采集行为改动时，保留必要的 Windows/真实页面验收，说明实际测试范围。

## 在 Codex Cloud 接续

在支持云端工作的界面选择 `Work in > Cloud`，创建或选择环境，连接 GitHub 并选择本仓库。让环境准备依赖并验证工作流，审阅后发布环境，再开始任务。具体入口与账户可用性以当前界面为准。

官方说明：[Codex Cloud](https://learn.chatgpt.com/docs/cloud)。

可给新任务这段提示词，再追加本次想做的改动：

> 请先阅读 README.md、docs/CLOUD_HANDOFF.md、reports/README.md 和 reports/github-upload-2026-10-09.md，理解当前代码与历史操作记录，再继续我的任务。保留现有商品资料、原图字节、人工修订和暂停恢复规则。测试使用隔离数据，不能假定云端已有本机数据库、图片、密钥或浏览器登录状态。完成后更新相关操作记录，说明实际验证结果。
