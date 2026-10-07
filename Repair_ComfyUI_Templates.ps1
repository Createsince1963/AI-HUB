<#
 Repairs the ComfyUI workflow template packages of the portable workspace.
 Symptom: the "Templates" browser in the ComfyUI GUI is empty or incomplete because one of the
 comfyui-workflow-templates-* sub packages (media-video / media-image / media-other, ...) is missing
 or has the wrong version.
   1. refuses to run while ComfyUI is running
   2. reads the required version from ComfyUI\requirements.txt (comfyui-workflow-templates==X)
   3. removes zero-byte *.whl leftovers of aborted downloads in site-packages
   4. force-reinstalls the meta package WITH its pinned dependencies (torch and the CUDA stack are not touched)
   5. verifies every Requires-Dist of the meta package, runs pip check
 Windows PowerShell 5.1 compatible.
   powershell -ExecutionPolicy Bypass -File Repair_ComfyUI_Templates.ps1 [-Root <COMFYUI>] [-Port 8188] [-CheckOnly]
#>
param(
    [string]$Root = "",
    [int]$Port = 8188,
    [switch]$CheckOnly
)
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Definition
if (-not $Root) { $Root = Join-Path (Resolve-Path (Join-Path $Here "..")).Path "COMFYUI" }
$Py    = Join-Path $Root "python_embeded\python.exe"
$Comfy = Join-Path $Root "ComfyUI"
$Site  = Join-Path $Root "python_embeded\Lib\site-packages"
if (-not (Test-Path $Py) -or -not (Test-Path (Join-Path $Comfy "main.py"))) { throw "No ComfyUI portable in $Root" }

function Get-TemplateStatus {
    # Returns the list of missing / wrong-version template packages (empty list = OK).
    $problems = @()
    $meta = Get-ChildItem -Path $Site -Directory -Filter "comfyui_workflow_templates-*.dist-info" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $meta) { return @("comfyui-workflow-templates (meta package not installed)") }
    $req = Get-Content (Join-Path $meta.FullName "METADATA") | Where-Object { $_ -match "^Requires-Dist: " -and $_ -notmatch "extra ==" }
    foreach ($line in $req) {
        if ($line -match "^Requires-Dist:\s*([A-Za-z0-9_.-]+)==([^\s;]+)") {
            $name = $Matches[1]; $ver = $Matches[2]
            $norm = ($name -replace "[-.]", "_").ToLower()
            $hit = Get-ChildItem -Path $Site -Directory -Filter "$norm-*.dist-info" -ErrorAction SilentlyContinue |
                   Where-Object { $_.Name.ToLower() -eq "$norm-$ver.dist-info" }
            if (-not $hit) { $problems += "$name==$ver" }
        }
    }
    return $problems
}

Write-Host "`n[1/5] Checks" -ForegroundColor Cyan
$busy = $false
try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $Port); $c.Close(); $busy = $true } catch {}
if ($busy) { throw "ComfyUI is running (port $Port). Stop it in the launcher first." }

Write-Host "`n[2/5] Current state" -ForegroundColor Cyan
$before = @(Get-TemplateStatus)
if ($before.Count -eq 0) { Write-Host "All template packages are installed with the required versions." -ForegroundColor Green }
else { $before | ForEach-Object { Write-Warning "missing / wrong: $_" } }
if ($CheckOnly) { if ($before.Count -eq 0) { exit 0 } else { exit 1 } }

$reqFile = Join-Path $Comfy "requirements.txt"
$pin = Select-String -Path $reqFile -Pattern "^comfyui-workflow-templates==" | Select-Object -First 1
if (-not $pin) { throw "comfyui-workflow-templates pin not found in $reqFile" }
$spec = $pin.Line.Trim()

Write-Host "`n[3/5] Clean up aborted downloads" -ForegroundColor Cyan
Get-ChildItem -Path $Site -Filter "*.whl" -File -ErrorAction SilentlyContinue | Where-Object { $_.Length -eq 0 } | ForEach-Object {
    Write-Host "  remove empty $($_.Name)"
    Remove-Item -LiteralPath $_.FullName -Force
}

Write-Host "`n[4/5] Reinstall $spec (with pinned sub packages)" -ForegroundColor Cyan
& $Py -s -m pip install --force-reinstall $spec --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE). Check network / disk space and run again." }

Write-Host "`n[5/5] Verify" -ForegroundColor Cyan
$after = @(Get-TemplateStatus)
if ($after.Count -gt 0) { $after | ForEach-Object { Write-Warning "still missing: $_" }; throw "Template packages are still incomplete." }
& $Py -s -m pip check
& $Py -s -m pip freeze | Where-Object { $_ -match "^comfyui[-_](workflow|frontend)" } | ForEach-Object { Write-Host "  $_" }
Write-Host "`nDone. Start ComfyUI and reload the page with Ctrl+F5." -ForegroundColor Green
