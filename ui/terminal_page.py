"""Integrated terminal: cmd / PowerShell / runtime Python with history, colours, clear / copy / save log."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QProcess, QProcessEnvironment, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeyEvent, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QVBoxLayout, QWidget)

from .context import Ctx
from .widgets import PathRow, button, page_header

IS_WIN = sys.platform == "win32"
QUICK = ["", "python --version", "python -m pip list", "python -m pip list --outdated", "nvidia-smi",
         "ollama list", "ollama ps", "where python", "dir", "systeminfo | findstr /B /C:\"OS\" /C:\"Total Physical\""]


class HistoryEdit(QLineEdit):
    def __init__(self):
        super().__init__()
        self.hist: list[str] = []
        self.pos = 0

    def push(self, text: str) -> None:
        if text and (not self.hist or self.hist[-1] != text):
            self.hist.append(text)
        self.pos = len(self.hist)

    def keyPressEvent(self, e: QKeyEvent) -> None:        # noqa: N802
        if e.key() == Qt.Key_Up and self.hist:
            self.pos = max(0, self.pos - 1)
            self.setText(self.hist[self.pos])
        elif e.key() == Qt.Key_Down and self.hist:
            self.pos = min(len(self.hist), self.pos + 1)
            self.setText(self.hist[self.pos] if self.pos < len(self.hist) else "")
        else:
            super().keyPressEvent(e)


class TerminalPage(QWidget):
    def __init__(self, ctx: Ctx, header: bool = True):
        super().__init__()
        self.setObjectName("Page")
        self.ctx = ctx
        self.proc: QProcess | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 10)
        if header:        # HUB Terminal supplies the page header when this is embedded as a sub-tab
            lay.addWidget(page_header("Terminal", "Integrated command line with the portable runtime on PATH"))

        bar = QHBoxLayout()
        self.shell = QComboBox()
        if IS_WIN:
            self.shell.addItem("Command Prompt (cmd)", "cmd")
            self.shell.addItem("PowerShell", "ps")
        else:
            self.shell.addItem("bash", "bash")
        self.shell.addItem("Runtime Python (REPL)", "py")
        self.shell.currentIndexChanged.connect(self.restart)
        bar.addWidget(QLabel("Shell"))
        bar.addWidget(self.shell)
        bar.addWidget(QLabel("Folder"))
        self.cwd = PathRow()
        self.cwd.edit.setText(str(ctx.cfg.root))
        self.cwd.changed.connect(lambda _t: self.restart())
        bar.addWidget(self.cwd, 1)
        self.use_rt = QCheckBox("@Runtime on PATH")
        self.use_rt.setChecked(True)
        self.use_rt.toggled.connect(self.restart)
        bar.addWidget(self.use_rt)
        bar.addWidget(button("Restart shell", "", self.restart))
        lay.addLayout(bar)

        inp = QHBoxLayout()
        self.prompt = QLabel(">")
        self.prompt.setStyleSheet("font-weight:800; color:#146C94;")
        self.edit = HistoryEdit()
        self.edit.setPlaceholderText("Type a command and press Enter  (Up/Down = history)")
        self.edit.returnPressed.connect(self.send)
        self.quick = QComboBox()
        self.quick.addItems([q or "Quick commands..." for q in QUICK])
        self.quick.activated.connect(self._quick)
        inp.addWidget(self.prompt)
        inp.addWidget(self.edit, 1)
        inp.addWidget(self.quick)
        inp.addWidget(button("Send", "Primary", self.send))
        inp.addWidget(button("Break", "Danger", self.brk, "Stop the running command (keeps the shell)"))
        lay.addLayout(inp)

        # Shown when the running program seems to wait for an answer (Y/n, [y/N], "Press any key", ...)
        self.ask = QWidget()
        ab = QHBoxLayout(self.ask)
        ab.setContentsMargins(0, 0, 0, 0)
        ql = QLabel("Program is waiting for input:")
        ql.setStyleSheet("font-weight:700; color:#B26A00;")
        ab.addWidget(ql)
        ab.addWidget(button("Yes (y)", "Primary", lambda: self._answer("y")))
        ab.addWidget(button("No (n)", "", lambda: self._answer("n")))
        ab.addWidget(button("Enter", "", lambda: self._answer("")))
        ab.addWidget(button("Ctrl+C", "Danger", self.brk, "Abort the running command"))
        ab.addStretch(1)
        self.ask.setVisible(False)
        lay.addWidget(self.ask)
        self._tail = ""

        tools = QHBoxLayout()
        tools.addWidget(button("Clear log", "", self.clear))
        tools.addWidget(button("Copy all", "", self.copy_all))
        tools.addWidget(button("Copy selection", "", self.copy_sel))
        tools.addWidget(button("Save log...", "", self.save))
        self.wrap = QCheckBox("Word wrap")
        self.wrap.toggled.connect(lambda on: self.out.setLineWrapMode(
            QPlainTextEdit.WidgetWidth if on else QPlainTextEdit.NoWrap))
        self.auto = QCheckBox("Auto-scroll")
        self.auto.setChecked(True)
        tools.addWidget(self.wrap)
        tools.addWidget(self.auto)
        tools.addWidget(button("A-", "", lambda: self._font(-1)))
        tools.addWidget(button("A+", "", lambda: self._font(+1)))
        tools.addStretch(1)
        self.state = QLabel("")
        self.state.setObjectName("Muted")
        tools.addWidget(self.state)
        lay.addLayout(tools)

        self.out = QPlainTextEdit()
        self.out.setObjectName("Console")
        self.out.setReadOnly(True)
        self.out.installEventFilter(self)      # typing in the log is redirected to the input line
        self.out.setMaximumBlockCount(20000)
        self.out.setLineWrapMode(QPlainTextEdit.NoWrap)
        f = QFont("Cascadia Mono" if IS_WIN else "monospace")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(10)
        self.out.setFont(f)
        lay.addWidget(self.out, 1)
        self._started = False

    # ---------------------------------------------------------------- process
    def on_show(self) -> None:
        if not self._started:
            self._started = True
            self.restart()
        self.edit.setFocus()

    def _runtime_python(self) -> str:
        p = self.ctx.cfg.path("runtime") / "python" / "python.exe"
        return str(p) if p.exists() else sys.executable

    def restart(self, *_a) -> None:
        if not self._started:
            return
        self._kill()
        kind = self.shell.currentData()
        env = QProcessEnvironment.systemEnvironment()
        rt = self.ctx.cfg.path("runtime")
        if self.use_rt.isChecked():
            from core.runtime_tools import path_dirs
            extra = os.pathsep.join(str(p) for p in (rt / "bin", rt / "python", rt / "python" / "Scripts",
                                                     *path_dirs(self.ctx.cfg)) if p.exists())
            if extra:
                env.insert("PATH", extra + os.pathsep + env.value("PATH"))
            env.insert("OLLAMA_MODELS", str(self.ctx.cfg.path("ollama_models")))
        env.insert("PYTHONUTF8", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        proc = QProcess(self)
        proc.setProcessEnvironment(env)
        wd = self.cwd.edit.text().strip()
        proc.setWorkingDirectory(wd if Path(wd).is_dir() else str(self.ctx.cfg.root))
        proc.readyReadStandardOutput.connect(lambda: self._read(proc, False))
        proc.readyReadStandardError.connect(lambda: self._read(proc, True))
        proc.finished.connect(lambda code, _s: self._ended(proc, code))
        if kind == "cmd":
            prog, args = "cmd.exe", ["/Q", "/K", "chcp 65001>nul"]
        elif kind == "ps":
            prog, args = "powershell.exe", ["-NoLogo", "-NoProfile", "-NoExit", "-Command",
                                            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                                            "Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force"]
        elif kind == "bash":
            prog, args = "/bin/bash", ["--norc", "-i"]
        else:
            prog, args = self._runtime_python(), ["-u", "-i"]
        self.proc = proc
        proc.start(prog, args)
        self.prompt.setText({"cmd": "cmd>", "ps": "PS>", "bash": "$", "py": ">>>"}[kind])
        self._write(f"--- {self.shell.currentText()}  |  {proc.workingDirectory()} ---\n", "#7FB3D5")
        self.state.setText("running")

    def _kill(self) -> None:
        if self.proc is not None:
            p, self.proc = self.proc, None
            p.blockSignals(True)
            self._kill_tree(p)
            p.kill()
            p.waitForFinished(1000)
            p.deleteLater()

    def _kill_tree(self, p: QProcess, children_only: bool = False) -> None:
        pid = int(p.processId())
        if not pid:
            return
        try:
            import psutil
            parent = psutil.Process(pid)
            for c in parent.children(recursive=True):
                c.kill()
            if not children_only:
                parent.kill()
        except Exception:      # noqa: BLE001
            pass

    def brk(self) -> None:
        if self.proc:
            self._kill_tree(self.proc, children_only=True)
            self._write("^C (child processes ended)\n", "#F0B429")

    def _ended(self, proc: QProcess, code: int) -> None:
        if proc is self.proc:
            self._write(f"\n[shell ended, exit code {code}] - press 'Restart shell'\n", "#F0B429")
            self.state.setText("stopped")

    # ---------------------------------------------------------------- io
    def _read(self, proc: QProcess, err: bool) -> None:
        data = bytes(proc.readAllStandardError() if err else proc.readAllStandardOutput())
        text = data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "")
        self._write(text, "#FF8A80" if err else None)
        self._tail = (self._tail + text)[-300:]
        self.ask.setVisible(bool(self._WAIT_RE.search(self._tail)))
        if self.ask.isVisible():
            self.edit.setFocus()

    def _write(self, text: str, colour: str | None = None) -> None:
        cur = self.out.textCursor()
        cur.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(colour or "#D7E3F1"))
        cur.insertText(text, fmt)
        if self.auto.isChecked():
            self.out.moveCursor(QTextCursor.End)

    _WAIT_RE = re.compile(
        r"(?:\(\s*[yj]\s*/\s*n\s*\)|\[\s*[yj]\s*/\s*n\s*\]|\[\s*(?:yes|ja)\s*/\s*no?\s*\]"
        r"|press (?:any key|enter)[^\n]*|\?)\s*:?\s*$", re.I)

    def eventFilter(self, obj, ev) -> bool:      # noqa: N802
        if obj is self.out and ev.type() == QEvent.KeyPress:
            t = ev.text()
            if t and t.isprintable() and not (ev.modifiers() & (Qt.ControlModifier | Qt.AltModifier)):
                self.edit.setFocus()
                self.edit.insert(t)
                return True
            if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
                self.edit.setFocus()
                self.send()
                return True
        return super().eventFilter(obj, ev)

    def _answer(self, text: str) -> None:
        if not self.proc:
            return
        self._write(f"{text}\n", "#9ED0A8")
        self.proc.write((text + "\n").encode("utf-8"))
        self._tail = ""
        self.ask.setVisible(False)
        self.edit.setFocus()

    _PROMPT_RE = re.compile(r"^\s*(?:PS\s+[^>\n]*>|PS>|cmd>|>>>|\$)\s*", re.I)
    _PS1_RE = re.compile(r'(?:^|\s)(?:-File\s+)?(?:"([^"]+\.ps1)"|\'([^\']+\.ps1)\'|(\S+\.ps1))(.*)$', re.I)

    def _normalize(self, text: str) -> str:
        """Strip pasted prompt prefixes ('PS F:\\x> ', 'PS> ') and smart quotes."""
        text = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'").strip()
        while True:
            m = self._PROMPT_RE.match(text)
            if not m or not m.group(0).strip():
                break
            text = text[m.end():]
        return text

    def _resolve(self, name: str) -> Path | None:
        base = Path(self.cwd.edit.text().strip() or self.ctx.cfg.root)
        p = Path(name)
        p = p if p.is_absolute() else base / p
        return p if p.is_file() else None

    def _prepare(self, text: str) -> str | None:
        """Make .ps1 calls robust: existence check, quoting, execution policy. None = do not send."""
        m = self._PS1_RE.search(text)
        if not m or not re.match(r"^\s*(?:powershell(?:\.exe)?\b|pwsh\b|&|\.\\|[A-Za-z]:|\"|')", text, re.I):
            return text
        name = m.group(1) or m.group(2) or m.group(3)
        args = (m.group(4) or "").strip()
        p = self._resolve(name)
        if p is None:
            self._write(f"[!] Script not found: {name}\n", "#FF8A80")
            base = Path(self.cwd.edit.text().strip() or self.ctx.cfg.root)
            try:
                hits = [str(x) for x in base.rglob(Path(name).name.lower()) ][:5] or \
                       [str(x) for x in base.rglob("*.ps1") if Path(name).stem.lower()[:6] in x.stem.lower()][:5]
            except OSError:
                hits = []
            if hits:
                self._write("    Did you mean:\n" + "".join(f"      {h}\n" for h in hits), "#F0B429")
            return None
        kind = self.shell.currentData()
        if kind == "ps":
            q = str(p).replace("'", "''")
            return f"& '{q}' {args}".rstrip()
        return f'powershell -NoProfile -ExecutionPolicy Bypass -File "{p}" {args}'.rstrip()

    def send(self) -> None:
        raw = self.edit.text()
        if not self.proc:
            return
        if not raw.strip():                       # bare Enter, e.g. for "Press Enter to continue"
            self._answer("")
            return
        self._tail = ""
        self.ask.setVisible(False)
        self.edit.push(raw)
        self._write(f"{self.prompt.text()} {raw}\n", "#9ED0A8")
        self.edit.clear()
        text = self._normalize(raw)
        if self.shell.currentData() in ("ps", "cmd"):
            text = self._prepare(text)
        if not text:
            return
        self.proc.write((text + "\n").encode("utf-8"))

    def _quick(self, i: int) -> None:
        if i > 0:
            self.edit.setText(QUICK[i])
            self.quick.setCurrentIndex(0)
            self.edit.setFocus()

    # ---------------------------------------------------------------- toolbar
    def clear(self) -> None:
        self.out.clear()

    def copy_all(self) -> None:
        QGuiApplication.clipboard().setText(self.out.toPlainText())
        self.state.setText("log copied")
        QTimer.singleShot(1500, lambda: self.state.setText("running" if self.proc else "stopped"))

    def copy_sel(self) -> None:
        self.out.copy()

    def save(self) -> None:
        f, _ = QFileDialog.getSaveFileName(self, "Save terminal log", "terminal_log.txt", "Text (*.txt)")
        if f:
            Path(f).write_text(self.out.toPlainText(), encoding="utf-8")

    def _font(self, d: int) -> None:
        f = self.out.font()
        f.setPointSize(max(7, min(22, f.pointSize() + d)))
        self.out.setFont(f)

    def shutdown(self) -> None:
        self._kill()
