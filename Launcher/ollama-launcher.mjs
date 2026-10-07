#!/usr/bin/env node
// =============================================================================
// Ollama Portable Launcher
// =============================================================================
// Orchestrates:
//   1. Profile/model selection
//   2. Model verification + pull (if needed)
//   3. Start Ollama server
//   4. Optional: Start Web UI (Open WebUI)
//   5. Show integration options (API, Claude, etc)

import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import url from 'node:url';
import readline from 'node:readline';

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------
const __filename = url.fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PORTABLE_ROOT = path.resolve(__dirname, '..');

const OLLAMA_BIN = path.join(PORTABLE_ROOT, 'app', 'ollama', 'ollama.exe');
const MODELS_DIR = path.join(PORTABLE_ROOT, 'models');
const CONFIG_DIR = path.join(PORTABLE_ROOT, 'config');
const PROFILES_DIR = path.join(PORTABLE_ROOT, 'profiles');

const OLLAMA_PORT = 11434;
const WEBUI_PORT = 3000;

// Available models with info
const AVAILABLE_MODELS = {
    'llama2:7b': {
        name: 'Llama 2 7B',
        size: '~13GB',
        description: 'Meta Llama 2, general purpose',
        tags: 'general, english',
    },
    'mistral:latest': {
        name: 'Mistral 7B',
        size: '~9GB',
        description: 'Mistral AI, fast and capable',
        tags: 'general, french',
    },
    'codellama:13b': {
        name: 'CodeLlama 13B',
        size: '~25GB',
        description: 'Meta CodeLlama, specialized for coding',
        tags: 'coding, debug',
    },
    'neural-chat:latest': {
        name: 'Neural Chat 7B',
        size: '~8GB',
        description: 'Intel Neural Chat, optimized for chat',
        tags: 'chat, general',
    },
    'deepseek-coder:latest': {
        name: 'DeepSeek Coder',
        size: '~26GB',
        description: 'DeepSeek-Coder, excellent code generation',
        tags: 'coding, math',
    },
};

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
async function main() {
    console.log('\n');
    console.log('╔════════════════════════════════════════════════════════════════╗');
    console.log('║          Ollama Portable Launcher                               ║');
    console.log('║  Local LLMs stay on this drive. Host machine untouched.         ║');
    console.log('╚════════════════════════════════════════════════════════════════╝');
    console.log('\n');

    try {
        ensureDirectories();
        console.log('✓ Directories ready\n');

        // Show model selection menu
        const selectedModel = await modelSelectionMenu();

        if (!selectedModel) {
            console.log('\n✗ No model selected. Exiting.');
            process.exit(0);
        }

        console.log(`\nSelected: ${selectedModel.name}\n`);

        // Ensure model is available (pull if needed)
        await ensureModel(selectedModel.tag);

        // Start Ollama server
        console.log('═══════════════════════════════════════════════════════════════');
        console.log(`Starting Ollama server on http://localhost:${OLLAMA_PORT}`);
        console.log('═══════════════════════════════════════════════════════════════\n');

        await startOllama(selectedModel.tag);

    } catch (err) {
        console.error('\nERROR:', err.message);
        process.exit(1);
    }
}

function ensureDirectories() {
    const dirs = [MODELS_DIR, CONFIG_DIR, PROFILES_DIR];
    for (const dir of dirs) {
        if (!fs.existsSync(dir)) {
            fs.mkdirSync(dir, { recursive: true });
        }
    }
}

async function modelSelectionMenu() {
    const rl = readline.createInterface({
        input: process.stdin,
        output: process.stdout,
    });

    console.log('Available Models:\n');

    let idx = 1;
    const models = Object.entries(AVAILABLE_MODELS).map(([tag, info]) => {
        console.log(`  [${idx}] ${info.name.padEnd(25)} (${info.size})`);
        console.log(`      ${info.description}`);
        console.log('');
        idx++;
        return { tag, ...info };
    });

    return new Promise((resolve) => {
        rl.question('[Select 1-' + models.length + ' or press Ctrl+C to quit]: ', (answer) => {
            rl.close();
            const choice = parseInt(answer, 10);
            if (choice >= 1 && choice <= models.length) {
                resolve(models[choice - 1]);
            } else {
                console.log('Invalid choice.');
                resolve(null);
            }
        });
    });
}

async function ensureModel(modelTag) {
    console.log(`Checking for ${modelTag}...`);

    // TODO: Check if model exists locally
    // For now, we'll assume Ollama will handle the pull

    const result = spawnSync(OLLAMA_BIN, ['pull', modelTag], {
        stdio: 'inherit',
    });

    if (result.error || result.status !== 0) {
        throw new Error(`Failed to pull model ${modelTag}`);
    }
}

async function startOllama(modelTag) {
    // Start Ollama server in background (headless)
    // Note: On Windows, we need to handle this differently

    console.log(`Loading model: ${modelTag}`);
    console.log('\nServer ready. Use one of these:\n');
    console.log(`  CLI:       ollama run ${modelTag}`);
    console.log(`  API:       curl http://localhost:${OLLAMA_PORT}/api/generate`);
    console.log(`  Web UI:    http://localhost:${WEBUI_PORT} (if Open WebUI installed)`);
    console.log('\n' + '═'.repeat(63) + '\n');

    // Start the model and keep it running
    const result = spawnSync(OLLAMA_BIN, ['serve'], {
        stdio: 'inherit',
        env: {
            ...process.env,
            OLLAMA_HOST: `localhost:${OLLAMA_PORT}`,
        },
    });

    process.exit(result.status ?? 0);
}

main().catch(err => {
    console.error('\nFATAL ERROR:', err.message);
    process.exit(1);
});
