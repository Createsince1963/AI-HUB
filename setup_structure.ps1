# AI Launcher - Struktur-Setup Script
# Dieses Script organisiert die Dateien in die richtige Verzeichnisstruktur

param(
    [string]$LauncherPath = $PSScriptRoot
)

$ErrorActionPreference = "Continue"

Write-Host "📁 Organisiere AI Launcher Verzeichnisstruktur..." -ForegroundColor Cyan

# Erstelle Verzeichnisse
$dirs = @(
    "$LauncherPath\ui",
    "$LauncherPath\utils",
    "$LauncherPath\tools",
    "$LauncherPath\assets",
    "$LauncherPath\assets\icons",
    "$LauncherPath\assets\styles"
)

foreach ($dir in $dirs) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Write-Host "  ✓ Erstellt: $dir" -ForegroundColor Green
    } else {
        Write-Host "  ℹ Existiert: $dir" -ForegroundColor Gray
    }
}

# Verschiebe UI-Dateien
$uiFiles = @("launcher_tab.py", "status_tab.py", "config_tab.py")
foreach ($file in $uiFiles) {
    $src = "$LauncherPath\$file"
    $dst = "$LauncherPath\ui\$file"
    if (Test-Path $src) {
        Move-Item -Path $src -Destination $dst -Force
        Write-Host "  ✓ Verschoben: $file → ui\$file" -ForegroundColor Green
    }
}

# Verschiebe Utils-Dateien
$utilsFiles = @("tool_manager.py", "config.py")
foreach ($file in $utilsFiles) {
    $src = "$LauncherPath\$file"
    $dst = "$LauncherPath\utils\$file"
    if (Test-Path $src) {
        Move-Item -Path $src -Destination $dst -Force
        Write-Host "  ✓ Verschoben: $file → utils\$file" -ForegroundColor Green
    }
}

# Erstelle __init__.py Dateien
$initFiles = @(
    "$LauncherPath\ui\__init__.py",
    "$LauncherPath\utils\__init__.py",
    "$LauncherPath\tools\__init__.py"
)

$initContent = '"""Auto-generated __init__.py"""'

foreach ($file in $initFiles) {
    if (-not (Test-Path $file)) {
        New-Item -ItemType File -Path $file -Force | Out-Null
        Set-Content -Path $file -Value $initContent
        Write-Host "  ✓ Erstellt: $(Split-Path $file -Leaf)" -ForegroundColor Green
    }
}

Write-Host "`n✓ Verzeichnisstruktur eingerichtet!" -ForegroundColor Green
Write-Host "`nNächste Schritte:" -ForegroundColor Cyan
Write-Host "  1. Öffne PowerShell/CMD im AI_Launcher Verzeichnis"
Write-Host "  2. Führe aus: .\run.ps1"
Write-Host "  3. Oder Windows: run.bat"
