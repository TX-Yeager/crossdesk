$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
# Mark the attempt before UAC so uninstall can clean up partial configuration.
$marker = Join-Path $PSScriptRoot 'install.ini'
$text = [IO.File]::ReadAllText($marker)
$text = $text -replace 'FirewallAttempted=0', 'FirewallAttempted=1'
[IO.File]::WriteAllText($marker, $text, [Text.Encoding]::Unicode)
& (Join-Path $PSHOME 'powershell.exe') -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File (Join-Path $PSScriptRoot 'Configure-Firewall.ps1') -Action Install
$result = $LASTEXITCODE
$message = switch ($result) {
    0 { 'CrossDesk 的专用局域网防火墙规则已配置。请在两台电脑上打开 CrossDesk 并配对。' }
    3 { '规则已配置，但当前没有专用网络。请仅将你信任的家庭或办公室网络设为“专用”，再连接。' }
    default { '管理员授权被取消或配置失败。请重试，或联系电脑管理员。详情见安装目录中的 firewall-result.txt（如有）。' }
}
[Windows.Forms.MessageBox]::Show($message, 'CrossDesk 局域网连接') | Out-Null
exit $result
