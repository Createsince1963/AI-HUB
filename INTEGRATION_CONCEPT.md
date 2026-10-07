# 🔗 AI Launcher Integration Concept

**Goal:** Unified Qt6 GUI that manages ALL portable AI tools via shared Launcher infrastructure

---

## 📦 Three Launcher Systems to Integrate

### 1. **AI_CLI Launcher** (Existing - JavaScript/Node.js)
```
F:\@AI Tools\AI_CLI\
├── launcher/
│   ├── launcher.mjs          ← Main orchestrator
│   ├── profiles.mjs
│   ├── sessions.mjs
│   ├── registry.mjs          ← Windows Explorer integration
│   └── ... (20+ modules)
├── Claude.bat                ← Bootstrap
├── Copilot.bat
├── Codex.bat
├── Antigravity.bat
└── Happy.bat
```

**Handles:** Claude Code, Copilot, Codex, Antigravity, Happy  
**Language:** JavaScript (Node.js)  
**Features:** Multi-profile, session picker, Windows Explorer integration

---

### 2. **ComfyUI Launcher** (New - JavaScript/Node.js)
```
F:\@AI Tools\ComfyUI_Portable\
├── launcher/
│   ├── comfyui-launcher.mjs  ← ComfyUI orchestrator
│   └── model-download-manager.mjs
├── ComfyUI.bat               ← Bootstrap
├── models/                   ← AI Image Models
└── profiles/
```

**Handles:** ComfyUI, Model Management  
**Language:** JavaScript (Node.js)  
**Features:** Model auto-download, profile management

---

### 3. **Ollama Launcher** (New - JavaScript/Node.js)
```
F:\@AI Tools\Ollama_Portable\
├── launcher/
│   ├── ollama-launcher.mjs   ← Ollama orchestrator
│   └── model-download-manager.mjs
├── Ollama.bat                ← Bootstrap
└── models/                   ← Local LLM Models
```

**Handles:** Ollama, Local LLMs  
**Language:** JavaScript (Node.js)  
**Features:** Model selection, auto-pull, server management

---

## 🎯 Qt6 GUI Integration Strategy

### Current State
```python
AI_Launcher/
├── main.py
├── tool_manager.py          ← Needs enhancement
├── launcher_tab.py
├── config_tab.py
├── status_tab.py
└── Launcher/                ← NEW: Symlink to/reference shared launchers
    ├── comfyui-launcher.mjs
    ├── ollama-launcher.mjs
    └── model-download-manager.mjs
```

### Enhanced tool_manager.py Strategy

```python
class ToolManager:
    """Manages ALL launcher systems"""
    
    def __init__(self, config):
        self.launchers = {
            'ai_cli': LauncherBridge('F:/@AI Tools/AI_CLI'),
            'comfyui': LauncherBridge('F:/@AI Tools/ComfyUI_Portable'),
            'ollama': LauncherBridge('F:/@AI Tools/Ollama_Portable'),
        }
    
    def start_tool(self, tool_name):
        """Route to appropriate launcher"""
        launcher = self._get_launcher_for_tool(tool_name)
        return launcher.start_tool(tool_name)
```

---

## 🔌 Launcher Bridge Pattern

### Python ↔ Node.js Communication

**Option 1: Process Execution (Current Approach)**
```python
# Start via .bat file
subprocess.Popen("F:/@AI Tools/ComfyUI_Portable/ComfyUI.bat")
```

**Option 2: Node.js Direct Call (Better)**
```python
# Call launcher module directly via Node.js
subprocess.Popen([
    "node",
    "F:/@AI Tools/ComfyUI_Portable/launcher/comfyui-launcher.mjs",
    "--start",
    "--profile", "default"
])
```

**Option 3: HTTP Bridge (Future - for remote/IPC)**
```python
# Start Node.js as HTTP server
# Qt GUI talks to it via REST API
# Better isolation, easier debugging
```

---

## 🏗️ Recommended Architecture

```
┌─────────────────────────────────────────┐
│      Qt6 GUI (main.py)                  │
│   ┌──────────────────────────────────┐  │
│   │  LauncherTab / StatusTab / ...   │  │
│   └──────────────────────────────────┘  │
│              ↓                           │
│   ┌──────────────────────────────────┐  │
│   │   ToolManager (enhanced)         │  │
│   │  - Routes to right launcher      │  │
│   │  - Manages process lifecycle     │  │
│   │  - Tracks status                 │  │
│   └──────────────────────────────────┘  │
│              ↓                           │
├──────────────┼──────────────┬────────────┤
│              │              │            │
↓              ↓              ↓            ↓
AI_CLI      ComfyUI        Ollama      OpenWebUI
Launcher    Launcher       Launcher    (direct)
(JS)        (JS)           (JS)        (Python)
```

---

## 🔧 Implementation Steps

### Phase 1: Integrate Existing AI_CLI
```python
# tool_manager.py enhancement
def _start_claude(self):
    """Route to AI_CLI launcher"""
    ai_cli_dir = self.ai_tools_root / "AI_CLI"
    
    # Option A: Call .bat
    subprocess.Popen(str(ai_cli_dir / "Claude.bat"))
    
    # Option B: Call launcher.mjs directly (better control)
    subprocess.Popen([
        self._get_node_exe(),
        str(ai_cli_dir / "launcher" / "launcher.mjs"),
        "--no-menu",  # Skip menu in GUI mode
    ])
```

### Phase 2: Integrate New ComfyUI/Ollama Launchers
```python
def _start_comfyui(self):
    """Start ComfyUI via portable launcher"""
    comfyui_dir = self.ai_tools_root / "ComfyUI_Portable"
    
    subprocess.Popen([
        self._get_node_exe(),
        str(comfyui_dir / "launcher" / "comfyui-launcher.mjs"),
        "--profile", "default",
    ])

def _start_ollama(self):
    """Start Ollama via portable launcher"""
    ollama_dir = self.ai_tools_root / "Ollama_Portable"
    
    subprocess.Popen([
        self._get_node_exe(),
        str(ollama_dir / "launcher" / "ollama-launcher.mjs"),
        "--model", "mistral:latest",
    ])
```

### Phase 3: Shared Model Manager
```python
# Shared across ComfyUI + Ollama
def _get_model_manager(self):
    """Access shared model index"""
    index_path = self.ai_tools_root / "@Shared" / "models-index.json"
    
    # Can query/update model status across tools
    models = self._load_json(index_path)
    return models
```

---

## 📊 Config Structure

### Enhanced config.json
```json
{
  "tools": {
    "claude": {
      "enabled": true,
      "launcher": "ai_cli",
      "launcher_module": "launcher.mjs",
      "path": "F:/@AI Tools/AI_CLI"
    },
    "comfyui": {
      "enabled": true,
      "launcher": "comfyui",
      "launcher_module": "comfyui-launcher.mjs",
      "path": "F:/@AI Tools/ComfyUI_Portable",
      "profile": "default",
      "auto_download_models": true
    },
    "ollama": {
      "enabled": true,
      "launcher": "ollama",
      "launcher_module": "ollama-launcher.mjs",
      "path": "F:/@AI Tools/Ollama_Portable",
      "model": "mistral:latest"
    }
  },
  "launcher_system": {
    "node_path": "auto",  # or specific path
    "shared_models_dir": "F:/@AI Tools/@Shared/models",
    "models_index": "F:/@AI Tools/@Shared/models-index.json"
  }
}
```

---

## 🚀 Execution Flow (User Clicks "Start ComfyUI")

```
1. Qt6 GUI: launcher_tab.py button click
   ↓
2. ToolManager.start_tool("comfyui")
   ↓
3. tool_manager.py: _start_comfyui()
   ├─ Get ComfyUI_Portable path from config
   ├─ Locate node.exe (from AI_CLI/app/node)
   ├─ Call launcher/comfyui-launcher.mjs
   ↓
4. comfyui-launcher.mjs
   ├─ Create directories
   ├─ Git clone ComfyUI (if needed)
   ├─ Check models (use shared model-download-manager.mjs)
   ├─ Pip install dependencies
   ├─ Start server on localhost:8188
   ├─ Broadcast ready event
   ↓
5. ToolManager: Wait for port 8188 to be ready
   ↓
6. Qt6 GUI: launcher_tab.py status updates
   ├─ Show "ComfyUI: RUNNING"
   ├─ Show "http://localhost:8188" link
   ├─ Optional: Open in browser
```

---

## 🔐 Advantages of This Architecture

✅ **Unified Interface** — One Qt6 GUI controls everything  
✅ **Modular** — Each tool has independent launcher  
✅ **Portable** — Everything on one drive  
✅ **Cross-Platform** — .mjs launchers work on Win/Mac/Linux  
✅ **Debuggable** — Can run launchers standalone  
✅ **Scalable** — Easy to add new tools  
✅ **User-Friendly** — No terminal needed  

---

## 📋 Next Steps

1. ✅ **Create LauncherBridge class** (Python)
   - Encapsulates Node.js launcher calls
   - Handles process lifecycle

2. ✅ **Update tool_manager.py** (already done)
   - Use LauncherBridge instead of direct commands
   - Route to correct launcher

3. ⏳ **Test Integration**
   - Claude.bat via GUI
   - ComfyUI.bat via GUI
   - Ollama.bat via GUI

4. ⏳ **Enhance launcher_tab.py**
   - Show launcher logs in real-time
   - Display tool URLs (clickable)
   - Profile/model selection per tool

5. ⏳ **Create shared launcher-core.mjs**
   - Common utilities for all launchers
   - UI components (progress, menus)
   - Error handling

---

## 🛠️ Key Files to Create/Update

```
AI_Launcher/
├── main.py                    [No change needed]
├── tool_manager_enhanced.py   [NEW - replaces tool_manager.py]
├── launcher_bridge.py         [NEW - encapsulates Node.js calls]
├── launcher_tab.py            [ENHANCE - show launcher status/logs]
├── config_tab.py              [ENHANCE - configure each launcher]
└── Launcher/                  [NEW - symlink or copy]
    ├── comfyui-launcher.mjs
    ├── ollama-launcher.mjs
    └── model-download-manager.mjs
```

---

**Status:** Architecture documented, ready for implementation ✓
