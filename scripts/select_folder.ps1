param([string]$InitialDirectory = '')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
$dialog = [System.Windows.Forms.FolderBrowserDialog]::new()
$dialog.Description = '选择商品资料保存到的实际文件夹'
if ($dialog.GetType().GetProperty('UseDescriptionForTitle')) {
    $dialog.UseDescriptionForTitle = $true
}
if ($InitialDirectory -and (Test-Path -LiteralPath $InitialDirectory -PathType Container)) {
    $dialog.SelectedPath = $InitialDirectory
}
$owner = [System.Windows.Forms.Form]::new()
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.WindowState = 'Minimized'
try {
    $answer = $dialog.ShowDialog($owner)
    if ($answer -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::Write($dialog.SelectedPath)
    }
} finally {
    $dialog.Dispose()
    $owner.Dispose()
}
