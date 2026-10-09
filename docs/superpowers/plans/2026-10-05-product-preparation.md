# 商品资料准备 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在现有采集项目中自动生成可检查、可移动的完整商品资料包，支持每批实际保存目录、独立状态与人工补充。

**Architecture:** 保留现有数据库、原图缓存、串行 Worker 和浏览器恢复机制。新增准备结构、批次/保存/人工修订记录及资料包服务，使用来源 ID 关联和相对素材路径；对旧接口保留兼容契约。

**Tech Stack:** 现有 Python、FastAPI、SQLAlchemy、SQLite、BeautifulSoup、Playwright、HTML/JavaScript；文件夹选择使用 Windows 本地对话框，不新增云依赖。

**Spec:** `docs/superpowers/specs/2026-10-05-product-preparation-design.md` 及其引用的已确认完整报告。

## Global Constraints

- 原始 Capture、原图、历史商品资料和登录会话保留，禁止无关重构。
- 仅采集与准备；不得触发平台保存、提交、上架、下架等写操作。
- 状态使用 obtained / not_set / not_applicable / not_obtained / failed，待确认独立。
- 四项物流信息及其附属规则不主动采集、不导出、不计必填。
- 3:4 主图和店铺中分类不主动纳入。
- 价格十进制文本，币种和数量单位独立；来源 ID 不能当作未来平台 ID。
- 每次更新保留完整历史版本；人工修订不能被重新采集覆盖。
- 通用 CSV 是资料整理格式，不宣称平台直接导入。
- Windows 中文、空格、设备保留名和路径长度必须处理。
- 所有测试使用隔离目录和样例，不读取真实凭据，不改变线上商品。
- 无 Git 仓库；以文件备份及逐任务验证记录代替 Git 提交和依赖 Git 的技能脚本。

## Review Focus

1. 跨店铺批次共享同一来源商品时，暂停/删除一个批次不能改写另一个批次的目标或保存记录。
2. 同一文件同时是主图、详情图和 SKU 图片时，去重文件但完整保留全部引用及重复详情位置。
3. 手工补充 SKU 后来源删除/调整 SKU 顺序时，人工值不按数组位置误贴到别的 SKU。
4. 目录检查通过后磁盘断开或写入中断时，采集数据持久化，未完整包不显示保存成功。
5. 旧数据来源不明、类目规则未知或后台无法读取时，不能升级为卖家实际值或显示必填齐全。

## 文件职责

新增 `app/preparation.py` 负责结构、状态、白名单和完整度；`app/product_package.py` 负责包文件、离线页面与校验；`app/preparation_service.py` 负责批次保存、人工修订及兼容读取；`app/taobao/seller_read.py` 负责有证据的卖家数据读取/导入边界；`scripts/select_folder.ps1` 负责 Windows 目录选择。

现有 `products.py` 增加新记录模型；`parser.py` 修正数据准确性并增加详情块；`product_service.py` 保存新准备快照；`collection_service.py` 接入新批次与保存；`main.py` 增加兼容请求及准备接口；`collection_export.py` 接入完整资料包；现有模板、脚本与样式增加相应控件。

### Task 1: 来源解析准确性与图文块

**Files:** 修改 `app/taobao/parser.py`、`selectors.py`；新增 `tests/test_preparation_parser.py`。

**Interfaces:** ParsedProduct 以有默认值的新字段扩展 `detail_blocks: list[dict]`、`category_data: dict`、`buyer_display: dict`，不破坏旧位置参数和检查点。图片引用保留 role 与用途内 order，详情块保存 type/text/url/order。

- [x] 实施前备份 app/templates/static/scripts/requirements/README 和 SQLite 一致性备份，保留浏览器会话目录原样。源码备份不包含 .env 密钥；备份检查成功后才修改业务代码。
- [x] 写失败测试：quantity=0 保留 0；区间 10-20 不作为单一实际价格；同 URL 多角色保留；文字、图、文字、重复图顺序保留；详情容器外推荐不进入图文块；错商品 ID 的响应不参与关联。
- [x] 运行 `.venv/Scripts/python.exe -m pytest tests/test_preparation_parser.py -q`，确认新断言失败。
- [x] 修正 truthiness 取数、价格区间和素材引用；新增安全详情顺序提取；记录公开值来源，保留原名称及原值，不猜类目。
- [x] 同时运行上述测试与 `tests/test_shop_detail_collection.py`，应全通过；记录改动及兼容偏差。

### Task 2: 准备结构与人工值保护

**Files:** 新增 `app/preparation.py`；修改 `app/products.py`、`app/product_service.py`；新增 `tests/test_preparation_data.py`。

**Interfaces:** `build_preparation(product, parsed=None, aliases=None) -> dict`；`apply_overrides(document: dict, overrides: dict) -> dict`；`evaluate_completeness(document: dict) -> dict`。新表 PreparationCapture、ManualRevision 保存不可变准备快照及修订；旧 Product 表不移除字段。

- [x] 写失败测试：状态四分、未知非 0、已知零、单位独立、部分 SKU 不算总库存、物流递归排除、原始/最终值分离、类目规则未知不齐全。
- [x] 运行数据测试确认失败，再实现白名单分组和来源结构。
- [x] 写来源更新与人工修订测试：稳定来源 SKU 身份、不按序号覆盖；重采保留人工值、raw_data、Capture 和旧 current_data 证据。
- [x] 保存不可变准备快照和旧数据保护快照，建立最终值合并规则及冲突清单。
- [x] 运行数据测试、`tests/test_collection_review.py` 与 `tests/test_collection_tool.py`，应全通过。

### Task 3: 完整离线资料包与历史版本

**Files:** 新增 `app/product_package.py`；修改 `app/collection_export.py`；新增 `tests/test_product_package.py`。

**Interfaces:** `write_package(data_dir: Path, product, document: dict, target: Path, check=None) -> dict`；返回 status/path/errors/verified_at。`verify_package(path: Path) -> dict` 校验文件及素材哈希。使用 Task 2 的 document，CSV 中 source_* 是追溯字段。

- [x] 写失败测试：JSON/商品 CSV/SKU CSV/README.html/detail.html/清单齐备；每条 SKU 图片准确；重复详情图保留位置；四项物流不出包。
- [x] 测试先失败，再生成版本完整目录、UTF-8 JSON/CSV、HTML 安全转义、相对素材引用及原字节复制。
- [x] 增加移动目录后逐一检查 href/src 存在、无远程主动加载；多角色只复制一份原文件；原图哈希不变；新版本不覆盖旧包。
- [x] 注入复制失败/中断，验证未写完整标记、采集记录仍存在，并可恢复或新版本补存。
- [x] ZIP 采用同一套包结构，兼容原 JSON/CSV 下载路由；运行新测试与既有导出/原图测试，应全通过。

### Task 4: 批次目录、保存状态与恢复

**Files:** 新增 `app/preparation_service.py`；修改 `products.py`、`collection_service.py`、`main.py`；新增 `tests/test_preparation_batches.py`。

**Interfaces:** `validate_directory(path: str) -> Path`；`create_batch(session, directory: str, mode: str) -> PreparationBatch`；`save_job_package(session_factory, data_dir: Path, task_id: str) -> dict`。BatchCollectionRequest/CollectionRequest 增加可选 `save_directory`、`collection_mode`（reuse/refresh）。旧请求无目录保留原有任务复用行为。

- [x] 写失败测试：相同商品不同目录产生独立保存请求与完整包；店铺子商品继承批次目录；偏好变更不改旧任务；refresh 重读来源、reuse 显示旧时间且复用原图。
- [x] 运行批次测试确认失败，再新增批次和包保存表，任务 preset 固定 batch_id/target；新批次内按来源 ID 去重，不跨批次复用任务身份。
- [x] 保存失败仅更新 package_status；采集结果先提交。新增补存接口，补存不访问淘宝；批次汇总分别统计采集、必填与保存。
- [x] 增加不可写目录、文件代替目录、中文空格、保留设备名、重名、长路径、暂停中断、重启恢复、跨批次暂停/删除隔离测试。
- [x] 运行新测试与现有暂停/重试/删除/进度测试，应全通过。

### Task 5: 已保存卖家数据的只读补充边界

**Files:** 新增 `app/taobao/seller_read.py`；修改 `main.py`、`preparation_service.py`；新增 `tests/test_seller_read.py`。

**Interfaces:** `read_capabilities() -> dict`；`merge_saved_seller_data(document: dict, source: dict) -> dict`。仅接受有商品 ID、来源说明及已保存状态证据的数据，格式未核实的文件不得冒充官方导出。

- [x] 写失败测试：商品 ID 错误拒绝；新建表单默认值拒绝；SKU ID 缺失且组合歧义拒绝；实际价库存优先但原值及冲突保留；无法读取原因明确；物流白名单之外无字段进入结构。
- [x] 运行测试先失败，再实现经过验证数据的白名单合并与能力说明，禁止猜后台接口和选择器。
- [x] 尝试当前可用浏览器只读访问；遇到登录/验证提示人工完成；无法访问时保留详细限制，继续非依赖工作。
- [x] 检查是否存在可用本地 OCR；有则建立原图关联辅助证据，没有则显示未识别原因，不安装未经需要的新服务。
- [x] 运行 seller 测试与公开采集回归测试；记录哪些结论有真实页面证据，哪些只有样例验证。

### Task 6: 路径选择、补存和人工补充界面

**Files:** 新增 `scripts/select_folder.ps1`；修改 `main.py`、`templates/collection.html`、`product_dialog.html`、`static/js/collection.js`、`static/css/collection.css`；新增 `tests/test_preparation_ui.py`。

**Interfaces:** GET `/api/collection/preparation/settings`；POST `/api/collection/preparation/directory/check`、`directory/select`；GET `/api/collection/products/{id}/preparation`；POST `/api/collection/products/{id}/manual`；POST `/api/collection/jobs/{id}/save-package`。人工 API 校验字段路径、状态和值，不接受未知路径或物流字段。

- [x] 写路由/界面失败测试：保存路径显示完整、取消选择不创建任务、目录错误阻止开始、复用/刷新文案、三种状态分别显示、缺失列表及 SKU 图片关联。
- [x] 运行新测试确认失败，再接入真实 Windows 文件夹对话框（本地服务限制、单实例保护、隐藏辅助控制台）；完整路径输入作替代，偏好存入本地设置。
- [x] 人工编辑提供字段值、读取状态、待确认、单位；支持 SKU 稳定身份路径。人工保存只改本地修订并产生新包版本。
- [x] UI 暴露真实保存路径和补存原因；后台不可读明确提示；历史上架记录不误作未来设置。
- [x] 在宽屏/窄屏实际浏览器验证交互、关闭/键盘焦点、安全转义及无横向溢出；运行新测试和既有 UI 回归测试，应全通过。

### Task 7: 验收、文档与数据保留核对

**Files:** 更新 `README.md`；新增 `reports/product-preparation-validation-2026-10-05.md`；必要时扩展 `scripts/collection_ui_acceptance.py`。

**Interfaces:** 不新增业务接口。接收前六任务的结构与验收证据，输出支持字段、真实读取限制、备份位置、启动方式及使用说明。

- [x] 核对 Task 1 的实施前源码备份、SQLite 一致性备份和浏览器会话保护记录。
- [x] 全量 `.venv/Scripts/python.exe -m pytest -q` 及 `node --check static/js/collection.js`；Expected：全部测试通过、脚本语法退出码 0。
- [x] 使用隔离服务进行普通/多 SKU、选目录、自动包保存、补存、人工修订、移动离线查看验收，不把样例文件当真实商品资料。
- [x] 核对生产历史记录计数及原图保护，没有清理或覆盖历史；汇总十项用户验收场景及真实后台未验证事项。
- [x] 按技能要求开展独立代码审阅，修复实质问题后只重复受影响验证；更新计划勾选及报告。

## 计划自查

全部用户字段、四项排除、素材用途、SKU 关联、目录保存、复用/刷新、历史和人工值、三类状态及十项验收均有归属任务。真实后台、官方导出和 OCR 能力缺失不阻止本地流程实现，但不能虚构已取得字段。Task 2 文档为 Task 3/5/6 的共同输入；Task 4 固定保存目标为 Task 6 补存接口的共同输入，无相互冲突契约。

## 执行方式建议

建议由当前会话逐项实现，完成后进行独立审阅：这些任务共享来源结构、任务状态和资料包接口，顺序实施便于保持兼容。也可选择逐任务子代理实现与审阅，增加独立检查轮次及上下文成本。已按当前会话顺序实施；独立审阅发现的资料关联、排除和中断问题已修复。真实后台、官方导出、OCR 与真实手选文件夹未验证，明确限制见 reports/product-preparation-validation-2026-10-05.md，不将样例测试算作真实读取成功。
