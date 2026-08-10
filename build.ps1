# -*- coding: utf-8 -*-
# 打包一步到位：清旧产物 → PyInstaller → Inno Setup
# 用法（在 .venv-build 已激活的情况下）：
#   .\build.ps1                       # 打包 + 生成安装包
#   .\build.ps1 -SkipInstaller        # 只打 PyInstaller 目录，跳过 Inno Setup
#
# 前置：
#   1) 干净 venv：   python -m venv .venv-build && .\.venv-build\Scripts\Activate.ps1
#   2) 装依赖：      pip install -r requirements-runtime.txt
#   3) Inno Setup：  安装 https://jrsoftware.org/isinfo.php ，把 ISCC.exe 加进 PATH（或改下方 $ISCC 常量）
param([switch]$SkipInstaller)

$ErrorActionPreference = 'Stop'

Write-Host "== 清理旧产物 =="
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

Write-Host "== PyInstaller 打包 =="
pyinstaller typeoff.spec --clean --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

$distDir = "dist\Typeoff"
if (-not (Test-Path $distDir)) { throw "PyInstaller 输出目录不存在：$distDir" }
$exePath = Join-Path $distDir "Typeoff.exe"
if (-not (Test-Path $exePath)) { throw "找不到 Typeoff.exe" }

$size = (Get-ChildItem -Recurse $distDir -File | Measure-Object Length -Sum).Sum
Write-Host ("打包完成：{0}  ({1:N0} MB)" -f $distDir, ($size/1MB))

if ($SkipInstaller) {
    Write-Host "已跳过 Inno Setup。可直接 .\dist\Typeoff\Typeoff.exe 试运行。"
    exit 0
}

# --- Inno Setup ---
$ISCC = "iscc"  # 若未加入 PATH，改成绝对路径，如 "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
try { & $ISCC /? *> $null } catch { throw "找不到 Inno Setup 编译器 (ISCC)。装 https://jrsoftware.org/isinfo.php 后重试。" }

Write-Host "== Inno Setup 编译 =="
& $ISCC installer\setup.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 失败" }

Write-Host "全部完成。安装包在 installer\output\ 下。"
