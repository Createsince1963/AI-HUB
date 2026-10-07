"""HUB Terminal: one sidebar page that hosts every terminal-like view as a sub-tab.

Sub-tabs (tab bar sits next to the page title):
  Shell        the integrated command line (cmd / PowerShell / runtime Python) - always present
  Processes    one inner tab per started service / updater / installer with live log + progress -
               appears as soon as the first process is started
  Claude, Codex, Copilot, Antigravity
               embedded CLI terminals - created (and started) when the tool is started, closed with the
               tab's close button (which stops the process)

No paths live here: everything is resolved through ctx.cfg by the pages themselves."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QMessageBox, QStackedWidget, QTabBar, QVBoxLayout, QWidget

from .cli_terminal_page import CliTerminalPage
from .context import Ctx
from .widgets import page_header

SHELL, PROCESSES = "shell", "processes"


class HubTerminalPage(QWidget):
    def __init__(self, ctx: Ctx, shell: QWidget, processes: QWidget, cli_tools: list[str]):
        super().__init__()
        self.setObjectName("Page")
        self.ctx = ctx
        self.cli_tools = list(cli_tools)
        self.pages: dict[str, QWidget] = {}
        self._rank = [SHELL, PROCESSES] + [f"cli:{t}" for t in self.cli_tools]    # fixed tab order

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 10)
        top = QHBoxLayout()
        top.setSpacing(14)
        top.addWidget(page_header("HUB Terminal", "Shell, processes and CLI terminals as sub-tabs"))
        self.bar = QTabBar()
        self.bar.setDocumentMode(True)
        self.bar.setExpanding(False)
        self.bar.setDrawBase(False)
        self.bar.setTabsClosable(True)
        self.bar.setMovable(False)
        self.bar.currentChanged.connect(self._bar_changed)
        self.bar.tabCloseRequested.connect(self._close_tab)
        top.addWidget(self.bar, 1, Qt.AlignBottom)
        lay.addLayout(top)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)

        self._add(SHELL, "Shell", shell)
        self._processes = processes
        self.pages[PROCESSES] = processes             # built up-front (it listens to ProcessManager), tab shown lazily
        self.stack.addWidget(processes)
        ctx.procs.spawned.connect(self._on_spawned)

        self._dots = QTimer(self)                      # "●" marker on tabs whose process is running
        self._dots.setInterval(1000)
        self._dots.timeout.connect(self._refresh_titles)
        self._dots.start()

    # ------------------------------------------------------------------ tab management
    def _index_of(self, key: str) -> int:
        for i in range(self.bar.count()):
            if self.bar.tabData(i) == key:
                return i
        return -1

    def _add(self, key: str, title: str, widget: QWidget) -> None:
        if key not in self.pages:
            self.pages[key] = widget
            self.stack.addWidget(widget)
        if self._index_of(key) >= 0:
            return
        pos = sum(1 for i in range(self.bar.count()) if self._rank.index(self.bar.tabData(i)) < self._rank.index(key))
        self.bar.blockSignals(True)
        i = self.bar.insertTab(pos, title)
        self.bar.setTabData(i, key)
        if not key.startswith("cli:"):                 # Shell / Processes stay; only CLI tabs can be closed
            self.bar.setTabButton(i, QTabBar.RightSide, None)
        self.bar.blockSignals(False)

    def _select(self, key: str) -> None:
        i = self._index_of(key)
        if i < 0:
            return
        self.bar.blockSignals(True)
        self.bar.setCurrentIndex(i)
        self.bar.blockSignals(False)
        self._show(key)

    def _bar_changed(self, i: int) -> None:
        if i >= 0:
            self._show(self.bar.tabData(i))

    def _show(self, key: str) -> None:
        w = self.pages[key]
        self.stack.setCurrentWidget(w)
        if hasattr(w, "on_show"):
            w.on_show()

    def _refresh_titles(self) -> None:
        procs = self.ctx.procs
        for i in range(self.bar.count()):
            key = self.bar.tabData(i)
            if key.startswith("cli:"):
                page = self.pages.get(key)
                running = bool(page is not None and page.embedded_is_running())
                self.bar.setTabText(i, ("● " if running else "○ ") + key.split(":", 1)[1])
            elif key == PROCESSES:
                n = len([k for k in procs.running_keys() if not k.startswith("cli:")])
                self.bar.setTabText(i, f"● Processes ({n})" if n else "Processes")

    # ------------------------------------------------------------------ public API (called by MainWindow.goto)
    def on_show(self) -> None:
        i = self.bar.currentIndex()
        if i >= 0:
            self._show(self.bar.tabData(i))

    def open_processes(self) -> None:
        self._add(PROCESSES, "Processes", self._processes)
        self._select(PROCESSES)

    def open_cli(self, tool: str, start: bool = False) -> None:
        """Create the sub-tab for `tool` on first use and show it. A fresh page starts its terminal in on_show();
        start=True additionally restarts an existing, stopped one (Dashboard 'Start')."""
        key = f"cli:{tool}"
        fresh = key not in self.pages
        if fresh:
            self._add(key, tool, CliTerminalPage(self.ctx, tool, header=False))
        else:
            self._add(key, tool, self.pages[key])
        self._select(key)
        page = self.pages[key]
        if start and not fresh and not page.embedded_is_running():
            page.restart()
        self._refresh_titles()

    def _on_spawned(self, _key: str, _title: str) -> None:
        """A captured process was started anywhere -> make sure the Processes sub-tab exists and is selected
        (only inside this page; the sidebar selection is left to whoever started the process)."""
        self._add(PROCESSES, "Processes", self._processes)
        self._select(PROCESSES)
        self._refresh_titles()

    def _close_tab(self, i: int) -> None:
        key = self.bar.tabData(i)
        if not key.startswith("cli:"):
            return
        page = self.pages[key]
        if page.embedded_is_running():
            if QMessageBox.question(self, f"Close {key.split(':', 1)[1]}",
                                    "The session in this tab is still running and will be stopped.\nClose anyway?"
                                    ) != QMessageBox.Yes:
                return
        page.shutdown()
        self.bar.blockSignals(True)
        self.bar.removeTab(i)
        self.bar.blockSignals(False)
        self.stack.removeWidget(page)
        self.pages.pop(key, None)
        page.deleteLater()
        j = min(i, self.bar.count() - 1)
        if j >= 0:
            self._select(self.bar.tabData(j))
        self.ctx.procs.unregister_embedded(f"cli:{key.split(':', 1)[1]}", page)

    # ------------------------------------------------------------------ lifecycle / theme
    def shutdown(self) -> None:
        for key, page in list(self.pages.items()):
            if hasattr(page, "shutdown"):
                page.shutdown()

    def apply_theme(self) -> None:
        for page in self.pages.values():
            if hasattr(page, "apply_theme"):
                page.apply_theme()
