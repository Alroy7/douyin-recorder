<#
.SYNOPSIS
  Initialize a local git repository for the source project and commit.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools/init_git.ps1 -Name "Your Name" -Email "you@example.com" -Remote "https://github.com/you/douyin-recorder.git"
#>
param(
    [string]$Name = "",
    [string]$Email = "",
    [string]$Remote = "",
    [switch]$Push
)
$ErrorActionPreference = "Continue"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    Write-Host "Git not found. Install it first:" -ForegroundColor Yellow
    Write-Host "  winget install --id Git.Git -e --source winget" -ForegroundColor Yellow
    Write-Host "  or download: https://git-scm.com/download/win" -ForegroundColor Yellow
    exit 2
}

if (-not (Test-Path (Join-Path $Root ".git"))) {
    git init -b main
    Write-Host "Initialized local repository (main)." -ForegroundColor Green
} else {
    Write-Host "Existing .git found." -ForegroundColor Green
}

if ($Name) { git config user.name $Name }
if ($Email) { git config user.email $Email }
$curName = git config user.name
$curEmail = git config user.email
if (-not $curName -or -not $curEmail) {
    Write-Host "Set your identity first (run with -Name and -Email, or git config manually)." -ForegroundColor Yellow
    exit 2
}
Write-Host ("Author: " + $curName + " <" + $curEmail + ">")

git add -A
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "chore: initial commit"
    Write-Host "Initial commit created." -ForegroundColor Green
} else {
    Write-Host "No changes to commit." -ForegroundColor Yellow
}

if ($Remote) {
    $existing = git remote get-url origin 2>$null
    if ($LASTEXITCODE -eq 0 -and $existing) {
        git remote set-url origin $Remote
    } else {
        git remote add origin $Remote
    }
    Write-Host ("Remote origin: " + $Remote) -ForegroundColor Green
    if ($Push) {
        git push -u origin main
    } else {
        Write-Host "Next step: git push -u origin main" -ForegroundColor Cyan
    }
} else {
    Write-Host "No remote configured. Add one with: git remote add origin <repo-url>" -ForegroundColor Cyan
}
