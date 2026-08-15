# 停掉后台运行的 Saymore。
$procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*saymore.main*" -or $_.CommandLine -like "*voice_input.py*" -or $_.CommandLine -like "*run_saymore.py*" }
$procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "已停 PID $($_.ProcessId)" }
# 硬杀 python 会跳过 atexit,llama-server 子进程不会自己死 → 必须显式收掉,否则孤儿占显存(端口写死单实例,按镜像名杀不误伤)
Get-Process llama-server -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.Id -Force; Write-Host "已停 llama-server PID $($_.Id)" }
if (-not $procs) { Write-Host "python 侧没在运行(llama-server 已顺带清理)。" }
