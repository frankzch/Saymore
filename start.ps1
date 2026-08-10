# 一键后台启动 Saymore。用 venv 里的 pythonw.exe（无黑窗、无控制台），
# 日志按日切分落到 logs\voice_input-YYYY-MM-DD.log（utf-8，每行带毫秒时间戳）。已在跑就不重复启。
# 用法：powershell -ExecutionPolicy Bypass -File start.ps1

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$pythonw = Join-Path $here ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "找不到 $pythonw，先按 README 建 .venv 并装依赖" }

$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*saymore.main*" -or $_.CommandLine -like "*voice_input.py*" }
if ($running) { Write-Host "已在运行（PID $($running.ProcessId -join ',')），跳过启动。"; return }

Start-Process $pythonw -ArgumentList "-m","saymore.main" -WorkingDirectory $here
Write-Host "已后台启动。日志目录：logs\ 。停用：powershell -File stop.ps1"
