<#
 Shared portable runtime for own developments (AI Launcher, Model Manager, ...).
   <ROOT>\@Runtime\python      full relocatable CPython 3.13 (python-build-standalone, install_only)
   <ROOT>\@Runtime\wheels      downloaded wheels (re-install works offline)
   <ROOT>\@Runtime\bin         drive-letter-independent wrappers (pyside6-designer.cmd, ...)
   <ROOT>\@Runtime\downloads   archives
 Nothing is copied from other tools; everything is downloaded fresh and SHA256-verified.
 Windows PowerShell 5.1 compatible.  Run:  powershell -ExecutionPolicy Bypass -File Setup_Runtime.ps1
#>
param(
    [string]$PythonSeries = "3.13",
    [switch]$Force            # re-download python even if present
)
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.Net.Http

$Here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Root = (Resolve-Path (Join-Path $Here "..")).Path
$RT   = Join-Path $Root "@Runtime"
$Py   = Join-Path $RT "python"
$PyExe = Join-Path $Py "python.exe"
$Wheels = Join-Path $RT "wheels"
$Down   = Join-Path $RT "downloads"
$Bin    = Join-Path $RT "bin"
$Req    = Join-Path $Here "requirements.txt"

function Step($n, $t) { Write-Host "`n[$n] $t" -ForegroundColor Cyan }

function Get-File([string]$Url, [string]$Out, [string]$Label) {
    $client = New-Object System.Net.Http.HttpClient
    $client.Timeout = [TimeSpan]::FromMinutes(30)
    $client.DefaultRequestHeaders.UserAgent.ParseAdd("AI-Launcher-Setup")
    $resp = $client.GetAsync($Url, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).Result
    $resp.EnsureSuccessStatusCode() | Out-Null
    $total = $resp.Content.Headers.ContentLength
    $in = $resp.Content.ReadAsStreamAsync().Result
    $fs = [IO.File]::Create($Out)
    try {
        $buf = New-Object byte[] 1MB; $read = 0; $last = 0
        while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
            $fs.Write($buf, 0, $n); $read += $n
            if ($total -and ($read - $last) -gt 2MB) {
                $last = $read
                Write-Progress -Activity "Downloading $Label" -Status ("{0:N0} / {1:N0} MB" -f ($read/1MB), ($total/1MB)) -PercentComplete ([int](100*$read/$total))
            }
        }
    } finally { $fs.Close(); $in.Close(); Write-Progress -Activity "Downloading $Label" -Completed }
}

function Invoke-GitHubJson([string]$Url) {
    Invoke-RestMethod -Uri $Url -Headers @{ "User-Agent" = "AI-Launcher-Setup"; "Accept" = "application/vnd.github+json" }
}

Write-Host "Root:    $Root`nRuntime: $RT"
foreach ($d in $RT, $Wheels, $Down, $Bin) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

# ---------------------------------------------------------------- 1. Python
Step "1/4" "Python $PythonSeries (full, relocatable)"
if ((Test-Path $PyExe) -and -not $Force) {
    Write-Host "  already present: $(& $PyExe -V)"
} else {
    $rel = Invoke-GitHubJson "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
    $pattern = "^cpython-" + [regex]::Escape($PythonSeries) + "\.\d+\+\d+-x86_64-pc-windows-msvc-install_only\.tar\.gz$"
    $asset = $null; $sums = $null; $assetVer = $null
    for ($page = 1; $page -le 20 -and -not ($asset -and $sums); $page++) {
        $assets = Invoke-GitHubJson "https://api.github.com/repos/astral-sh/python-build-standalone/releases/$($rel.id)/assets?per_page=100&page=$page"
        if (-not $assets) { break }
        foreach ($a in $assets) {
            if ($a.name -match $pattern) {
                $ver = [version](($a.name -split "\+")[0] -replace "^cpython-", "")
                if (-not $asset -or $ver -gt $assetVer) { $asset = $a; $assetVer = $ver }
            }
            if ($a.name -eq "SHA256SUMS") { $sums = $a }
        }
    }
    if (-not $asset) { throw "No python-build-standalone asset for $PythonSeries found in release $($rel.tag_name)." }
    Write-Host "  release $($rel.tag_name): $($asset.name)"
    $tar = Join-Path $Down $asset.name
    Get-File $asset.browser_download_url $tar $asset.name

    if ($sums) {
        $sumFile = Join-Path $Down "SHA256SUMS"
        Get-File $sums.browser_download_url $sumFile "SHA256SUMS"
        $line = Get-Content $sumFile | Where-Object { $_ -match ([regex]::Escape($asset.name) + "$") } | Select-Object -First 1
        if ($line) {
            $expected = ($line -split "\s+")[0].ToLower()
            $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $tar).Hash.ToLower()
            if ($expected -ne $actual) { Remove-Item $tar -Force; throw "SHA256 mismatch for $($asset.name)" }
            Write-Host "  SHA256 verified"
        } else { Write-Warning "no checksum line for the archive - skipped verification" }
    } else { Write-Warning "SHA256SUMS not found in release - skipped verification" }

    if (Test-Path $Py) { Remove-Item $Py -Recurse -Force }
    Write-Host "  extracting..."
    & tar.exe -xzf $tar -C $RT          # archive root folder is "python"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $PyExe)) { throw "extract failed" }
    Write-Host "  $(& $PyExe -V) ready"
}

# ---------------------------------------------------------------- 2. pip
Step "2/4" "pip"
& $PyExe -m ensurepip --upgrade 2>&1 | Out-Null
& $PyExe -m pip --version

# ---------------------------------------------------------------- 3. packages
Step "3/4" "Packages from requirements.txt (Qt6 complete + dev tools)"
# 'python -m pip' on purpose: pip.exe / Scripts\*.exe launchers contain absolute paths
$common = @("--no-warn-script-location", "--no-index", "--find-links", $Wheels, "-r", $Req)
$ErrorActionPreference = "Continue"
& $PyExe -m pip install @common *> $null
$ErrorActionPreference = "Stop"
if ($LASTEXITCODE -ne 0) {
    Write-Host "  not fully in the local wheel cache - downloading (internet needed once)..."
    & $PyExe -m pip download -r $Req -d $Wheels
    if ($LASTEXITCODE -ne 0) { throw "pip download failed" }
    & $PyExe -m pip install @common
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}

# ---------------------------------------------------------------- 4. wrappers
Step "4/4" "Portable tool wrappers in @Runtime\bin"
$tools = "designer","uic","rcc","linguist","lupdate","lrelease","assistant","deploy","project","qmllint","qmlformat","genpyi"
foreach ($t in $tools) {
    $cmd = "@echo off`r`n`"%~dp0..\python\python.exe`" -c `"import sys; from PySide6.scripts.pyside_tool import $t as f; sys.exit(f())`" %*`r`n"
    Set-Content -LiteralPath (Join-Path $Bin "pyside6-$t.cmd") -Value $cmd -Encoding ASCII
}
Set-Content -LiteralPath (Join-Path $Bin "python.cmd") -Value "@echo off`r`n`"%~dp0..\python\python.exe`" %*`r`n" -Encoding ASCII
Set-Content -LiteralPath (Join-Path $Bin "pyinstaller.cmd") -Value "@echo off`r`n`"%~dp0..\python\python.exe`" -m PyInstaller %*`r`n" -Encoding ASCII
Write-Host "  wrappers: python, pyinstaller, pyside6-{$($tools -join ',')}"

# ---------------------------------------------------------------- verify
& $PyExe -c "import PySide6, shiboken6, sys; from PySide6.QtWidgets import QApplication; print('OK  Python', sys.version.split()[0], ' PySide6', PySide6.__version__)"
if ($LASTEXITCODE -ne 0) { throw "verification failed" }
Write-Host "`nDone. Add  $Bin  to PATH in your shell if you want the wrappers everywhere." -ForegroundColor Green
