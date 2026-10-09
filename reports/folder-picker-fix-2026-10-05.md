# 选择文件夹无响应修复

本次发现并处理两条原因：

1. 旧 collection.js 没有准备资料按钮事件，旧 CSS 也没有路径输入样式；新 HTML 沿用相同静态 URL，会被旧缓存影响。为三份页面资源添加内容哈希版本，新服务会随文件内容变更生成不同 URL；兼容当前已启动服务的模板回退版本，让本次刷新立即生效。
2. select_folder.ps1 原为不带 BOM 的 UTF-8，本机 Windows PowerShell 解析该中文脚本时产生两项错误（字符串未结束及意外符号），窗口尚未创建就退出。改用 UTF-8 BOM，保持中文提示、参数及选中路径的 UTF-8 输出。

验证：新增两个回归测试先失败，再修复通过。实际 powershell.exe ParseFile 错误从 2 个降为 0，中文提示保持正确；资源 URL 会随单文件变化单独更新。目录接口、三页面/弹层 UI 一组共 5 passed；路由、进度和批次保存相关一组共 14 passed，均仅有既有 Starlette 弃用警告。

当前 127.0.0.1:8000 页面已读取带新版本的资源。独立 Chrome 验证点击会发送 POST 选择请求，取消回复能显示、按钮恢复可用，新输入框高度 45.25px，页面脚本错误为零。该浏览器验证拦截并模拟取消回复，避免代替用户选目录；Windows 原生交互窗口仍由用户手选。截图 artifacts/folder-button-fixed.png；输出 folder-button-live-check.txt。

未重启正在运行的采集服务，未操作店铺或新建采集任务。修复前文件保存在 reports/folder-picker-before-fix-20261005。

用户操作：刷新采集页（Ctrl+F5）后重新点击“选择文件夹”；无需为这次修复重启服务。
