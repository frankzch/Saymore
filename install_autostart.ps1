# 注册/卸载开机自启。登录当前用户时，在交互会话里后台运行（无控制台窗口）。
# 不能用 Windows 服务：服务在 Session 0 没有桌面，无法粘贴/弹悬浮窗/全局热键。
#
# 安装：右键“以管理员身份运行 PowerShell”，先激活你的 venv（如有），再执行本脚本。
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1
# 卸载：
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Uninstall

# -Elevated：以最高权限运行（可粘进管理员窗口），但注册时需管理员身份。默认普通权限，免管理员。
param([switch]$Uninstall, [switch]$Elevated)

$ErrorActionPreference = "Stop"
$TaskName = "VoiceInputCN"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已卸载自启任务 $TaskName。"
    return
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$script = Join-Path $here "voice_input.py"

# pythonw.exe 与当前 python 同目录（无黑窗口）；激活 venv 后会指向 venv 的解释器
$py = (Get-Command python).Source
$pythonw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $py }
Write-Host "使用解释器：$pythonw"

$action    = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $here
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$runLevel  = if ($Elevated) { "Highest" } else { "Limited" }
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel $runLevel
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "已注册开机自启任务 $TaskName。注销/重启登录后自动后台运行。"
Write-Host "想立即试跑：Start-ScheduledTask -TaskName $TaskName"
Write-Host "想停止本次：Stop-ScheduledTask  -TaskName $TaskName"


