"""One tab per CLI tool (Claude / Codex / Copilot / Antigravity), each an
embedded real terminal (ConPTY via pyte + pywinpty) running that tool's
AI_CLI\\<Name>.bat directly inside the GUI - light background matching the
rest of the app, instead of the separate console window the Dashboard's
'Start' button still opens.

Arrow-key menus (profile picker, --select-model) work exactly as in a real
console because pyte keeps a correct character grid; only the colours are
retuned for white instead of a dark terminal (see pty_terminal.LIGHT_FG).

Double-start protection is NOT implemented here. This page registers its
embedded terminal with the central ProcessManager under the same key the
Dashboard row and the external-console fallback use ("cli:<Name>"), and the
manager enforces "one running instance per key" for every start path -
Dashboard Start, "Neu starten" here and "Externe Konsole" alike. A guard
local to this page could only ever see two of those three.

A running session is never ended silently. If the tool is already active in
another variant, the start is REFUSED with a clear message - what would have
been killed is typically a live CLI conversation or a running agent task.
Switching variants is its own named command ("Wechseln"), which asks first
and then stops and restarts in a controlled order. Only stop_all() and the
GUI shutdown end everything without asking; there that IS the intent.
"""
from __future__ import annotations

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton,
                               QVBoxLayout, QWidget)

from core import mcp, models, profiles
from .context import Ctx
from .pty_terminal import PtyTerminalWidget, pty_available
from .widgets import button, page_header


class CliTerminalPage(QWidget):
    """tool: the AI_CLI bat file stem, e.g. 'Claude', 'Codex', 'Copilot', 'Antigravity'."""

    def __init__(self, ctx: Ctx, tool: str, header: bool = True):
        super().__init__()
        self.setObjectName("Page")
        self.ctx = ctx
        self.tool = tool
        self._started = False
        self._busy = False                      # re-entrancy guard for hammered buttons

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 10)
        if header:
            lay.addWidget(page_header(tool, f"Eingebettetes {tool}-Terminal - Pfeiltasten-Menüs, Farben, "
                                            "Bild-Neuzeichnen funktionieren wie im externen Konsolenfenster."))

        bar = QHBoxLayout()
        self.profile_box: QComboBox | None = None
        if profiles.supported(tool):
            bar.addWidget(QLabel("Profil"))
            self.profile_box = QComboBox()
            self._reload_profiles()
            self.profile_box.currentTextChanged.connect(self._profile_changed)
            bar.addWidget(self.profile_box)
        self.model_box: QComboBox | None = None
        self.effort_box: QComboBox | None = None
        if profiles.supported(tool):
            bar.addWidget(QLabel("Modell"))
            self.model_box = QComboBox()
            self.model_box.currentIndexChanged.connect(self._model_changed)
            bar.addWidget(self.model_box)
            bar.addWidget(QLabel("Stärke"))
            self.effort_box = QComboBox()
            self.effort_box.currentIndexChanged.connect(self._effort_changed)
            bar.addWidget(self.effort_box)
            self._reload_model_effort()
        self.mcp_btn: QPushButton | None = None
        if mcp.supported(tool):
            bar.addWidget(QLabel("MCP"))
            self.mcp_btn = QPushButton("MCP: none")
            self.mcp_btn.setToolTip("MCP servers passed to this tool at the next start (Scrapling is started "
                                     "automatically) - same setting as the Dashboard row's MCP button.")
            self.mcp_btn.clicked.connect(self._mcp_menu)
            bar.addWidget(self.mcp_btn)
            self._update_mcp_label()
        bar.addStretch(1)
        self.hold_label = QLabel("")
        self.hold_label.setObjectName("Muted")
        self.hold_label.setStyleSheet("color: #f9ab00;")
        bar.addWidget(self.hold_label)
        self.state = QLabel("")
        self.state.setObjectName("Muted")
        bar.addWidget(self.state)
        bar.addWidget(button("Neu starten", "", self.restart, f"{tool} in diesem Tab neu starten"))
        bar.addWidget(button("Stoppen", "Danger", self.stop, f"{tool}-Prozess in diesem Tab beenden"))
        bar.addWidget(button("Externe Konsole", "", self.start_external,
                              f"{tool} stattdessen in einem eigenen Konsolenfenster starten - Fallback, falls "
                              "pywinpty/pyte fehlen oder das eingebettete Terminal Probleme macht. "
                              "Verweigert, solange die CLI anderswo läuft - dafür gibt es 'Wechseln'."))
        bar.addWidget(button("Wechseln", "", self.switch_session,
                              f"Ausdrücklicher Wechsel zwischen eingebettetem Terminal und externer Konsole: "
                              f"beendet die laufende {tool}-Sitzung kontrolliert und startet sie in der anderen "
                              "Variante neu. Fragt vorher nach - die laufende Sitzung geht dabei verloren."))
        self.dark_box = QCheckBox("Dunkles Terminal")
        self.dark_box.setToolTip("Manche CLIs sind auf einen dunklen Hintergrund ausgelegt "
                                  "(z. B. dimmed/inverse Text) - hier umschaltbar statt fest hell.")
        self.dark_box.toggled.connect(lambda on: self.term.set_light(not on))
        bar.addWidget(self.dark_box)
        self.install_btn = button("Pakete installieren (pywinpty, pyte)", "Primary", self._install_deps,
                                   "Installiert die für das eingebettete Terminal fehlenden Python-Pakete "
                                   "über den vom Launcher bekannten Runtime-Python-Pfad - kein manuelles Tippen.")
        bar.addWidget(self.install_btn)
        lay.addLayout(bar)

        self.term = PtyTerminalWidget()
        self.term.started.connect(lambda: self.state.setText("läuft"))
        self.term.ended.connect(lambda _c: self.state.setText("beendet - 'Neu starten' zum erneuten Start"))
        lay.addWidget(self.term, 1)

        self._install_proc: QProcess | None = None
        self._sync_install_btn()
        # Registered last, once self.term exists: from here on ctx.procs sees this tab's embedded
        # terminal as a running instance of "cli:<Tool>" and stops it before any other start path
        # (Dashboard row, external console) starts the same tool.
        self.ctx.procs.register_embedded(self._cli_key(), self)

    def _sync_install_btn(self) -> None:
        ready = pty_available()
        self.install_btn.setVisible(not ready)
        if not ready:
            self.state.setText("pywinpty/pyte fehlen")

    # ------------------------------------------------------------ profile combo
    def _reload_profiles(self) -> None:
        if self.profile_box is None:
            return
        names = profiles.list_profiles(self.ctx.cfg, self.tool) or ["default"]
        current = profiles.selected(self.ctx.cfg, self.tool)
        self.profile_box.blockSignals(True)
        self.profile_box.clear()
        self.profile_box.addItems(names)
        if current in names:
            self.profile_box.setCurrentText(current)
        self.profile_box.blockSignals(False)

    def _profile_changed(self, name: str) -> None:
        if not name:
            return
        profiles.select(self.ctx.cfg, self.tool, name)
        try:
            self.ctx.cfg.save()
        except OSError as exc:
            self.ctx.log(f"[{self.tool}] Profil-Auswahl nicht gespeichert: {exc}")
        self.ctx.log(f"[{self.tool}] Profil: {name} - gilt ab 'Neu starten'")
        self._reload_model_effort()      # model/effort choice is stored per profile

    # ------------------------------------------------------------ model + effort combo
    def _current_profile(self) -> str:
        return profiles.selected(self.ctx.cfg, self.tool)

    def _reload_model_effort(self) -> None:
        """(Re)populate both combos from models.json + the profile's stored .model.json choice.
        Same catalog and same files the Node launcher's model-select.mjs reads/writes, so the
        GUI and `<Tool>.bat --select-model` never disagree."""
        if self.model_box is None:
            return
        cfg, profile = self.ctx.cfg, self._current_profile()
        tc = models.tool_catalog(cfg, self.tool)
        current = models.get_selected_model(cfg, profile, self.tool)

        self.model_box.blockSignals(True)
        self.model_box.clear()
        for m in tc.get("models", []):
            self.model_box.addItem(m.get("label", m["id"]), m["id"])
        idx = self.model_box.findData(current["model"])
        if idx >= 0:
            self.model_box.setCurrentIndex(idx)
        self.model_box.blockSignals(False)

        self._reload_effort_options(current.get("effort"))

    def _reload_effort_options(self, current_effort: str | None) -> None:
        if self.effort_box is None:
            return
        cfg = self.ctx.cfg
        supports = models.supports_effort(cfg, self.tool)
        self.effort_box.blockSignals(True)
        self.effort_box.clear()
        if supports:
            for lvl in models.effort_levels(cfg, self.tool):
                self.effort_box.addItem(models.effort_label(cfg, self.tool, lvl), lvl)
            idx = self.effort_box.findData(current_effort)
            if idx >= 0:
                self.effort_box.setCurrentIndex(idx)
        self.effort_box.setVisible(supports)
        self.effort_box.blockSignals(False)

    def _model_changed(self) -> None:
        if self.model_box is None:
            return
        model_id = self.model_box.currentData()
        if not model_id:
            return
        effort = self.effort_box.currentData() if self.effort_box is not None else None
        models.save_selected_model(self.ctx.cfg, self._current_profile(), self.tool, model_id, effort)
        self.ctx.log(f"[{self.tool}] Modell: {self.model_box.currentText()} - gilt ab 'Neu starten'")

    def _effort_changed(self) -> None:
        if self.model_box is None or self.effort_box is None:
            return
        model_id = self.model_box.currentData()
        effort = self.effort_box.currentData()
        if not model_id:
            return
        models.save_selected_model(self.ctx.cfg, self._current_profile(), self.tool, model_id, effort)
        self.ctx.log(f"[{self.tool}] Stärke: {self.effort_box.currentText()} - gilt ab 'Neu starten'")

    # ------------------------------------------------------------ MCP servers (mirrors Dashboard's ServiceRow.mcp)
    def _update_mcp_label(self) -> None:
        if self.mcp_btn is None:
            return
        cfg = self.ctx.cfg
        names = [mcp.servers(cfg)[i].get("name", i).split(" (")[0] for i in mcp.enabled(cfg, self.tool)]
        text = "off" if mcp.is_off(cfg, self.tool) else (", ".join(names) if names else "none")
        self.mcp_btn.setText(f"MCP: {text}  ▾")

    def _mcp_menu(self) -> None:
        cfg = self.ctx.cfg
        menu = QMenu(self)
        on = set(mcp.enabled(cfg, self.tool))
        off = menu.addAction("Start without MCP")
        off.setCheckable(True)
        off.setChecked(mcp.is_off(cfg, self.tool))
        off.setData("__off__")
        menu.addSeparator()
        for sid, s in mcp.servers(cfg).items():
            act = menu.addAction(s.get("name", sid))
            act.setCheckable(True)
            act.setChecked(sid in on)
            act.setData(sid)
        if not menu.actions():
            menu.addAction("keine MCP-Server definiert (config.json -> mcp.servers)").setEnabled(False)
        picked = menu.exec(self.mcp_btn.mapToGlobal(self.mcp_btn.rect().bottomLeft()))
        if picked is None or picked.data() is None:
            return
        sid = picked.data()
        if sid == "__off__":
            mcp.set_off(cfg, self.tool, not mcp.is_off(cfg, self.tool))
            ordered = [] if mcp.is_off(cfg, self.tool) else [i for i in mcp.servers(cfg) if i in on]
        else:
            on.symmetric_difference_update({sid})
            ordered = [i for i in mcp.servers(cfg) if i in on]
            mcp.set_enabled(cfg, self.tool, ordered)
            mcp.set_off(cfg, self.tool, False)
        try:
            cfg.save()
        except OSError as exc:
            self.ctx.log(f"[{self.tool}] MCP-Auswahl nicht gespeichert: {exc}")
        self._update_mcp_label()
        self.ctx.log(f"[{self.tool}] MCP servers: {', '.join(ordered) or '(none)'} - gilt ab 'Neu starten'")

    # ------------------------------------------------------------ cross-tool CLI limit (max_concurrent_clis)
    def _cli_limit_reason(self) -> str | None:
        return self.ctx.procs.cli_limit_reason(self._cli_key())

    def _update_hold_indicator(self) -> None:
        reason = self._cli_limit_reason()
        self.hold_label.setText(f"⏸ {reason}" if reason else "")

    # ------------------------------------------------------------ lifecycle
    def on_show(self) -> None:
        self._update_hold_indicator()
        if not self._started:
            self._started = True
            if pty_available():
                self.restart()

    # ------------------------------------------------------------ one-click dependency install
    def _runtime_python(self) -> str:
        p = self.ctx.cfg.path("runtime") / "python" / "python.exe"
        return str(p) if p.exists() else "python"

    def _install_deps(self) -> None:
        if self._install_proc is not None:
            return
        py = self._runtime_python()
        self.install_btn.setEnabled(False)
        self.state.setText("installiere pywinpty, pyte ...")
        # --only-binary=:all: fails fast with a clear pip error if no matching
        # wheel exists for this Python/arch/Windows combo, instead of silently
        # trying (and usually failing, for lack of a Rust toolchain) a source build.
        args = ["-m", "pip", "install", "--only-binary=:all:", "pywinpty", "pyte"]
        self.ctx.log(f"[{self.tool}] $ \"{py}\" " + " ".join(args))
        proc = QProcess(self)
        proc.setProgram(py)
        proc.setArguments(args)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(
            lambda: self.ctx.log(bytes(proc.readAllStandardOutput()).decode("utf-8", "replace").rstrip()))
        proc.finished.connect(self._install_finished)
        self._install_proc = proc
        proc.start()

    def _install_finished(self, code: int, _status) -> None:
        self._install_proc = None
        self.install_btn.setEnabled(True)
        if code == 0 and pty_available():
            self.ctx.log(f"[{self.tool}] Pakete installiert - Terminal startet ...")
            self._sync_install_btn()
            self.restart()
        else:
            self.ctx.log(f"[{self.tool}] Installation fehlgeschlagen (exit {code}) - siehe Log oben.")
            self.state.setText("Installation fehlgeschlagen")

    def _build_cmd(self) -> tuple[list[str], str] | None:
        bat = self.ctx.cfg.path("cli") / f"{self.tool}.bat"
        if not bat.exists():
            self.state.setText("nicht gefunden: " + str(bat))
            return None
        args = [*profiles.cli_args(self.ctx.cfg, self.tool), *mcp.cli_args(self.ctx.cfg, self.tool)]
        return [str(bat), *args], str(bat.parent)

    def _cli_key(self) -> str:
        # Same key format Dashboard's module rows use for this tool's own interactive-console Start
        # button (see dashboard.py, r.mod.id "cli:<Name>") - sharing it lets ctx.procs.is_running()/stop()
        # here see and control that same process, instead of the two start paths being blind to each other.
        return f"cli:{self.tool}"

    # ---- embedded-owner protocol (called by core.procs.ProcessManager, never by this page) ----
    def embedded_is_running(self) -> bool:
        term = getattr(self, "term", None)
        return bool(term is not None and term.is_running())

    def embedded_stop(self) -> None:
        term = getattr(self, "term", None)
        if term is not None:
            term.stop()

    # ------------------------------------------------------------ refusal / confirmation
    _HOLDER_TEXT = {"embedded": "im eingebetteten Terminal", "console": "in einer externen Konsole",
                    "captured": "über das Dashboard"}

    def _refuse(self, holder: str, what: str) -> None:
        where = self._HOLDER_TEXT.get(holder, "bereits")
        self.state.setText(f"{self.tool} läuft {where} - 'Wechseln' benutzen")
        self.ctx.log(f"[{self.tool}] {what} abgelehnt: läuft bereits {where}. Die laufende Sitzung wird "
                      "nicht automatisch beendet - 'Wechseln' beendet sie ausdrücklich und startet neu.")

    def _confirm(self, text: str) -> bool:
        """Separate method so tests can answer it without a dialog."""
        return QMessageBox.question(self, "Sitzung wechseln", text,
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    # ------------------------------------------------------------ start / stop
    def restart(self) -> None:
        """Restart in THIS tab's embedded terminal. Refuses while the tool is held elsewhere;
        restarting our own session is what the button is for and needs no confirmation."""
        if self._busy:
            return
        self._busy = True
        try:
            reason = self._cli_limit_reason()
            if reason is not None:
                self._update_hold_indicator()     # sole indicator for this - avoids showing it twice in the bar
                self.ctx.log(f"[{self.tool}] Start abgelehnt: {reason}. Zuerst eine andere CLI stoppen.")
                return
            built = self._build_cmd()
            if built is None:
                return
            holder = self.ctx.procs.holder_of(self._cli_key())
            if holder is not None and holder != "embedded":
                self._refuse(holder, "Start im eingebetteten Terminal")
                return
            cmd, cwd = built
            self.ctx.log(f"[{self.tool}] Terminal-Tab: $ {' '.join(cmd)}")
            self.term.start(cmd, cwd, None)      # start() stops our own previous session itself
        finally:
            self._busy = False

    def start_external(self) -> None:
        """Fallback: run this CLI in its own console window instead of the embedded ConPTY terminal -
        for when pywinpty/pyte aren't installed, or the embedded terminal misbehaves for some reason.
        Refused while anything else holds the tool; 'Wechseln' is the deliberate path."""
        if self._busy:
            return
        self._busy = True
        try:
            reason = self._cli_limit_reason()
            if reason is not None:
                self._update_hold_indicator()     # sole indicator for this - avoids showing it twice in the bar
                self.ctx.log(f"[{self.tool}] Start abgelehnt: {reason}. Zuerst eine andere CLI stoppen.")
                return
            built = self._build_cmd()
            if built is None:
                return
            holder = self.ctx.procs.holder_of(self._cli_key())
            if holder is not None:
                self._refuse(holder, "Start in externer Konsole")
                return
            cmd, cwd = built
            self.ctx.log(f"[{self.tool}] Externe Konsole: $ {' '.join(cmd)}")
            if self.ctx.procs.start(self._cli_key(), cmd, cwd, None, title=self.tool, interactive=True):
                self.state.setText("läuft (externe Konsole)")
        finally:
            self._busy = False

    def switch_session(self) -> None:
        """The named switch command: embedded <-> external console. Asks first, then stops the
        running variant in a controlled order and starts the other one - never silently."""
        if self._busy:
            return
        holder = self.ctx.procs.holder_of(self._cli_key())
        if holder is None:
            self.restart()                       # nothing running: just start here
            return
        if holder == "captured":
            self.state.setText(f"{self.tool} wurde über das Dashboard gestartet - dort stoppen")
            self.ctx.log(f"[{self.tool}] Wechsel abgelehnt: vom Dashboard gestartet, dort beenden.")
            return
        to_external = holder == "embedded"
        target = "externe Konsole" if to_external else "eingebettetes Terminal"
        if not self._confirm(f"{self.tool} läuft {self._HOLDER_TEXT[holder]}.\n\n"
                              f"Sitzung beenden und als {target} neu starten?\n"
                              "Der laufende Dialog bzw. eine laufende Aufgabe geht dabei verloren."):
            self.ctx.log(f"[{self.tool}] Wechsel abgebrochen - laufende Sitzung bleibt bestehen.")
            return
        self._busy = True
        try:
            built = self._build_cmd()
            if built is None:
                return
            cmd, cwd = built
            self.ctx.log(f"[{self.tool}] Wechsel -> {target} (laufende Sitzung wird beendet)")
            if to_external:
                # takeover=True: the manager stops the embedded terminal, then starts the console.
                if self.ctx.procs.start(self._cli_key(), cmd, cwd, None, title=self.tool,
                                        interactive=True, takeover=True):
                    self.state.setText("läuft (externe Konsole)")
            else:
                self.ctx.procs.stop(self._cli_key())     # controlled stop of the console first
                self.term.start(cmd, cwd, None)
        finally:
            self._busy = False

    def stop(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            self.ctx.procs.stop(self._cli_key())     # stops embedded AND external in one place
            self.state.setText("gestoppt")
        finally:
            self._busy = False

    def shutdown(self) -> None:
        """GUI shutdown: unconditional, no confirmation - ending everything is the intent here."""
        self.ctx.procs.stop(self._cli_key())
        self.ctx.procs.unregister_embedded(self._cli_key(), self)

    def closeEvent(self, e) -> None:      # noqa: N802
        # Deregistration must not depend on shutdown() being called: a page removed from the
        # stack (or closed on its own) deregisters here, and the manager additionally drops the
        # entry via the weakref / Qt destroyed signal. The RuntimeError guards in core.procs are
        # only for the gap between C++ deletion and that signal.
        self.ctx.procs.unregister_embedded(self._cli_key(), self)
        super().closeEvent(e)
