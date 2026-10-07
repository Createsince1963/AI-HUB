<#
 Own ComfyUI updater for the portable workspace (replaces update\update_comfyui*.bat of the official package).
 Goals: never break the working CUDA build, never lose local settings, always be able to roll back.
   1. refuses to run while ComfyUI is running
   2. backup  -> COMFYUI\_backups\<timestamp>\ (pip freeze, commit, extra_model_paths, comfy.settings.json)
   3. updates ComfyUI source with the official updater script (update\update.py; -Stable = latest stable release)
   4. installs requirements with a CONSTRAINTS file that pins torch / torchvision / torchaudio / xformers /
      sageattention / triton to the installed versions (torch is never upgraded or replaced)
   5. re-applies ComfyUI-Manager requirements, prints the version summary
 Windows PowerShell 5.1 compatible.
   powershell -ExecutionPolicy Bypass -File Update_ComfyUI.ps1 [-Stable] [-Root <COMFYUI>] [-Port 8188]
#>
param(
    [switch]$Stable,
    [string]$Root = "",
    [int]$Port = 8188
)
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Definition
if (-not $Root) { $Root = Join-Path (Resolve-Path (Join-Path $Here "..")).Path "COMFYUI" }
$Py     = Join-Path $Root "python_embeded\python.exe"
$Comfy  = Join-Path $Root "ComfyUI"
$Upd    = Join-Path $Root "update"
if (-not (Test-Path $Py) -or -not (Test-Path (Join-Path $Comfy "main.py"))) { throw "No ComfyUI portable in $Root" }
if (-not (Test-Path (Join-Path $Upd "update.py"))) { throw "update\update.py not found in $Root - cannot update the source" }

Write-Host "`n[1/5] Checks" -ForegroundColor Cyan
$busy = $false
try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $Port); $c.Close(); $busy = $true } catch {}
if ($busy) { throw "ComfyUI is running (port $Port). Stop it in the launcher first." }
$running = Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$Root*" }
if ($running) { throw "A python.exe from $Root is still running. Stop it first." }

Write-Host "`n[2/5] Backup" -ForegroundColor Cyan
$Bak = Join-Path $Root ("_backups\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $Bak | Out-Null
$freeze = & $Py -s -m pip freeze
$freeze | Set-Content -Encoding ASCII (Join-Path $Bak "pip-freeze.txt")
foreach ($f in "config\extra_model_paths.yaml", "ComfyUI\extra_model_paths.yaml", "ComfyUI\user\default\comfy.settings.json") {
    $src = Join-Path $Root $f
    if (Test-Path $src) { Copy-Item -LiteralPath $src -Destination (Join-Path $Bak ($f -replace "[\\/]", "_")) -Force }
}
$ver = Join-Path $Comfy "comfyui_version.py"
if (Test-Path $ver) { Copy-Item -LiteralPath $ver -Destination (Join-Path $Bak "comfyui_version.py") -Force }
$head = Join-Path $Comfy ".git\HEAD"
if (Test-Path $head) { Copy-Item -LiteralPath $head -Destination (Join-Path $Bak "git-HEAD.txt") -Force }
Write-Host "Backup: $Bak"

Write-Host "`n[3/5] Constraints (keep the CUDA stack)" -ForegroundColor Cyan
$Con = Join-Path $Bak "constraints.txt"
$freeze | Where-Object { $_ -match "^(torch|torchvision|torchaudio|xformers|sageattention|triton|triton-windows)==" } | Set-Content -Encoding ASCII $Con
Get-Content $Con | ForEach-Object { Write-Host "  pin $_" }

Write-Host "`n[4/5] Update ComfyUI source ($(if ($Stable) {'stable release'} else {'latest'}))" -ForegroundColor Cyan
Push-Location $Upd
try {
    $args1 = @(".\update.py", "..\ComfyUI\")
    if ($Stable) { $args1 += "--stable" }
    & $Py -s @args1
    if ($LASTEXITCODE -ne 0) { throw "update.py failed (exit $LASTEXITCODE) - nothing else was changed, backup: $Bak" }
    if (Test-Path ".\update_new.py") {          # the updater can ship a newer copy of itself
        Move-Item -Force ".\update_new.py" ".\update.py"
        Write-Host "Updater replaced itself, running again ..."
        & $Py -s @args1
        if ($LASTEXITCODE -ne 0) { throw "update.py (2nd run) failed (exit $LASTEXITCODE)" }
    }
} finally { Pop-Location }

Write-Host "`n[5/5] Requirements (torch stays as installed)" -ForegroundColor Cyan
$pipArgs = @("-s", "-m", "pip", "install", "-r", (Join-Path $Comfy "requirements.txt"), "-c", $Con, "--no-warn-script-location")
& $Py @pipArgs
if ($LASTEXITCODE -ne 0) { Write-Warning "requirements install reported errors (exit $LASTEXITCODE). To roll back packages: pip install -r $Bak\pip-freeze.txt" }
$man = Join-Path $Comfy "manager_requirements.txt"
if (Test-Path $man) {
    & $Py -s -m pip install -r $man -c $Con --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { Write-Warning "Manager requirements failed" }
}

Write-Host "`nVersions now:" -ForegroundColor Cyan
& $Py -s -m pip freeze | Where-Object { $_ -match "^(torch|torchvision|torchaudio|xformers|sageattention|comfyui)" } | ForEach-Object { Write-Host "  $_" }
if (Test-Path $ver) { Get-Content $ver | Select-String "__version__" | ForEach-Object { Write-Host "  ComfyUI $($_.Line)" } }
Write-Host "`nDone. Backup kept in $Bak" -ForegroundColor Green
