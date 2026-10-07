#Requires -Version 5.1
<#
.SYNOPSIS
    AI Toolchain Launcher v2.0 - Professional UI for Shell + Python Integration

.DESCRIPTION
    Multi-mode launcher for Ollama, Python, and custom AI tools with real-time output

.NOTES
    Thomas' Portable AI Setup - RTX 5070 + Qwen3.8 optimized
    Requires: PowerShell 5.1+, .NET Framework 4.5+

.EXAMPLE
    .\AI-Launcher-Main.ps1
#>

param(
    [switch]$NoUI,
    [string]$ConfigPath = "$PSScriptRoot\launcher-config.json"
)

# ============================================
# INITIALIZATION
# ============================================

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# Load assemblies
Add-Type -AssemblyName PresentationFramework -ErrorAction SilentlyContinue
Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue

$Script:LauncherVersion = "2.0"
$Script:ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script:LogPath = "$Script:ScriptRoot\logs"
$Script:DataPath = "$Script:ScriptRoot\data"

# Create directories
@($Script:LogPath, $Script:DataPath) | ForEach-Object {
    if (-not (Test-Path $_)) {
        New-Item -ItemType Directory -Path $_ -Force | Out-Null
    }
}

# ============================================
# CONFIGURATION LOADER
# ============================================

function Initialize-Config {
    if (-not (Test-Path $ConfigPath)) {
        Write-Warning "Config not found. Creating default..."
        New-DefaultConfig | Out-File $ConfigPath -Encoding UTF8
    }

    $config = Get-Content $ConfigPath | ConvertFrom-Json
    return $config
}

function New-DefaultConfig {
    @{
        version = "2.0"
        launcher = @{
            theme = "dark"
            width = 900
            height = 700
            logCommands = $true
            autoRefresh = $false
        }
        environment = @{
            ollamaPath = "C:\Users\$env:USERNAME\AppData\Local\Programs\Ollama"
            pythonPath = "python"
            cudaPath = "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4"
            dockerEnabled = $false
        }
        models = @(
            @{
                name = "Qwen3.8-27B"
                source = "Luis23333/Qwen3.8-27B-SSMFIX-UD-Q3_K_XL-GGUF"
                type = "ollama"
                quantization = "UD-Q3_K_XL"
                estimatedSize = "13.4 GB"
                gpuLayers = 15
                contextSize = 4096
                recommended = $true
            }
            @{
                name = "TinyLlama (Test)"
                source = "heybails/tinyllama"
                type = "ollama"
                quantization = "Q4_0"
                estimatedSize = "636 MB"
                gpuLayers = 10
                contextSize = 2048
                recommended = $false
            }
        )
        categories = @{
            Shell = @{
                icon = "🖥️"
                color = "#007acc"
            }
            Python = @{
                icon = "🐍"
                color = "#3776ab"
            }
        }
    } | ConvertTo-Json -Depth 10
}

# Load config
$Script:Config = Initialize-Config

# ============================================
# LOGGING SYSTEM
# ============================================

function Write-Log {
    param(
        [string]$Message,
        [ValidateSet("Info", "Warning", "Error", "Success")]
        [string]$Level = "Info"
    )

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logEntry = "[$timestamp] [$Level] $Message"

    $logFile = "$Script:LogPath\launcher_$(Get-Date -Format 'yyyyMMdd').log"
    Add-Content -Path $logFile -Value $logEntry -ErrorAction SilentlyContinue

    switch ($Level) {
        "Success" { Write-Host "✓ $Message" -ForegroundColor Green }
        "Warning" { Write-Host "⚠ $Message" -ForegroundColor Yellow }
        "Error" { Write-Host "✗ $Message" -ForegroundColor Red }
        default { Write-Host "▶ $Message" -ForegroundColor Cyan }
    }
}

# ============================================
# SHELL COMMANDS REGISTRY
# ============================================

function Get-ShellCommands {
    return @{
        "Ollama Management" = @{
            "Pull Qwen3.8-27B (Production)" = {
                $env:Path += ";$($Script:Config.environment.ollamaPath)"
                Write-Log "Pulling Qwen3.8-27B-SSMFIX-UD-Q3_K_XL..." "Info"
                ollama pull Luis23333/Qwen3.8-27B-SSMFIX-UD-Q3_K_XL-GGUF
                Write-Log "Qwen3.8-27B downloaded successfully" "Success"
            }
            "Pull TinyLlama (Test Model)" = {
                $env:Path += ";$($Script:Config.environment.ollamaPath)"
                Write-Log "Pulling TinyLlama..." "Info"
                ollama pull heybails/tinyllama
                Write-Log "TinyLlama downloaded" "Success"
            }
            "List All Models" = {
                $env:Path += ";$($Script:Config.environment.ollamaPath)"
                Write-Log "Fetching installed models..." "Info"
                Write-Host "`n=== Downloaded Models ===" -ForegroundColor Cyan
                ollama list
            }
            "Show Model Details" = {
                $env:Path += ";$($Script:Config.environment.ollamaPath)"
                $model = "Luis23333/Qwen3.8-27B-SSMFIX-UD-Q3_K_XL-GGUF"
                Write-Host "`n=== Model: $model ===" -ForegroundColor Cyan
                ollama show $model --verbose 2>$null | Select-Object -First 30
            }
            "Benchmark Performance" = {
                param([int]$SubtabValue = 3)

                $env:Path += ";$($Script:Config.environment.ollamaPath)"
                $iterations = if ($SubtabValue) { $SubtabValue } else { 3 }

                Write-Host "`n=== Ollama Performance Benchmark ===" -ForegroundColor Cyan
                Write-Host "Model: Qwen3.8-27B-SSMFIX-UD-Q3_K_XL" -ForegroundColor Yellow
                Write-Host "Iterations: $iterations | GPU Layers: 15 | Context: 4096`n" -ForegroundColor Yellow

                $prompt = "Describe the architecture of a Plant3D BOM export system in 50 words."
                $times = @()

                for ($i = 1; $i -le $iterations; $i++) {
                    Write-Host "Run $i/$iterations..." -ForegroundColor Green -NoNewline
                    $time = Measure-Command {
                        ollama generate Luis23333/Qwen3.8-27B-SSMFIX-UD-Q3_K_XL-GGUF $prompt 2>$null | Out-Null
                    }
                    $times += $time.TotalSeconds
                    Write-Host " $([math]::Round($time.TotalSeconds, 2))s" -ForegroundColor Cyan
                }

                $avg = ($times | Measure-Object -Average).Average
                Write-Host "`n✓ Average: $([math]::Round($avg, 2))s" -ForegroundColor Green

                if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
                    Write-Host "`n=== GPU Memory ===" -ForegroundColor Cyan
                    nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits
                }
            }
            "Start Ollama Service" = {
                $env:Path += ";$($Script:Config.environment.ollamaPath)"
                if (-not (Get-Process ollama -ErrorAction SilentlyContinue)) {
                    Write-Log "Starting Ollama..." "Info"
                    Start-Process ollama -NoNewWindow -RedirectStandardOutput "$Script:LogPath\ollama.log"
                    Start-Sleep -Seconds 3
                    Write-Log "Ollama started (PID: $(Get-Process ollama).Id)" "Success"
                } else {
                    Write-Log "Ollama already running" "Warning"
                }
            }
            "Stop Ollama Service" = {
                Get-Process ollama -ErrorAction SilentlyContinue | Stop-Process -Force
                Write-Log "Ollama stopped" "Success"
            }
        }

        "Environment Setup" = @{
            "Check System Requirements" = {
                Write-Host "`n=== System Requirements Check ===" -ForegroundColor Cyan

                # Ollama
                Write-Host "`n1. Ollama Installation:" -ForegroundColor Yellow
                if (Test-Path $Script:Config.environment.ollamaPath) {
                    Write-Host "   ✓ Found at: $($Script:Config.environment.ollamaPath)" -ForegroundColor Green
                } else {
                    Write-Host "   ✗ Not found. Download: https://ollama.ai" -ForegroundColor Red
                }

                # Python
                Write-Host "`n2. Python Installation:" -ForegroundColor Yellow
                if (Get-Command python -ErrorAction SilentlyContinue) {
                    $pyVersion = python --version 2>&1
                    Write-Host "   ✓ $pyVersion" -ForegroundColor Green
                } else {
                    Write-Host "   ✗ Python not found" -ForegroundColor Red
                }

                # NVIDIA GPU
                Write-Host "`n3. NVIDIA GPU:" -ForegroundColor Yellow
                if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
                    $gpuInfo = nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
                    Write-Host "   ✓ $gpuInfo" -ForegroundColor Green
                } else {
                    Write-Host "   ✗ NVIDIA drivers not found" -ForegroundColor Red
                }

                # Docker
                Write-Host "`n4. Docker:" -ForegroundColor Yellow
                if (Get-Command docker -ErrorAction SilentlyContinue) {
                    Write-Host "   ✓ Docker installed" -ForegroundColor Green
                } else {
                    Write-Host "   ○ Optional (for OpenWebUI)" -ForegroundColor Yellow
                }
            }
            "Add Ollama to PATH (Permanent)" = {
                Write-Log "Adding Ollama to system PATH..." "Info"
                $currentPath = [Environment]::GetEnvironmentVariable('Path', 'User')
                if ($currentPath -notmatch [regex]::Escape($Script:Config.environment.ollamaPath)) {
                    [Environment]::SetEnvironmentVariable(
                        'Path',
                        "$currentPath;$($Script:Config.environment.ollamaPath)",
                        'User'
                    )
                    Write-Log "Ollama added to PATH. Restart PowerShell." "Success"
                } else {
                    Write-Log "Ollama already in PATH" "Warning"
                }
            }
            "View Configuration" = {
                Write-Host "`n=== Launcher Configuration ===" -ForegroundColor Cyan
                $Script:Config | ConvertTo-Json | Write-Host
            }
        }
    }
}

# ============================================
# CREATOR DETECTION SYSTEM
# ============================================

function Initialize-CreatorRegistry {
    $registry = @{}

    foreach ($creator in $Script:Config.creators) {
        foreach ($alias in $creator.aliases) {
            $registry[$alias.ToLower()] = $creator
        }
        foreach ($pattern in $creator.patterns) {
            $registry[$pattern.ToLower()] = $creator
        }
    }

    return $registry
}

function Detect-Creator {
    param(
        [string]$CommandName,
        [string]$CommandSource = "",
        [string]$Category = ""
    )

    $creatorRegistry = Initialize-CreatorRegistry

    # Priority 1: Exact pattern match in command name
    foreach ($key in $creatorRegistry.Keys) {
        if ($CommandName -match $key) {
            return $creatorRegistry[$key]
        }
    }

    # Priority 2: Pattern match in source/category
    foreach ($key in $creatorRegistry.Keys) {
        if ($CommandSource -match $key -or $Category -match $key) {
            return $creatorRegistry[$key]
        }
    }

    # Priority 3: Return default "Community" creator (don't show dialog - prevents mini windows at startup)
    return @{ name = "Community"; displayName = "Community"; color = "#808080"; aliases = @("community"); patterns = @() }
}

function Show-CreatorSelectionDialog {
    # DISABLED: This function caused multiple mini windows at startup
    # Detect-Creator now returns "Community" as default (see line 319)
    # This is kept as a no-op stub for backward compatibility
    return @{ name = "Community"; displayName = "Community"; color = "#808080"; aliases = @("community"); patterns = @() }

    return @{ name = "Unknown"; color = "#9E9E9E" }
}

# ============================================
# COMMAND METADATA WITH CREATOR
# ============================================

function Get-CommandMetadata {
    param(
        [string]$Command,
        [string]$Category,
        [string]$Mode,
        [string]$Source = ""
    )

    $creator = Detect-Creator -CommandName $Command -CommandSource $Source -Category $Category

    $metadata = @{
        command = $Command
        category = $Category
        creator = $creator
        source = $Source
        subtabs = @()
    }

    # Define subtabs for specific commands (like P3D Data Manager)
    $subtabDefinitions = @{
        "Benchmark Performance" = @(
            @{ Name = "Quick (1x)"; Value = 1; Description = "Single run" }
            @{ Name = "Standard (3x)"; Value = 3; Description = "Recommended" }
            @{ Name = "Extended (5x)"; Value = 5; Description = "Detailed analysis" }
        )
        "Pull Qwen3.8-27B (Production)" = @(
            @{ Name = "Q4_K_M (17.1 GB)"; Value = "q4_k_m"; Description = "Balanced" }
            @{ Name = "Q3_K_M (13.8 GB)"; Value = "q3_k_m"; Description = "Smaller" }
            @{ Name = "UD-Q3_K_XL (13.4 GB)"; Value = "ud_q3_k_xl"; Description = "Optimized" }
        )
        "List HuggingFace Models" = @(
            @{ Name = "Qwen Models"; Value = "qwen"; Description = "Qwen only" }
            @{ Name = "Mixtral"; Value = "mixtral"; Description = "MoE models" }
            @{ Name = "All Categories"; Value = "all"; Description = "Full list" }
        )
        "Run Inference Benchmark (3 iterations)" = @(
            @{ Name = "Qwen3.5-2B (Fast)"; Value = "qwen35_2b"; Description = "5.3 GB" }
            @{ Name = "TinyLlama (Test)"; Value = "tinyllama"; Description = "2 GB" }
            @{ Name = "Qwen3.8-27B (Heavy)"; Value = "qwen38_27b"; Description = "13.4 GB" }
        )
        "Pull TinyLlama (Test Model)" = @(
            @{ Name = "Q4_0 (Standard)"; Value = "q4_0"; Description = "Default" }
            @{ Name = "Q5_K_M (Higher Quality)"; Value = "q5_k_m"; Description = "Larger" }
            @{ Name = "Latest Build"; Value = "latest"; Description = "Auto-detect" }
        )
    }

    if ($subtabDefinitions.ContainsKey($Command)) {
        $metadata.subtabs = $subtabDefinitions[$Command]
    }

    return $metadata
}

# ============================================
# CONTEXT MENU WITH DYNAMIC SUBTABS
# ============================================

function Show-SubtabContextMenu {
    param(
        [array]$Subtabs,
        [string]$Command,
        [string]$Category,
        [System.Windows.Point]$Position
    )

    # Create context menu
    $contextMenu = New-Object System.Windows.Controls.ContextMenu

    foreach ($subtab in $Subtabs) {
        $menuItem = New-Object System.Windows.Controls.MenuItem
        $menuItem.Header = "$($subtab.Name) — $($subtab.Description)"
        $menuItem.Tag = @{
            Command = $Command
            Category = $Category
            Value = $subtab.Value
            Subtab = $subtab.Name
        }

        # Add click handler
        $menuItem.Add_Click({
            param($sender, $e)
            $tag = $sender.Tag

            Write-ColorOutput "Executing: $($tag.Command) [$($tag.Subtab)]" "Info"

            # Execute with subtab parameter
            Invoke-CommandExecutionWithSubtab `
                -Command $tag.Command `
                -Category $tag.Category `
                -Mode $Script:CurrentMode `
                -SubtabValue $tag.Value

            # Update UI (need to pass controls reference)
            # This would need refactoring to access $controls
        })

        $contextMenu.Items.Add($menuItem) | Out-Null
    }

    # Add separator
    $contextMenu.Items.Add([System.Windows.Controls.Separator]::new()) | Out-Null

    # Add "Execute Default" option
    $defaultItem = New-Object System.Windows.Controls.MenuItem
    $defaultItem.Header = "⏎ Execute Default"
    $defaultItem.Tag = @{
        Command = $Command
        Category = $Category
    }

    $defaultItem.Add_Click({
        param($sender, $e)
        $tag = $sender.Tag
        Write-ColorOutput "Executing (default): $($tag.Command)" "Info"
        Invoke-CommandExecution -Command $tag.Command -Category $tag.Category -Mode $Script:CurrentMode
    })

    $contextMenu.Items.Add($defaultItem) | Out-Null

    # Show menu
    $contextMenu.IsOpen = $true
}

function Invoke-CommandExecutionWithSubtab {
    param(
        [string]$Command,
        [string]$Category,
        [string]$Mode,
        $SubtabValue
    )

    $Script:OutputText = "▶ Executing: $Command [Subtab: $SubtabValue]`n" + ("=" * 70) + "`n`n"

    try {
        if ($Mode -eq "Shell") {
            $scriptBlock = (Get-ShellCommands)[$Category][$Command]
        } else {
            $scriptBlock = (Get-PythonCommands)[$Category][$Command]
        }

        if ($scriptBlock) {
            # Pass subtab value as parameter if script block accepts it
            $output = & $scriptBlock -SubtabValue $SubtabValue 2>&1 | Out-String
            $Script:OutputText += $output
            Write-Log "Executed: $Command (Subtab: $SubtabValue)" "Success"
        }
    } catch {
        $Script:OutputText += "`n✗ ERROR: $_`n$($_.Exception.Message)"
        Write-Log "Command failed: $_" "Error"
    }
}

# ============================================
# PYTHON COMMANDS REGISTRY
# ============================================

function Get-PythonCommands {
    return @{
        "Model Management" = @{
            "Download via HuggingFace CLI" = {
                Write-Log "Checking HuggingFace CLI..." "Info"
                $hfInstalled = python -c "import huggingface_hub" 2>&1

                if ($LASTEXITCODE -ne 0) {
                    Write-Log "Installing huggingface-hub..." "Warning"
                    python -m pip install -q huggingface-hub
                }

                Write-Host "`n=== Downloading Qwen3.8-27B ===" -ForegroundColor Cyan
                python -m huggingface_hub download `
                    "Luis23333/Qwen3.8-27B-SSMFIX-UD-Q3_K_XL-GGUF" `
                    --local-dir "$Script:DataPath\Qwen3.8-SSMFIX" `
                    --include "*UD-Q3_K_XL*"

                Write-Log "Download complete" "Success"
            }
            "List HuggingFace Models" = {
                python -c @"
import huggingface_hub
print("\n=== Available Qwen3.8 Quantizations ===")
api = huggingface_hub.HfApi()
models = api.repo_info("Luis23333/Qwen3.8-27B-SSMFIX-UD-Q3_K_XL-GGUF", files_metadata=True)
print(f"Last updated: {models.last_modified}")
print(f"Repo size: {models.siblings}")
"@
            }
        }

        "Benchmarking" = @{
            "Run Inference Benchmark (3 iterations)" = {
                Write-Host "`n=== Model Inference Benchmark ===" -ForegroundColor Cyan
                python -c @"
import subprocess
import time
import json
import os
from datetime import datetime

os.environ['PATH'] += f";$($Script:Config.environment.ollamaPath)"

model = "Luis23333/Qwen3.8-27B-SSMFIX-UD-Q3_K_XL-GGUF"
prompt = "Describe the key components in a Plant3D BOM export system."

results = {
    'timestamp': datetime.now().isoformat(),
    'model': model,
    'iterations': []
}

for i in range(1, 4):
    print(f"\nIteration {i}/3...", flush=True)
    start = time.time()

    try:
        subprocess.run(
            ['ollama', 'generate', model, prompt],
            capture_output=True,
            timeout=300
        )
        elapsed = time.time() - start
        results['iterations'].append({
            'num': i,
            'time': round(elapsed, 2)
        })
        print(f"✓ Completed in {elapsed:.2f}s")
    except Exception as e:
        print(f"✗ Error: {e}")

if results['iterations']:
    avg = sum(r['time'] for r in results['iterations']) / len(results['iterations'])
    print(f"\n=== Results ===")
    print(f"Average time: {avg:.2f}s")
    print(f"Tokens/sec: {int(100 / avg)}")  # Rough estimate

    # Save results
    os.makedirs("$Script:LogPath", exist_ok=True)
    with open("$Script:LogPath/benchmark_$($timestamp).json", 'w') as f:
        json.dump(results, f, indent=2)
    print(f"\n✓ Results saved to logs")
"@
            }
            "GPU Memory Profile" = {
                python -c @"
import subprocess
import json

print("\n=== GPU Memory Profile ===\n")

# Get GPU info
gpu_info = subprocess.run(
    ['nvidia-smi', '--query-gpu=index,name,memory.total,memory.free,memory.used,utilization.gpu'],
    capture_output=True,
    text=True
).stdout

print(gpu_info)

# Memory for models
print("\n=== Model VRAM Requirements ===")
models = {
    'Qwen3.8-27B-UD-Q3_K_XL': 13.4,
    'Qwen3.8-27B-UD-Q4_K_M': 17.1,
    'TinyLlama-Q4': 2.0,
    'Mixtral-8x7B-Q4': 25.0
}

for model, size in models.items():
    print(f"{model:35} {size:6.1f} GB")
"@
            }
        }

        "Development" = @{
            "Install Dependencies" = {
                Write-Log "Installing Python dependencies..." "Info"
                python -m pip install -q `
                    ollama `
                    huggingface-hub `
                    requests `
                    pyyaml `
                    python-dotenv
                Write-Log "Dependencies installed" "Success"
            }
            "Test Python + Ollama Integration" = {
                python -c @"
import sys
import subprocess

print("=== Integration Test ===\n")

# Test 1: Python version
print(f"1. Python: {sys.version.split()[0]}")

# Test 2: Ollama accessibility
try:
    result = subprocess.run(['ollama', '--version'], capture_output=True, text=True)
    print(f"2. Ollama: {result.stdout.strip()}")
except FileNotFoundError:
    print("2. Ollama: NOT FOUND")

# Test 3: Common libraries
libs = ['requests', 'yaml', 'dotenv']
for lib in libs:
    try:
        __import__(lib.replace('-', '_'))
        print(f"3. {lib}: ✓")
    except ImportError:
        print(f"3. {lib}: ✗")

print("\n✓ Integration test complete")
"@
            }
        }
    }
}

# ============================================
# UI BUILDER
# ============================================

function New-LauncherUI {
    [xml]$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="AI Toolchain Launcher v$($Script:LauncherVersion)"
        Height="700" Width="900"
        Background="#1e1e1e"
        Foreground="#e0e0e0"
        WindowStartupLocation="CenterScreen"
        Icon="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==">

    <Window.Resources>
        <Style TargetType="Button">
            <Setter Property="Padding" Value="12,8"/>
            <Setter Property="Margin" Value="0,5"/>
            <Setter Property="FontSize" Value="12"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="BorderThickness" Value="0"/>
            <Setter Property="Foreground" Value="White"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border Background="{TemplateBinding Background}"
                                CornerRadius="4"
                                Padding="{TemplateBinding Padding}">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style TargetType="ListBox">
            <Setter Property="Background" Value="#2d2d30"/>
            <Setter Property="Foreground" Value="#cccccc"/>
            <Setter Property="BorderBrush" Value="#3e3e42"/>
            <Setter Property="BorderThickness" Value="1"/>
        </Style>
    </Window.Resources>

    <Grid>
        <Grid.ColumnDefinitions>
            <ColumnDefinition Width="250"/>
            <ColumnDefinition Width="*"/>
        </Grid.ColumnDefinitions>

        <!-- Sidebar -->
        <StackPanel Grid.Column="0" Background="#252526" Padding="15">
            <TextBlock Text="AI LAUNCHER" FontWeight="Bold" FontSize="14"
                      Foreground="#00d4ff" Margin="0,0,0,20"/>

            <!-- Mode Buttons -->
            <Button Name="BtnShell" Content="🖥️  Shell Commands"
                   Background="#007acc" FontWeight="Bold"/>
            <Button Name="BtnPython" Content="🐍 Python Scripts"
                   Background="#3776ab" FontWeight="Bold"/>

            <Separator Margin="0,15" Background="#3e3e42" Height="1"/>

            <!-- Creator Filter -->
            <TextBlock Text="Filter by Creator" FontWeight="Bold" FontSize="11"
                      Foreground="#00d4ff" Margin="0,10,0,8"/>
            <ComboBox Name="CreatorFilter" Padding="8,6" Margin="0,0,0,10"
                     Background="#2d2d30" Foreground="#cccccc" FontSize="11">
                <ComboBoxItem IsSelected="True">All Creators</ComboBoxItem>
                <ComboBoxItem>Qwen</ComboBoxItem>
                <ComboBoxItem>Mistral</ComboBoxItem>
                <ComboBoxItem>Meta (Llama)</ComboBoxItem>
                <ComboBoxItem>Nous Research</ComboBoxItem>
                <ComboBoxItem>Microsoft (Phi)</ComboBoxItem>
                <ComboBoxItem>DeepSeek</ComboBoxItem>
                <ComboBoxItem>Unsloth</ComboBoxItem>
                <ComboBoxItem>Community</ComboBoxItem>
            </ComboBox>

            <!-- Category List -->
            <TextBlock Text="Categories" FontWeight="Bold" FontSize="11"
                      Foreground="#00d4ff" Margin="0,10,0,8"/>
            <ListBox Name="CategoryList" MaxHeight="250" Padding="5"/>

            <!-- Quick Actions -->
            <Separator Margin="0,15" Background="#3e3e42" Height="1"/>
            <TextBlock Text="Actions" FontWeight="Bold" FontSize="11"
                      Foreground="#00d4ff" Margin="0,10,0,8"/>

            <Button Name="BtnRefresh" Content="🔄 Refresh" Background="#464647"/>
            <Button Name="BtnOpenLogs" Content="📋 Logs" Background="#464647"/>
            <Button Name="BtnConfig" Content="⚙️ Config" Background="#464647"/>
            <Button Name="BtnExit" Content="❌ Exit" Background="#464647" Margin="0,30,0,0"/>
        </StackPanel>

        <!-- Main Content -->
        <Grid Grid.Column="1" Padding="20">
            <Grid.RowDefinitions>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="*"/>
                <RowDefinition Height="180"/>
            </Grid.RowDefinitions>

            <!-- Header Info -->
            <StackPanel Grid.Row="0" Margin="0,0,0,15">
                <TextBlock Name="HeaderTitle" FontSize="16" FontWeight="Bold"
                          Foreground="#00d4ff" Margin="0,0,0,5"/>
                <TextBlock Name="HeaderDesc" FontSize="11" Foreground="#999999"/>
            </StackPanel>

            <!-- Commands List -->
            <ListBox Name="CommandList" Grid.Row="1" Padding="10" Margin="0,0,0,10"/>

            <!-- Output Console -->
            <Border Grid.Row="2" Background="#1e1e1e" BorderThickness="1"
                   BorderBrush="#3e3e42" CornerRadius="4" Padding="10">
                <ScrollViewer>
                    <TextBlock Name="OutputConsole" TextWrapping="Wrap"
                              Foreground="#00ff00" FontFamily="Consolas" FontSize="10"
                              VerticalAlignment="Top"/>
                </ScrollViewer>
            </Border>
        </Grid>
    </Grid>
</Window>
"@

    return $xaml
}

# ============================================
# EVENT HANDLERS
# ============================================

function Invoke-CommandExecution {
    param(
        [string]$Command,
        [string]$Category,
        [string]$Mode
    )

    try {
        $Script:OutputText = "▶ Executing: $Command`n" + ("=" * 70) + "`n`n"

        if ($Mode -eq "Shell") {
            $scriptBlock = (Get-ShellCommands)[$Category][$Command]
        } else {
            $scriptBlock = (Get-PythonCommands)[$Category][$Command]
        }

        if ($scriptBlock) {
            $output = & $scriptBlock 2>&1 | Out-String
            $Script:OutputText += $output

            # Log
            Write-Log "Executed: $Command" "Success"
        } else {
            $Script:OutputText += "✗ Command not found"
        }
    } catch {
        $Script:OutputText += "`n✗ ERROR: $_`n$($_.Exception.Message)"
        Write-Log "Command failed: $_" "Error"
    }
}

# ============================================
# MAIN APPLICATION LOGIC
# ============================================

function Show-Launcher {
    $xaml = New-LauncherUI
    $reader = [System.Xml.XmlNodeReader]::new($xaml.DocumentElement)

    try {
        $window = [System.Windows.Markup.XamlReader]::Load($reader)
    } catch {
        Write-Log "Failed to load XAML: $_" "Error"
        Write-Host "✗ UI Loading failed: $_"
        throw
    }

    # Get controls with validation
    $controls = @{
        CategoryList = $window.FindName("CategoryList")
        CommandList = $window.FindName("CommandList")
        OutputConsole = $window.FindName("OutputConsole")
        HeaderTitle = $window.FindName("HeaderTitle")
        HeaderDesc = $window.FindName("HeaderDesc")
        CreatorFilter = $window.FindName("CreatorFilter")
        BtnShell = $window.FindName("BtnShell")
        BtnPython = $window.FindName("BtnPython")
        BtnRefresh = $window.FindName("BtnRefresh")
        BtnOpenLogs = $window.FindName("BtnOpenLogs")
        BtnConfig = $window.FindName("BtnConfig")
        BtnExit = $window.FindName("BtnExit")
    }

    # Validate all controls were found
    foreach ($name in $controls.Keys) {
        if ($null -eq $controls[$name]) {
            Write-Log "Control not found: $name" "Error"
            Write-Host "✗ Control missing: $name"
            throw "Required control not found: $name"
        }
    }

    Write-Log "All UI controls loaded successfully" "Info"

    $Script:CurrentMode = "Shell"
    $Script:OutputText = "Ready. Select a category and command to begin.`n"

    # INITIALIZE CACHE FIRST (before setting up event handlers)
    Write-Log "Initializing: Caching commands..." "Info"

    $Script:ShellCommandsCache = $null
    $Script:PythonCommandsCache = $null

    try {
        Write-Log "  Loading Shell Commands..." "Debug"
        $Script:ShellCommandsCache = Get-ShellCommands
        Write-Log "  ✓ Shell cached: $($Script:ShellCommandsCache.Keys.Count) categories" "Info"
    } catch {
        Write-Log "  ✗ Shell cache error: $_" "Error"
        $Script:ShellCommandsCache = @{}
    }

    try {
        Write-Log "  Loading Python Commands..." "Debug"
        $Script:PythonCommandsCache = Get-PythonCommands
        Write-Log "  ✓ Python cached: $($Script:PythonCommandsCache.Keys.Count) categories" "Info"
    } catch {
        Write-Log "  ✗ Python cache error: $_" "Error"
        $Script:PythonCommandsCache = @{}
    }

    # Shell Mode (with error handling) - USE CACHED COMMANDS
    $controls.BtnShell.Add_Click({
        try {
            $Script:CurrentMode = "Shell"
            $controls.HeaderTitle.Text = "🖥️ Shell Commands"
            $controls.HeaderDesc.Text = "Execute PowerShell and system commands"
            $controls.CategoryList.Items.Clear()

            # ⚡ USE CACHED COMMANDS (fast!) - not Get-ShellCommands (slow!)
            if ($Script:ShellCommandsCache -and $Script:ShellCommandsCache.Keys.Count -gt 0) {
                $Script:ShellCommandsCache.Keys | ForEach-Object { $controls.CategoryList.Items.Add($_) }
            } else {
                Write-Log "No shell commands in cache" "Warning"
                $controls.HeaderDesc.Text = "No shell commands available"
            }

            $controls.BtnShell.Background = "#007acc"
            $controls.BtnPython.Background = "#3776ab"
        } catch {
            Write-Log "Shell mode error: $_" "Error"
            $controls.HeaderDesc.Text = "Error loading shell commands"
        }
    })

    # Python Mode (with error handling) - USE CACHED COMMANDS
    $controls.BtnPython.Add_Click({
        try {
            $Script:CurrentMode = "Python"
            $controls.HeaderTitle.Text = "🐍 Python Scripts"
            $controls.HeaderDesc.Text = "Execute Python integration and utilities"
            $controls.CategoryList.Items.Clear()

            # ⚡ USE CACHED COMMANDS (fast!) - not Get-PythonCommands (slow!)
            if ($Script:PythonCommandsCache -and $Script:PythonCommandsCache.Keys.Count -gt 0) {
                $Script:PythonCommandsCache.Keys | ForEach-Object { $controls.CategoryList.Items.Add($_) }
            } else {
                Write-Log "No python commands in cache" "Warning"
                $controls.HeaderDesc.Text = "No python commands available"
            }

            $controls.BtnPython.Background = "#3776ab"
            $controls.BtnShell.Background = "#007acc"
        } catch {
            Write-Log "Python mode error: $_" "Error"
            $controls.HeaderDesc.Text = "Error loading python commands"
        }
    })

    # Creator Filter Selection (with initialization guard) - USE CACHED COMMANDS
    $filterInitialized = $false
    $controls.CreatorFilter.Add_SelectionChanged({
        if (-not $filterInitialized) { return }  # Skip first trigger during init

        # Re-populate categories based on selected creator
        $selectedCreator = $controls.CreatorFilter.SelectedItem
        $controls.CategoryList.Items.Clear()

        # ⚡ USE CACHED COMMANDS (fast!) - not Get-ShellCommands (slow!)
        $commands = if ($Script:CurrentMode -eq "Shell") {
            $Script:ShellCommandsCache
        } else {
            $Script:PythonCommandsCache
        }

        # Filter categories that have commands from selected creator
        $categories = @()
        foreach ($cat in $commands.Keys) {
            foreach ($cmd in $commands[$cat].Keys) {
                $metadata = Get-CommandMetadata -Command $cmd -Category $cat -Mode $Script:CurrentMode
                $creator = $metadata.creator.name

                if ($selectedCreator -eq "All Creators" -or $creator -eq $selectedCreator) {
                    if ($cat -notin $categories) {
                        $categories += $cat
                    }
                }
            }
        }

        $categories | Sort-Object | ForEach-Object { $controls.CategoryList.Items.Add($_) }
        $controls.CommandList.Items.Clear()
    })

    # Mark filter as initialized (allow events now)
    $filterInitialized = $true

    # Category Selection with Creator Detection (with error handling) - USE CACHED COMMANDS
    $controls.CategoryList.Add_SelectionChanged({
        try {
            $category = $controls.CategoryList.SelectedItem
            if ($null -eq $category -or $category -eq "") { return }

            $controls.CommandList.Items.Clear()
            $selectedCreator = $controls.CreatorFilter.SelectedItem

            # ⚡ USE CACHED COMMANDS (fast!) - not Get-ShellCommands (slow!)
            $commands = if ($Script:CurrentMode -eq "Shell") {
                ($Script:ShellCommandsCache)[$category].Keys
            } else {
                ($Script:PythonCommandsCache)[$category].Keys
            }

            if ($null -eq $commands) {
                Write-Log "No commands found for category: $category" "Warning"
                return
            }

            foreach ($cmd in $commands) {
                try {
                    # Detect creator and create display item with prefix
                    $metadata = Get-CommandMetadata -Command $cmd -Category $category -Mode $Script:CurrentMode
                    $creator = $metadata.creator.name

                    # Apply creator filter
                    if ($selectedCreator -ne "All Creators" -and $creator -ne $selectedCreator) {
                        continue
                    }

                    # Create display text with creator prefix
                    $displayText = "[$creator] $cmd"

                    # Add to list with metadata tags
                    $item = $controls.CommandList.Items.Add($displayText)

                    # Store metadata for later use
                    $controls.CommandList.Tag = @{
                        "$displayText" = $metadata
                    }
                } catch {
                    Write-Log "Error processing command $cmd: $_" "Warning"
                    continue
                }
            }
        } catch {
            Write-Log "Category selection error: $_" "Error"
        }
    })

    # Command Execution (Left Click / Double Click) with error handling
    $controls.CommandList.Add_MouseDoubleClick({
        try {
            $command = $controls.CommandList.SelectedItem
            $category = $controls.CategoryList.SelectedItem

            if ($null -eq $command -or $null -eq $category) {
                Write-Log "No command or category selected" "Warning"
                return
            }

            # Remove creator prefix from display text
            $cleanCommand = $command -replace '^\[[^\]]+\]\s+', ''

            Invoke-CommandExecution -Command $cleanCommand -Category $category -Mode $Script:CurrentMode
            $controls.OutputConsole.Text = $Script:OutputText
        } catch {
            Write-Log "Command execution error: $_" "Error"
            $controls.OutputConsole.Text = "✗ Error executing command: $_"
        }
    })

    # Right-Click Context Menu with Dynamic Subtabs (P3D Data Manager style) with error handling
    $controls.CommandList.Add_MouseRightButtonUp({
        param($sender, $e)
        try {
            $command = $controls.CommandList.SelectedItem
            $category = $controls.CategoryList.SelectedItem

            if ($null -eq $command -or $null -eq $category) { return }

            # Remove creator prefix from display text
            $cleanCommand = $command -replace '^\[[^\]]+\]\s+', ''

            # Get command metadata from config
            $commandData = Get-CommandMetadata -Command $cleanCommand -Category $category -Mode $Script:CurrentMode

            if ($commandData.subtabs -and $commandData.subtabs.Count -gt 0) {
                Show-SubtabContextMenu -Subtabs $commandData.subtabs `
                                       -Command $cleanCommand `
                                       -Category $category `
                                       -Position $e.GetPosition($controls.CommandList)
            } else {
                # Fallback: Execute default
                Invoke-CommandExecution -Command $cleanCommand -Category $category -Mode $Script:CurrentMode
                $controls.OutputConsole.Text = $Script:OutputText
            }
        } catch {
            Write-Log "Right-click menu error: $_" "Error"
        }
    })

    # Refresh (with error handling)
    $controls.BtnRefresh.Add_Click({
        try {
            $controls.CategoryList.Items.Refresh()
            $controls.CommandList.Items.Refresh()
            $Script:OutputText = "✓ Refreshed`n"
            $controls.OutputConsole.Text = $Script:OutputText
            Write-Log "Manual refresh triggered" "Info"
        } catch {
            Write-Log "Refresh error: $_" "Error"
            $controls.OutputConsole.Text = "✗ Refresh failed: $_"
        }
    })

    # Open Logs (with error handling)
    $controls.BtnOpenLogs.Add_Click({
        try {
            if (Test-Path $Script:LogPath) {
                Invoke-Item $Script:LogPath
            } else {
                Write-Host "Log directory not found: $Script:LogPath"
            }
        } catch {
            Write-Log "Open logs error: $_" "Error"
        }
    })

    # Config (with error handling)
    $controls.BtnConfig.Add_Click({
        try {
            if (Test-Path $ConfigPath) {
                Invoke-Item $ConfigPath
            } else {
                Write-Host "Config file not found: $ConfigPath"
            }
        } catch {
            Write-Log "Open config error: $_" "Error"
        }
    })

    # Exit (with cleanup)
    $controls.BtnExit.Add_Click({
        try {
            Write-Log "Exit requested by user" "Info"
            $window.Close()
        } catch {
            Write-Log "Exit error: $_" "Error"
            $window.Close()
        }
    })

    # Initialize Shell mode (commands already cached above)
    # Direct invocation instead of RaiseEvent (PowerShell 5.1 compatibility)
    $Script:CurrentMode = "Shell"
    $controls.HeaderTitle.Text = "🖥️ Shell Commands"
    $controls.HeaderDesc.Text = "Execute PowerShell and system commands"
    $controls.CategoryList.Items.Clear()

    # Use cached commands instead of calling Get-ShellCommands (which is slow!)
    $Script:ShellCommandsCache.Keys | ForEach-Object { $controls.CategoryList.Items.Add($_) }
    $controls.BtnShell.Background = "#007acc"
    $controls.BtnPython.Background = "#3776ab"
    $controls.OutputConsole.Text = $Script:OutputText

    Write-Log "Initialization complete (UI should now be responsive)" "Info"

    # Show window
    $window.ShowDialog() | Out-Null
}

# ============================================
# ENTRY POINT
# ============================================

Write-Log "AI Launcher v$Script:LauncherVersion starting..." "Info"
Write-Log "Working directory: $Script:ScriptRoot" "Info"

if ($NoUI) {
    Write-Host "No UI mode - use: Get-ShellCommands | Get-PythonCommands"
    Write-Log "Launcher running in CLI mode" "Info"
} else {
    try {
        Write-Log "Loading UI components..." "Info"
        Show-Launcher
        Write-Log "Launcher exited normally" "Info"
    } catch {
        Write-Log "Launcher error: $_" "Error"
        Write-Log "Stack trace: $($_.ScriptStackTrace)" "Error"
        Write-Host "`n✗ Launcher Error: $_`n"
        Write-Host "Troubleshooting:`n  - Ensure .NET Framework 4.5+ is installed`n  - Run: Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`n  - Restart PowerShell`n"
        Read-Host "Press Enter to exit"
    }
}

Write-Log "Launcher closed" "Info"
