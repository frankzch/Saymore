# -*- coding: utf-8 -*-
# 打包一步到位：自动建 venv → 装依赖 → PyInstaller → Inno Setup
# 用法：
#   .\build.ps1                       # 打包 + 生成安装包（首次自动建 .venv-build）
#   .\build.ps1 -SkipInstaller        # 只打 PyInstaller 目录，跳过 Inno Setup
#   .\build.ps1 -Rebuild              # 强制重建 .venv-build（依赖有更新时用）
#
# 前置：Inno Setup 6，装 https://jrsoftware.org/isinfo.php ，把 ISCC.exe 加进 PATH
param([switch]$SkipInstaller, [switch]$Rebuild)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = "1"   # 防止 pip 读中文注释的 requirements 时 GBK 崩溃

# --- 准备 venv ---
$venv = ".venv-build"
$venvPy = Join-Path $venv "Scripts\python.exe"
$reqFile = "requirements-runtime.txt"
$stamp = Join-Path $venv ".req.stamp"   # 记录上次装依赖时 requirements 的哈希

if ($Rebuild -and (Test-Path $venv)) {
    Write-Host "== 强制重建 venv =="
    Remove-Item -Recurse -Force $venv
}

if (-not (Test-Path $venvPy)) {
    Write-Host "== 首次建 venv：$venv =="
    python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "创建 venv 失败" }
}

# requirements 变了才重装
$reqHash = (Get-FileHash $reqFile -Algorithm SHA256).Hash
$needInstall = $true
if (Test-Path $stamp) {
    if ((Get-Content $stamp -Raw).Trim() -eq $reqHash) { $needInstall = $false }
}
if ($needInstall) {
    Write-Host "== 装/更新依赖 =="
    & $venvPy -m pip install --upgrade pip
    & $venvPy -m pip install -r $reqFile
    if ($LASTEXITCODE -ne 0) { throw "pip install 失败" }
    Set-Content -Path $stamp -Value $reqHash -Encoding ascii
} else {
    Write-Host "== 依赖未变，跳过 pip install =="
}

# 让后续 pyinstaller 命令走 venv
$env:PATH = (Resolve-Path (Join-Path $venv "Scripts")).Path + ";" + $env:PATH

Write-Host "== 清理旧产物 =="
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

Write-Host "== PyInstaller 打包 =="
pyinstaller saymore.spec --clean --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

$distDir = "dist\Saymore"
if (-not (Test-Path $distDir)) { throw "PyInstaller 输出目录不存在：$distDir" }
$exePath = Join-Path $distDir "Saymore.exe"
if (-not (Test-Path $exePath)) { throw "找不到 Saymore.exe" }

$size = (Get-ChildItem -Recurse $distDir -File | Measure-Object Length -Sum).Sum
Write-Host ("打包完成：{0}  ({1:N0} MB)" -f $distDir, ($size/1MB))

if ($SkipInstaller) {
    Write-Host "已跳过 Inno Setup。可直接 .\dist\Saymore\Saymore.exe 试运行。"
    exit 0
}

# --- Inno Setup ---（自动找 ISCC.exe：PATH → v7 → v6，32/64 位路径都试）
$ISCC = $null
$cand = @(
    "iscc",
    "C:\Program Files (x86)\Inno Setup 7\ISCC.exe",
    "C:\Program Files\Inno Setup 7\ISCC.exe",
    "D:\Program Files (x86)\Inno Setup 7\ISCC.exe",
    "D:\Program Files\Inno Setup 7\ISCC.exe",
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe",
    "D:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "D:\Program Files\Inno Setup 6\ISCC.exe"
)
foreach ($c in $cand) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) { $ISCC = $cmd.Source; break }
    if (Test-Path $c) { $ISCC = $c; break }
}
if (-not $ISCC) { throw "找不到 Inno Setup 编译器 (ISCC.exe)。装 https://jrsoftware.org/isinfo.php 后重试。" }
Write-Host "使用 ISCC：$ISCC"

Write-Host "== Inno Setup 编译 =="
& $ISCC installer\setup.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 失败" }

Write-Host "全部完成。安装包在 installer\output\ 下。"
