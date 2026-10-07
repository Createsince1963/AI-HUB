# AI Portable - Handoff (2026-09-20)

Goal: fully self-contained portable AI toolchain on the external SSD (`F:\@AI Tools`), RTX 5070 12 GB.
All paths are relative to the auto-detected ROOT (parent of `AI_Launcher`), so F: -> G: / other PC works.

## Layout
| Folder | Purpose |
|---|---|
| `ComfyUI_Portable\` | ComfyUI Windows portable (cu130), moved from `Media_COMFYUI\...`. Launcher: `ComfyUI.bat` + `launcher\comfyui-launcher.mjs` (uses `python_embeded`, `config\extra_model_paths.yaml`, output in `output\`) |
| `AI_Modells\` | Central model library (checkpoints, loras, vae, text_encoders, diffusion_models, gguf, ...). Filled by `consolidate-models-v2.ps1` (copy from old installs) |
| `AI_Ollama_portable\` | Ollama (install still running / to verify) |
| `AI_CLI\` | Existing Node launcher system (Claude/Codex/Copilot/Happy/Antigravity .bat). Own python in `app\python` - do NOT use for own apps |
| `@Runtime\` | Shared platform for own developments (created by `Setup_Runtime.ps1`): `python\` (CPython 3.13, fresh download), `wheels\`, `bin\` wrappers, `downloads\` |
| `AI_Launcher\` | PySide6 GUI (this project) |

## Decisions
- Move (not copy) the ComfyUI install: `move-comfyui-install.ps1` (robocopy /MOVE, progress bar). Custom nodes: all kept.
- Models stay central in `AI_Modells` (underscore); ComfyUI reads them via generated `extra_model_paths.yaml` (written by the GUI on each ComfyUI start).
- Paths stored as `{ROOT}/...` in `AI_Launcher\config.json`; file browser for free choice; paths outside ROOT are flagged "absolute".
- No venv, no host Python: shared runtime `@Runtime\python` + `python -m pip` only (pip.exe launchers hold absolute paths).
- Runtime = fresh download (python-build-standalone 3.13 install_only, SHA256 verified) + `requirements.txt`: PySide6 6.11.0 (Essentials/Addons/shiboken6) + PyInstaller 6.21.0. Wheels cached in `@Runtime\wheels` (offline re-install).
- New apps are Qt6/PySide6. Qt5 apps get ported with `Qt5_Scan.bat` (report; `--apply-safe` = mechanical fixes with .bak).
- OpenWebUI runs permanently on the Home Assistant server: `http://192.168.178.21:8080/` (monitored + open button only; API key not integrated yet).
- Rule: every `.ps1` start command is given as `powershell -ExecutionPolicy Bypass -File "<path>.ps1"`.
- Existing central system on C: (`C:\Users\mail\AI_Coworker_Claude\Tools_Central`, WinPython 3.13 with PySide6 6.11.0, `_SYNC\toolchains.json`) is NOT a dependency of the stick. Optional later: extra path in config + registry entry.

## Files in `AI_Launcher\`
`launcher.py`, `core\` (config, modules, procs), `ui\` (start/settings/install tabs), `Start_AI_Launcher.bat`, `Autostart_AI_Launcher.bat` (copy into `shell:startup`; finds the drive letter itself), `Setup_Runtime.bat/.ps1`, `Qt5_Scan.bat`, `tools\qt5_scan.py`, `requirements*.txt`.
GUI tabs: Start (status/start/stop/open, green = installed, external services), Setup configuration (paths, ports, host, import/export), Installation (ordered steps, "next step"), Log.

## Status
- [x] ComfyUI move script run (verify: `ComfyUI_Portable\python_embeded` + `ComfyUI\main.py` present, source folder empty)
- [~] `Setup_Runtime.ps1` was running step 1/4 (python download) - result not yet reported
- [ ] Start `ComfyUI_Portable\ComfyUI.bat` (first run downloads Node.js; custom nodes built for cu128 may show dependency errors with cu130)
- [ ] Check `AI_Modells` subfolders after the model copy (must be standard names: checkpoints, loras, vae, text_encoders, diffusion_models ...)
- [ ] Ollama: confirm layout in `AI_Ollama_portable` (GUI searches `ollama.exe`, sets `OLLAMA_MODELS=AI_Ollama_portable\models`)
- [ ] Start `AI_Launcher\Start_AI_Launcher.bat` and test (never run on Windows yet; only smoke-tested headless)

## Open / next
1. Model Manager tab (two-pane source/target, copy/move, categorizer) - update its old default path `AI_Modells\comfyui` to `AI_Modells`.
2. Ollama model dropdown; OpenWebUI API key handling (not in plain text).
3. Port P3D Data Manager PyQt5 -> PySide6 with `Qt5_Scan.bat` (needs its project path).
4. Optional: PyInstaller `--onedir` build of the launcher; registry entry in Tools_Central `toolchains.json`.
5. Known risk: `Setup_Runtime.ps1` finds the Python asset via GitHub API + file-name pattern (unverified); Qt needs the MSVC runtime on the host (`msvcp140.dll`).
