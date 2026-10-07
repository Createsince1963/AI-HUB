# 📦 Portable AI Toolchain Setup Guide

Complete portable AI suite for your external SSD: Claude Code, ComfyUI, Ollama + Local LLMs.
**Everything stays on the drive. Host machine completely untouched.**

---

## 🎯 Target Structure

```
F:\@AI Tools\
├── AI_CLI_Portable/              (Existing: Claude, Copilot, etc)
│   ├── Claude.bat
│   ├── launcher/
│   ├── profiles/
│   └── app/
│
├── ComfyUI_Portable/             (NEW: Image Generation AI)
│   ├── ComfyUI.bat               ← Start here
│   ├── launcher/
│   │   └── comfyui-launcher.mjs
│   ├── app/
│   │   ├── python/
│   │   ├── git/
│   │   └── comfyui/              (Git Clone)
│   ├── models/
│   │   ├── checkpoints/          (~50-100GB) Flux, SDXL, SD3
│   │   ├── loras/                (Fine-tuning)
│   │   ├── vaes/
│   │   ├── upscalers/
│   │   └── custom_nodes/
│   ├── profiles/                 (Workflows, configs)
│   ├── config/
│   └── output/                   (Generated images)
│
├── Ollama_Portable/              (NEW: Local LLMs)
│   ├── Ollama.bat                ← Start here
│   ├── launcher/
│   │   └── ollama-launcher.mjs
│   ├── app/
│   │   └── ollama/               (Binary)
│   ├── models/
│   │   ├── llama2/               (~13GB)
│   │   ├── mistral/              (~9GB)
│   │   ├── codellama/            (~25GB)
│   │   └── deepseek-coder/       (~26GB)
│   └── config/
│
└── @Shared/                      (Optional: Shared Tools)
    ├── launcher-core.mjs
    ├── model-download-manager.mjs
    └── models-index.json
```

---

## 🚀 Quick Start (Windows)

### 1. ComfyUI Setup
```bash
cd F:\@AI Tools\ComfyUI_Portable
ComfyUI.bat
```

**On first run:**
- Downloads Python 3.11 → `app/python/`
- Downloads Git → `app/git/`
- Clones ComfyUI → `app/comfyui/`
- Installs dependencies
- Opens web UI: `http://localhost:8188`

**On next runs:**
- Launches directly to web UI
- All models cached locally

### 2. Ollama Setup
```bash
cd F:\@AI Tools\Ollama_Portable
Ollama.bat
```

**Interactive model selection menu:**
```
Available Models:
  [1] Llama 2 7B              (~13GB)
      Meta Llama 2, general purpose
  [2] Mistral 7B              (~9GB)
      Mistral AI, fast and capable
  [3] CodeLlama 13B           (~25GB)
      Meta CodeLlama, specialized for coding
  [4] DeepSeek Coder          (~26GB)
      Best code generation
```

**After selection:**
- Automatically pulls model (first run)
- Starts server on `http://localhost:11434`
- Ready for: CLI, API, Web UI, Claude integration

---

## 📁 Directory Structure Explained

### `ComfyUI_Portable/`

**`app/`** — Portable runtimes + ComfyUI
- `python/` — Python 3.11 (downloaded, verified)
- `git/` — Git for Windows (downloaded)
- `comfyui/` — ComfyUI repository + code

**`models/`** — AI Models (persistent)
- `checkpoints/` — Main diffusion models (Flux, SDXL, SD3)
  - `flux-dev.safetensors` (23.5GB) — Latest SOTA
  - `sd_xl_turbo.safetensors` (7.7GB) — Fast generation
  - `sd3_medium.safetensors` (13.5GB) — Text-centric
  
- `loras/` — Fine-tuning models
- `vaes/` — Variational autoencoder models
- `upscalers/` — Super-resolution models
- `custom_nodes/` — ComfyUI plugins

**`profiles/`** — Workflow configurations
- `default/` — Default workflow settings
- Custom workflows can be saved here

**`config/`** — ComfyUI configuration
- `config.json` — Web UI settings
- `extra_model_paths.yaml` — Model locations

**`output/`** — Generated images (persistent)

---

### `Ollama_Portable/`

**`app/`**
- `ollama/` — Ollama executable

**`models/`** — Downloaded LLMs (persistent)
- Models auto-organized by Ollama
- No manual management needed

**`config/`** — Ollama configuration
- `Modelfile` — Custom model definitions
- `config.json` — Server settings

---

## 🎮 RTX 5070 (12GB) Optimization

### GPU Memory Strategy

**ComfyUI + Ollama (simultaneous use):**
- ComfyUI gets **8-10GB** for image generation
- Ollama gets **2-4GB** for LLM inference
- Shared CUDA memory management

**Configuration:**

**ComfyUI:**
```
Launch ComfyUI.bat with:
  --lowvram        (8GB models only)
  --normalvram     (12GB for larger models)
  --highvram       (RTX 5070 can handle Flux + SDXL)
```

**Ollama:**
```
Models with quantization:
  llama2:7b         → Q4_K_M (uses ~6GB)
  codellama:13b     → Q4_K_M (uses ~10GB)
  deepseek-coder    → Q4_K_M (uses ~11GB)
```

---

## 📥 Model Management

### Manual Download (ComfyUI)

Inside the web UI:
1. Right-click → Load Model → Download
2. Automatically saved to `models/checkpoints/`
3. Indexed in `models-index.json`

### Auto-Download (Future)

```javascript
// comfyui-launcher.mjs (v2)
await downloadModel('flux-dev', {
  destination: 'models/checkpoints/',
  verify: true,
  parallel: true,
});
```

### Ollama Auto-Pull

```bash
# Runs automatically when you select a model
ollama pull llama2:7b
ollama pull mistral:latest
```

---

## 🔌 Integration Points

### Claude Code + Ollama
```bash
# In Claude profile config:
export OLLAMA_API_URL=http://localhost:11434
```

Then use `claude --claude-code --use-ollama` to route requests.

### ComfyUI Workflows + Claude
Export ComfyUI workflows as JSON, feed to Claude for:
- Workflow optimization
- Batch processing
- Integration with other tools

---

## 🧹 Maintenance

### Check Installation Status
```bash
# ComfyUI
ComfyUI.bat --doctor

# Ollama
Ollama.bat --status
```

### Update Models
```bash
# Ollama (auto-checked daily)
Ollama.bat --update

# ComfyUI (manual via web UI or)
ComfyUI.bat --update-nodes
```

### Cleanup & Space Management
```bash
# Remove unused models (interactive)
Ollama.bat --cleanup

# Consolidate model cache
ComfyUI.bat --optimize-cache
```

---

## 📊 Expected Disk Usage

| Tool | Component | Size | Notes |
|------|-----------|------|-------|
| ComfyUI | Python | 400MB | One-time |
| | Git | 500MB | One-time |
| | ComfyUI repo | 300MB | One-time |
| | Flux model | 23.5GB | Largest checkpoint |
| | SDXL model | 7.7GB | Fast alternative |
| | SD3 model | 13.5GB | Text quality |
| | LoRAs (10x) | ~2GB | Optional |
| | Generated images | Variable | Grows with use |
| **Ollama** | Ollama binary | 200MB | One-time |
| | Llama2 7B | 13GB | Q4 quantization |
| | Mistral 7B | 9GB | Q4 quantization |
| | CodeLlama 13B | 25GB | Q4 quantization |
| | DeepSeek Coder | 26GB | Q4 quantization |

**Total for full setup:** ~150GB (including all models)  
**Typical setup:** ~80-100GB (2-3 models per tool)

---

## 🐛 Troubleshooting

### ComfyUI Won't Start
```bash
# Check Python
app\python\python-3.11.10\python.exe --version

# Check dependencies
app\python\python-3.11.10\python.exe -m pip list

# Reinstall
ComfyUI.bat --reinstall
```

### Ollama Slow / GPU Not Used
```bash
# Check CUDA availability
ollama --version

# Force CPU (if needed, slow but works)
Ollama.bat --cpu-only

# Check GPU memory
nvidia-smi
```

### Models Not Found
```bash
# Rebuild index
Ollama.bat --rebuild-index

# For ComfyUI, clear cache
del /s models\*.cache
```

---

## 📝 Next Steps

1. **Copy the templates** to your external SSD:
   - `ComfyUI.bat` → `ComfyUI_Portable/`
   - `comfyui-launcher.mjs` → `ComfyUI_Portable/launcher/`
   - `Ollama.bat` → `Ollama_Portable/`
   - `ollama-launcher.mjs` → `Ollama_Portable/launcher/`
   - `model-download-manager.mjs` → `@Shared/`

2. **Create directories:**
   ```bash
   mkdir ComfyUI_Portable\models\checkpoints
   mkdir ComfyUI_Portable\models\loras
   mkdir Ollama_Portable\models
   ```

3. **First run:**
   ```bash
   ComfyUI.bat    # ~5-10min for bootstrap
   Ollama.bat     # ~2-3min for bootstrap
   ```

4. **Download models** (happens during first use):
   - ComfyUI: Via web UI (~30-60min depending on model)
   - Ollama: Automatic (~15-30min depending on model)

---

## 🔐 Security & Integrity

All downloads are **SHA256-verified** before use:
- Node.js versions pinned
- Python versions pinned
- Model hashes logged in `models-index.json`

No external dependencies, everything self-contained.

---

**Questions?** Check the launcher source code or the shared `@Shared/` modules for detailed logic.
