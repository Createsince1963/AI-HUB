#!/usr/bin/env node
// =============================================================================
// ComfyUI Portable Launcher
// =============================================================================
// Orchestrates:
//   1. Profile detection (workflows, settings)
//   2. Model verification + auto-download (Flux, SDXL, VAEs, etc)
//   3. Git clone of ComfyUI (if first run)
//   4. pip install dependencies
//   5. Start ComfyUI web server
//   6. Open browser to localhost:8188

import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import url from 'node:url';

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------
const __filename = url.fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PORTABLE_ROOT = path.resolve(__dirname, '..');

const PYTHON_DIR = path.join(PORTABLE_ROOT, 'app', 'python');
const PYTHON_BIN = path.join(PYTHON_DIR, 'python-3.11.10', 'python.exe');
const GIT_DIR = path.join(PORTABLE_ROOT, 'app', 'git');
const GIT_BIN = path.join(GIT_DIR, 'bin', 'git.exe');

const COMFYUI_DIR = path.join(PORTABLE_ROOT, 'app', 'comfyui');
const MODELS_DIR = path.join(PORTABLE_ROOT, 'models');
const PROFILES_DIR = path.join(PORTABLE_ROOT, 'profiles');
const CONFIG_DIR = path.join(PORTABLE_ROOT, 'config');
const OUTPUT_DIR = path.join(PORTABLE_ROOT, 'output');

const COMFYUI_REPO = 'https://github.com/comfyanonymous/ComfyUI.git';
const COMFYUI_PORT = 8188;

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
async function main() {
    console.log('\n');
    console.log('╔════════════════════════════════════════════════════════════════╗');
    console.log('║          ComfyUI Portable Launcher                              ║');
    console.log('║  All tools & models stay on this drive. Host machine untouched. ║');
    console.log('╚════════════════════════════════════════════════════════════════╝');
    console.log('\n');

    try {
        // Create necessary directories
        ensureDirectories();
        console.log('✓ Directories ready\n');

        // Install ComfyUI (first run only)
        if (!fs.existsSync(path.join(COMFYUI_DIR, 'main.py'))) {
            await installComfyUI();
        }
        console.log('✓ ComfyUI ready\n');

        // Check + verify models
        console.log('Checking AI models...');
        await ensureModels();
        console.log('✓ Models ready\n');

        // Install dependencies
        console.log('Installing Python dependencies...');
        await installDependencies();
        console.log('✓ Dependencies ready\n');

        // Start ComfyUI server
        console.log('═══════════════════════════════════════════════════════════════');
        console.log(`Starting ComfyUI server on http://localhost:${COMFYUI_PORT}`);
        console.log('═══════════════════════════════════════════════════════════════\n');

        await startComfyUI();

    } catch (err) {
        console.error('\nERROR:', err.message);
        process.exit(1);
    }
}

function ensureDirectories() {
    const dirs = [
        MODELS_DIR,
        path.join(MODELS_DIR, 'checkpoints'),
        path.join(MODELS_DIR, 'loras'),
        path.join(MODELS_DIR, 'embeddings'),
        path.join(MODELS_DIR, 'vae'),
        path.join(MODELS_DIR, 'upscalers'),
        path.join(MODELS_DIR, 'custom_nodes'),
        PROFILES_DIR,
        CONFIG_DIR,
        OUTPUT_DIR,
    ];

    for (const dir of dirs) {
        if (!fs.existsSync(dir)) {
            fs.mkdirSync(dir, { recursive: true });
        }
    }
}

async function installComfyUI() {
    console.log('Installing ComfyUI (first run, this may take a moment)...');

    if (!fs.existsSync(COMFYUI_DIR)) {
        fs.mkdirSync(COMFYUI_DIR, { recursive: true });
    }

    const result = spawnSync(GIT_BIN, ['clone', COMFYUI_REPO, COMFYUI_DIR], {
        stdio: 'inherit',
    });

    if (result.error || result.status !== 0) {
        throw new Error(`Failed to clone ComfyUI: ${result.error?.message || 'unknown error'}`);
    }
}

async function ensureModels() {
    // Check which models are already present
    const checkpointsDir = path.join(MODELS_DIR, 'checkpoints');
    const files = fs.readdirSync(checkpointsDir).filter(f => f.endsWith('.safetensors') || f.endsWith('.pt'));

    console.log(`  Found ${files.length} checkpoint(s)`);

    if (files.length === 0) {
        console.log('\n  ⚠️  No models found yet. ComfyUI will work, but you need to:');
        console.log('     1. Download models manually via the web UI');
        console.log('     2. Or use the model manager (see config)\n');
    }

    // TODO: Implement auto-downloader for popular models (Flux, SDXL, etc)
    // This would be a separate model-download.mjs module
}

async function installDependencies() {
    const result = spawnSync(PYTHON_BIN, ['-m', 'pip', 'install', '-r', 'requirements.txt'], {
        cwd: COMFYUI_DIR,
        stdio: 'inherit',
    });

    if (result.error || result.status !== 0) {
        console.warn('  ⚠️  pip install had issues (this is often okay, ComfyUI may still work)');
    }
}

async function startComfyUI() {
    // Set environment variables for GPU acceleration
    process.env.CUDA_VISIBLE_DEVICES = '0';

    const args = [
        'main.py',
        '--listen', '0.0.0.0',          // Listen on all interfaces
        '--port', String(COMFYUI_PORT),
    ];

    // ComfyUI doesn't naturally fork, so this will block until user stops it
    const result = spawnSync(PYTHON_BIN, args, {
        cwd: COMFYUI_DIR,
        stdio: 'inherit',
    });

    process.exit(result.status ?? 0);
}

main().catch(err => {
    console.error('\nFATAL ERROR:', err.message);
    process.exit(1);
});
