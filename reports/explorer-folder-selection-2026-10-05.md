# 文件夹选择交互修复（2026-10-05）

已将采集页及补存资料包的目录选择改为 Windows 原生资源管理器样式窗口。可以浏览磁盘、选择已有文件夹，或在窗口中点击“新建文件夹”。采集页只显示选定的完整路径；补存入口也不再要求手动输入路径。

## 实际修改

- `scripts/ExplorerFolderPicker.cs`：使用系统 Common Item Dialog 的文件夹模式，独立 GUI 程序显示窗口，不依赖 PowerShell 脚本执行策略。返回 UTF-8 的实际文件系统路径，取消返回空结果。源码哈希决定本地编译缓存版本；使用本机已有的 .NET 编译器，无新增安装。原 `select_folder.ps1` 保留作历史代码，按钮不再调用。
- `app/preparation_routes.py`：启动原生组件，保留本机访问和同源限制、单窗口锁及超时处理。确认后的目录先显示和记忆，再单独报告可写性；不可写不会把“选好了目录”误报成“未选择”。
- `app/preparation_service.py`：可分别检查目录本身和实际写权限。开始采集及补存仍要求目录存在、路径合法且可写。
- `templates/collection.html`、`static/js/preparation.js`：保存位置为只读显示，说明窗口内可新建文件夹，取消保留原选择；移除手动输入补存目录的入口。
- `templates/base.html`：更新旧服务使用的静态资源版本后备标识，防止旧缓存继续使用之前的按钮逻辑。
- 更新 README，新增原生组件回归测试和实际窗口验收脚本。

系统接口参考：[Microsoft Common Item Dialog](https://learn.microsoft.com/en-us/windows/win32/shell/common-file-dialog)、[IFileDialog](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nn-shobjidl_core-ifiledialog)。

## 验证结果

- 相关回归：20 项通过；全部项目测试：154 项通过，91.49 秒。仅有既有 Starlette/AnyIO 弃用提示。
- `node --check static/js/preparation.js` 通过。
- 原生窗口已真实显示，检查了磁盘导航、目录列表、新建文件夹、选择及取消按钮。
- 在测试资料目录中通过原生窗口实际创建“新建文件夹”，选择该文件夹后组件正确返回其完整路径。
- 隔离数据库的真实页面验收：点击按钮 → 原生窗口 → 确认中文且带空格的目录 → 完整路径回填到只读位置 → 按钮恢复。无页面脚本错误。此测试未提交任何采集任务。
- 当前 `127.0.0.1:8000` 服务的真实取消验收：页面按钮调用真实目录选择接口，点击原生取消按钮后正常返回，原路径保留，按钮恢复。此测试没有使用模拟的目录选择响应。
- 不可写目录回归：选中目录仍返回并显示路径及错误；开始前目录检查返回拒绝，不会创建批次或宣称文件保存成功。
- 服务重启前确认 RUNNING/PENDING 均为 0，并用 SQLite 备份保存数据库。重启前后任务状态数量相同：CANCELED 124、FAILED 16、PARTIAL 4、SUCCESS 90；生产保存目录偏好未被验收测试更改。历史商品、图片及登录资料未作修改。

## 使用

刷新采集页面，点击“选择文件夹”。在 Windows 窗口中浏览到目标位置，选择已有文件夹或新建文件夹，再点击右下角“选择文件夹”。页面会显示完整路径，开始采集前自动检查可写性。ScraperAPI 配置及原采集流程继续沿用。

验收脚本：`python -m scripts.verify_native_folder_selection`（隔离资料的真实选择）；`python -m scripts.verify_folder_button --native`（当前本机服务的真实取消）。两者需要在实际原生窗口中完成相应操作。
