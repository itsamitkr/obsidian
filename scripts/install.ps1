<#
.SYNOPSIS
    One-shot install of the Knowledge System and everything it uses, for the
    current user only. No admin rights needed. Safe to re-run.

.DESCRIPTION
    Run `install.cmd` from the repo root (it calls this script with the
    PowerShell execution policy bypassed for this one run). It installs:

      * uv + Python 3.12   into .\.tools\ (nothing global; delete the folder to remove)
      * .venv + all Python dependencies: document conversion (MarkItDown),
        spreadsheets, local embedding model, embedded Qdrant vector DB,
        Control Panel, MCP server, dev tools
      * the embedding model (~90 MB, downloaded once; later starts are offline)
      * Obsidian           (optional UI)        per-user, skip with -NoObsidian
      * Ollama + a model   (optional local LLM)  per-user, skip with -NoLlm

    It also creates .env (if missing) and the vault in .\knowledge-vault.

.PARAMETER NoObsidian
    Don't install Obsidian.

.PARAMETER NoLlm
    Don't install Ollama or pull a model; enrichment stays off.

.PARAMETER LlmModel
    Ollama model to pull and use for enrichment (default llama3.2:3b, ~2 GB).

.PARAMETER Apps
    MCP apps to connect the knowledge base to: claude-desktop, cursor, kiro
    (comma-separated), or none. If omitted, the installer asks.
#>
param(
    [switch]$NoObsidian,
    [switch]$NoLlm,
    [string]$LlmModel = "llama3.2:3b",
    [string]$Apps = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$UvVersion = "0.13.0"
$PythonVersion = "3.12"
$tools = Join-Path $root ".tools"
$downloads = Join-Path $env:TEMP "knowledge-install"
New-Item -ItemType Directory -Force $tools, $downloads | Out-Null

$results = [ordered]@{}

# Ask up front so the rest of the install can run unattended.
if (-not $Apps -and [Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
    Write-Host ""
    Write-Host "Which AI apps should use your knowledge base?" -ForegroundColor Cyan
    Write-Host "  claude-desktop, cursor, kiro (comma-separated), or press Enter to skip"
    $Apps = Read-Host "Apps"
}
$Apps = ($Apps -replace '\s', '').ToLower()
if ($Apps -eq "none") { $Apps = "" }

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

function Get-File($url, $name) {
    $dest = Join-Path $downloads $name
    Write-Host "    downloading $url"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $dest
    return $dest
}

function Assert-Sha256($path, $expected) {
    $actual = (Get-FileHash -Algorithm SHA256 $path).Hash.ToLower()
    if ($actual -ne $expected.ToLower()) {
        Remove-Item $path -Force
        throw "Checksum mismatch for $(Split-Path -Leaf $path); the download was deleted."
    }
}

function Assert-Signed($path) {
    $sig = Get-AuthenticodeSignature $path
    if ($sig.Status -ne "Valid") {
        Remove-Item $path -Force
        throw "$(Split-Path -Leaf $path) is not validly signed ($($sig.Status)); the download was deleted."
    }
    Write-Host "    signed by: $($sig.SignerCertificate.Subject)"
}

function Set-EnvValue($key, $value) {
    $lines = Get-Content ".env"
    $found = $false
    $lines = $lines | ForEach-Object {
        if ($_ -match "^\s*$([regex]::Escape($key))=") { $found = $true; "$key=$value" } else { $_ }
    }
    if (-not $found) { $lines += "$key=$value" }
    Set-Content ".env" $lines -Encoding UTF8
}

# --------------------------------------------------------------------------
# 1. uv (Python + package manager), kept inside .tools\
# --------------------------------------------------------------------------
function Find-Uv {
    $f = Get-ChildItem (Join-Path $tools "uv") -Recurse -Filter "uv.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($f) { return $f.FullName }
    return $null
}

$uv = Find-Uv
if (-not $uv) {
    Step "Installing uv $UvVersion (into .tools\uv)"
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "aarch64" } else { "x86_64" }
    $asset = "uv-$arch-pc-windows-msvc.zip"
    $base = "https://github.com/astral-sh/uv/releases/download/$UvVersion"
    $zip = Get-File "$base/$asset" $asset
    $sum = (Invoke-WebRequest -UseBasicParsing "$base/$asset.sha256").Content
    if ($sum -is [byte[]]) { $sum = [Text.Encoding]::ASCII.GetString($sum) }
    Assert-Sha256 $zip ($sum.Trim() -split '\s+')[0]
    Expand-Archive -Force $zip (Join-Path $tools "uv")
    $uv = Find-Uv
    if (-not $uv) { throw "uv.exe not found after extracting $asset" }
}
$results["uv"] = "ok"

# Python interpreters managed by uv also live in .tools\ (no system changes).
$env:UV_PYTHON_INSTALL_DIR = Join-Path $tools "python"

# --------------------------------------------------------------------------
# 2. Python + virtual environment + all Python dependencies
# --------------------------------------------------------------------------
$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Step "Creating .venv with Python $PythonVersion (downloaded by uv if needed)"
    & $uv venv .venv --python $PythonVersion --seed
    if ($LASTEXITCODE -ne 0) { throw "Could not create .venv" }
}

Step "Installing the Knowledge System and all Python dependencies"
Write-Host "    (first run downloads ~1 GB, mostly PyTorch for the embedding model)"
& $uv pip install --python $py -e ".[all,dev]"
if ($LASTEXITCODE -ne 0) { throw "Dependency install failed" }
$results["Python + dependencies"] = "ok"

if (-not (Test-Path ".env")) {
    Step "Creating .env from .env.example"
    Copy-Item ".env.example" ".env"
}

Step "Creating the vault (.\knowledge-vault)"
& .\.venv\Scripts\knowledge.exe init
if ($LASTEXITCODE -ne 0) { throw "knowledge init failed" }

# --------------------------------------------------------------------------
# 3. Obsidian (optional UI), per-user
# --------------------------------------------------------------------------
function Test-Obsidian {
    (Test-Path "HKCU:\Software\Classes\obsidian") -or
    (Test-Path "$env:LOCALAPPDATA\Programs\Obsidian\Obsidian.exe") -or
    (Test-Path "$env:LOCALAPPDATA\Obsidian\Obsidian.exe")
}

if ($NoObsidian) {
    $results["Obsidian"] = "skipped (-NoObsidian)"
} elseif (Test-Obsidian) {
    $results["Obsidian"] = "already installed"
} else {
    Step "Installing Obsidian (current user)"
    try {
        $rel = Invoke-RestMethod "https://api.github.com/repos/obsidianmd/obsidian-releases/releases/latest"
        $a = $rel.assets | Where-Object { $_.name -match '^Obsidian-[\d.]+\.exe$' } | Select-Object -First 1
        if (-not $a) { throw "no Windows installer in the latest release" }
        $exe = Get-File $a.browser_download_url $a.name
        if ($a.digest -match '^sha256:(.+)$') { Assert-Sha256 $exe $Matches[1] }
        Assert-Signed $exe
        Start-Process -Wait -FilePath $exe -ArgumentList "/S"
        if (-not (Test-Obsidian)) { throw "installer finished but Obsidian wasn't found" }
        $results["Obsidian"] = "installed $($rel.tag_name)"
    } catch {
        Warn "Obsidian install failed: $($_.Exception.Message)"
        Warn "It's optional. Get it from https://obsidian.md/download"
        $results["Obsidian"] = "FAILED (optional)"
    }
}

# --------------------------------------------------------------------------
# 4. Ollama + model (optional local LLM for enrichment), per-user
# --------------------------------------------------------------------------
function Get-OllamaExe {
    $c = Get-Command ollama -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    if (Test-Path $p) { return $p }
    return $null
}

function Wait-Ollama($seconds) {
    for ($i = 0; $i -lt $seconds; $i++) {
        try { Invoke-RestMethod "http://127.0.0.1:11434/api/version" -TimeoutSec 2 | Out-Null; return $true }
        catch { Start-Sleep 1 }
    }
    return $false
}

if ($NoLlm) {
    $results["Ollama + model"] = "skipped (-NoLlm)"
} else {
    try {
        $ollama = Get-OllamaExe
        if (-not $ollama) {
            Step "Installing Ollama (current user; ~1.5 GB download)"
            $exe = Get-File "https://ollama.com/download/OllamaSetup.exe" "OllamaSetup.exe"
            Assert-Signed $exe
            Start-Process -Wait -FilePath $exe -ArgumentList "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"
            $ollama = Get-OllamaExe
            if (-not $ollama) { throw "installer finished but ollama.exe wasn't found" }
        }
        if (-not (Wait-Ollama 3)) {
            Start-Process -FilePath $ollama -ArgumentList "serve" -WindowStyle Hidden
            if (-not (Wait-Ollama 60)) { throw "the Ollama server didn't start" }
        }
        Step "Pulling LLM model $LlmModel (one time)"
        & $ollama pull $LlmModel
        if ($LASTEXITCODE -ne 0) { throw "ollama pull $LlmModel failed" }

        # Turn enrichment on, unless .env already points at another LLM.
        $current = (Select-String -Path ".env" -Pattern '^\s*KS_LLM__PROVIDER=(.*)$').Matches.Groups[1].Value
        if (-not $current -or $current.Trim() -eq "null") {
            Set-EnvValue "KS_LLM__PROVIDER" "openai"
            Set-EnvValue "KS_LLM__BASE_URL" "http://localhost:11434/v1"
            Set-EnvValue "KS_LLM__MODEL" $LlmModel
        }
        $results["Ollama + model"] = "ok ($LlmModel)"
    } catch {
        Warn "Local LLM setup failed: $($_.Exception.Message)"
        Warn "It's optional; ingestion works without it. Get Ollama from https://ollama.com/download"
        $results["Ollama + model"] = "FAILED (optional)"
    }
}

# --------------------------------------------------------------------------
# 5. Load every provider once (downloads the embedding model the first time)
# --------------------------------------------------------------------------
Step "Checking the install (loads the embedding model, vector DB and LLM)"
& .\.venv\Scripts\knowledge.exe health
$results["Health check"] = if ($LASTEXITCODE -eq 0) { "ok" } else { "FAILED" }

# --------------------------------------------------------------------------
# 6. Connect MCP apps (Claude Desktop, Cursor, Kiro)
# --------------------------------------------------------------------------
if (-not $Apps) {
    $results["MCP apps"] = "none (later: knowledge connect <app>)"
} else {
    Step "Connecting MCP apps: $Apps"
    & .\.venv\Scripts\knowledge.exe connect $Apps
    $results["MCP apps"] = if ($LASTEXITCODE -eq 0) { "connected ($Apps)" } else { "FAILED" }
}

Remove-Item -Recurse -Force $downloads -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Summary" -ForegroundColor Cyan
$results.GetEnumerator() | ForEach-Object { Write-Host ("  {0,-24} {1}" -f $_.Key, $_.Value) }
Write-Host ""
if ($results["Health check"] -ne "ok") {
    Write-Host "The health check failed; see the output above. Re-run install.cmd after fixing it." -ForegroundColor Red
    exit 1
}
Write-Host "Install complete. Start the Control Panel with:  start.cmd" -ForegroundColor Green
if ($results["Obsidian"] -notmatch "skipped|FAILED") {
    Write-Host "Optional: in Obsidian, choose 'Open folder as vault' and pick .\knowledge-vault"
}
