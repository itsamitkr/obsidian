<#
.SYNOPSIS
    One-shot setup for the Knowledge System on Windows (PowerShell).

.DESCRIPTION
    Creates a virtual environment, installs the package with the requested
    dependency set, and scaffolds the knowledge vault. Run from the repo root:

        ./scripts/setup.ps1              # core + dev (runs on fallbacks)
        ./scripts/setup.ps1 -Extras all  # everything: markitdown, embeddings, qdrant, mcp, api
        ./scripts/setup.ps1 -Extras convert,embed,vector   # pick specific extras

.PARAMETER Extras
    Comma-separated optional-dependency groups to install (api, convert, embed,
    vector, mcp, all). 'dev' is always included.
#>
param(
    [string]$Extras = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "==> Creating virtual environment (.venv)" -ForegroundColor Cyan
if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

$py = ".\.venv\Scripts\python.exe"

Write-Host "==> Upgrading pip" -ForegroundColor Cyan
& $py -m pip install --upgrade pip -q

# Build the extras spec, always including dev.
$groups = @("dev")
if ($Extras) { $groups += ($Extras -split ",") | ForEach-Object { $_.Trim() } }
$spec = "-e .[" + ($groups -join ",") + "]"

Write-Host "==> Installing package: $spec" -ForegroundColor Cyan
& $py -m pip install $spec

Write-Host "==> Preparing .env" -ForegroundColor Cyan
if ((Test-Path ".env.example") -and (-not (Test-Path ".env"))) {
    Copy-Item ".env.example" ".env"
}

Write-Host "==> Scaffolding the knowledge vault" -ForegroundColor Cyan
& .\.venv\Scripts\knowledge.exe init

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Activate with:  .\.venv\Scripts\Activate.ps1"
Write-Host "Then try:       knowledge health"
