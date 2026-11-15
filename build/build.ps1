#Requires -Version 5.1
<#
.SYNOPSIS
  构建免 Python 的 Windows x64 发行包（PyInstaller onedir）。
.DESCRIPTION
  产物：
    dist/DouyinRecorder/            可直接双击运行的目录（含 bin/ffmpeg.exe、config.json）
    dist/DouyinRecorder_win64.zip   压缩包（整个目录）

  依赖：项目内 .pylibs 里解包的 PyInstaller（不污染全局 Python 环境）。
  首次构建前先执行：
    python analysis/pip_fetch.py pyinstaller pyinstaller-hooks-contrib altgraph packaging pefile pywin32-ctypes
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File build/build.ps1
  powershell -ExecutionPolicy Bypass -File build/build.ps1 -SkipZip
  powershell -ExecutionPolicy Bypass -File build/build.ps1 -NoSmokeTest
#>
[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$SkipZip,
    [switch]$NoSmokeTest,
    [string]$DistDir = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Pylibs = Join-Path $Root ".pylibs"
$Dist = if ($DistDir) { [System.IO.Path]::GetFullPath($DistDir) } else { Join-Path $Root "dist" }
$OutDir = Join-Path $Dist "DouyinRecorder"
$WorkDir = Join-Path $Root "build/work"
$Stage = Join-Path $Root "build/stage"
$Spec = Join-Path $Root "build/DouyinRecorder.spec"
$ZipPath = Join-Path $Dist "DouyinRecorder_win64.zip"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host ("==> " + $Text) -ForegroundColor Cyan
}

Write-Step "0/6 检查构建依赖"
if (-not (Test-Path (Join-Path $Pylibs "PyInstaller/__init__.py"))) {
    throw "缺少 .pylibs/PyInstaller。请先执行：$Python analysis/pip_fetch.py pyinstaller pyinstaller-hooks-contrib altgraph packaging pefile pywin32-ctypes"
}
$pyVer = & $Python --version 2>&1
if ($LASTEXITCODE -ne 0) { throw "无法运行 Python：$Python" }
Write-Host ("Python: " + $pyVer)
$env:PYTHONPATH = $Pylibs

Write-Step "1/6 生成构建资源（config 模板 / 图标）并校验 PyInstaller"
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
& $Python (Join-Path $Root "build/make_assets.py")
if ($LASTEXITCODE -ne 0) { throw "生成构建资源失败（退出码 $LASTEXITCODE）" }

Write-Step "2/6 清理旧产物"
if (Test-Path $OutDir) { Remove-Item $OutDir -Recurse -Force }
if (Test-Path $WorkDir) { Remove-Item $WorkDir -Recurse -Force }
if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }

Write-Step "3/6 PyInstaller 打包（onedir，双入口）"
$env:DYR_PROJECT_ROOT = $Root
Push-Location $Root
try {
    & $Python -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $WorkDir $Spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（退出码 $LASTEXITCODE）" }
}
finally {
    Pop-Location
    Remove-Item Env:DYR_PROJECT_ROOT -ErrorAction SilentlyContinue
}

$guiExe = Join-Path $OutDir "DouyinRecorder.exe"
$cliExe = Join-Path $OutDir "DouyinRecorder-CLI.exe"
if (-not (Test-Path $guiExe)) { throw "构建产物缺失：$guiExe" }
if (-not (Test-Path $cliExe)) { throw "构建产物缺失：$cliExe" }

Write-Step "4/6 冒烟测试（--check）"
if (-not $NoSmokeTest) {
    $logFile = Join-Path $Stage "smoke-check.log"
    $outLines = & $cliExe --check 2>&1 | ForEach-Object { "$_" }
    $rc = $LASTEXITCODE
    $outLines | Set-Content -Path $logFile -Encoding UTF8
    if ($rc -ne 0) {
        throw ("打包后 --check 失败（退出码 " + $rc + "）：" + [Environment]::NewLine + ($outLines -join [Environment]::NewLine))
    }
    Write-Host ($outLines -join [Environment]::NewLine)
}

Write-Step "5/6 清理测试残留（logs/data/records/watchlist）"
foreach ($name in @("logs", "data", "records", "watchlist.json")) {
    $p = Join-Path $OutDir $name
    if (Test-Path $p) { Remove-Item $p -Recurse -Force }
}

Write-Step "6/6 生成压缩包"
if (-not $SkipZip) {
    Compress-Archive -Path $OutDir -DestinationPath $ZipPath -CompressionLevel Optimal -Force
}

$files = Get-ChildItem $OutDir -Recurse -File
$totalBytes = ($files | Measure-Object -Property Length -Sum).Sum
$guiBytes = (Get-Item $guiExe).Length
$cliBytes = (Get-Item $cliExe).Length
$ffBytes = (Get-Item (Join-Path (Join-Path $OutDir "bin") "ffmpeg.exe")).Length
Write-Host ""
Write-Host "===== 构建完成 =====" -ForegroundColor Green
Write-Host ("产物目录 : " + $OutDir)
Write-Host ("文件数量 : " + $files.Count)
Write-Host ("目录体积 : " + [math]::Round($totalBytes / 1MB, 1) + " MB")
Write-Host ("GUI 主程序: " + [math]::Round($guiBytes / 1MB, 2) + " MB")
Write-Host ("CLI 程序 : " + [math]::Round($cliBytes / 1MB, 2) + " MB")
Write-Host ("ffmpeg   : " + [math]::Round($ffBytes / 1MB, 1) + " MB")
if (Test-Path $ZipPath) {
    Write-Host ("压缩包   : " + $ZipPath + " (" + [math]::Round((Get-Item $ZipPath).Length / 1MB, 1) + " MB)")
}
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
