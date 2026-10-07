"""Embedded, real-terminal (ConPTY) widget for CLIs that need an actual TTY -
Claude, Codex, Copilot, Antigravity: arrow-key menus, redraws, colours.

Windows only (pywinpty / ConPTY). Rendering: pyte keeps a correct character
grid (it interprets cursor moves, erase-in-line, colours, ...) and this
widget re-paints that grid into a QPlainTextEdit on every chunk of output -
no raw escape codes ever reach the screen. Colours are remapped for a LIGHT
background by default (the CLIs were written for a dark terminal, so a
literal "bright white" foreground would be invisible on white - see
LIGHT_FG below); a dark mode is one call away (set_light(False)) for CLIs
that assume real terminal darkness for readability of dim/inverse text.

Reviewed against a second opinion (Copilot) that flagged several real
correctness risks; addressed here:
  - .bat must be launched through cmd.exe /d /s /c, not spawned directly
    (Windows CreateProcess cannot exec a .bat on its own).
  - stop() kills the whole process tree (taskkill /T /F), not just the
    PTY's immediate child - cmd.exe hands off to node.exe/the CLI binary,
    which would otherwise survive as an orphan.
  - resize events are debounced (150 ms) instead of hammering the PTY on
    every pixel of a drag-resize; PTY size is set before the pyte screen
    is resized to match.
  - Ctrl+V pastes clipboard text as input instead of sending a raw 0x16
    control byte; Shift+Tab sends back-tab.
  - reverse-video and underline SGR attributes are rendered, not just
    fg/bg/bold.
  - plain PageUp/PageDown go to the CLI (some menus may use them); local
    scrollback is Shift+PageUp/Shift+PageDown or the mouse wheel.

Second pass (technical function test against the running app), two more
confirmed issues fixed:
  - stop() only flagged the reader thread via _stop and dropped the QThread
    reference without ever joining it - the blocking pty.read() could still
    be in flight, so the thread kept running detached (leak; risk of a
    "QThread: Destroyed while thread is still running" abort on GUI close
    with several tabs open). stop() now kills the process tree first (which
    is what actually unblocks the read) and then joins the thread.
  - Strg+L (and any other Strg+<Letter> the app defines as a global
    QShortcut, e.g. the log-dock toggle) never reached the CLI: Qt resolves
    window-wide QShortcuts before an unhandled key event reaches a focused
    child widget's keyPressEvent. A ShortcutOverride handler in event() now
    claims exactly the keys this widget forwards to the CLI while it is
    running, so Strg+L (readline redraw / clear-screen) and friends work
    inside the terminal instead of silently toggling app UI.

Third pass (review of the second pass' own remaining risks):
  - QThread.terminate() is GONE. Killing a thread parked inside winpty's
    read() can leave the winpty handle, the GIL and Qt's signal bookkeeping
    in an undefined state - exactly the kind of crash the join was meant to
    prevent. A thread that refuses to end is instead DETACHED into a module
    level orphan set (keeping the QThread object alive, which is what
    "Destroyed while thread is still running" is actually about) and joined
    once, with a shared budget, at application shutdown - see
    drain_reader_threads().
  - The PTY handle is now closed by the reader thread itself, in its own
    finally block, never by the UI thread while a read() may be in flight
    (close-during-read is a use-after-free inside winpty). The UI thread
    only terminates the PROCESS, which is what makes the pending read
    return EOF.
  - stop() no longer blocks the UI for up to 2 s per tab: the process is
    already dead by then, so a short join (STOP_JOIN_MS) is enough in
    practice, and the rare slow case is detached instead of waited on.
    Closing a window with four live terminal tabs costs ~4 x 0.3 s worst
    case instead of 4 x 2 s.
  - stop() is re-entrancy guarded and takes the reader/pty references out
    of the widget BEFORE doing anything slow, so a second click (Stoppen /
    Neu starten hammered) cannot operate on the same handles twice.
  - _on_chunk/_on_ended tolerate arriving after the widget (or its pyte
    screen) is gone; closeEvent() stops the session, so a destroyed widget
    leaves no thread emitting into a deleted receiver.

Fourth pass (review of the third pass):
  - _ORPHAN_READERS alone does NOT solve the final shutdown. It prevents
    premature destruction *while the app runs*; at interpreter teardown the
    set itself is torn down, and a QThread object destroyed under a still
    running thread aborts with "QThread: Destroyed while thread is still
    running". The documented final strategy is now finalize_process_exit():
    if a reader is still stuck after the drain, the process ends via
    os._exit(), which runs no destructors at all - so the abort cannot
    happen - and lets the OS reclaim the thread and the PTY handle. Still
    no QThread.terminate() anywhere. See that function for the full
    reasoning and its limits.
  - Killing is now identity-checked: the PID is captured at spawn TOGETHER
    with the process creation time (_ProcHandle), and taskkill only runs
    when both still match, so a stale PID from a replaced session can never
    hit a recycled process.
  - Kill ORDER corrected: taskkill /T /F first (it needs the parent alive to
    walk the tree - pywinpty's terminate() ends only the process attached to
    the pseudoconsole, i.e. cmd.exe, and would orphan the node.exe below
    it), terminate(force=True) only as the fallback. The two are no longer
    both run routinely.

Fifth pass (v2.0 - "make the embedded terminal actually usable"; lifecycle code above untouched):
  - Terminal queries are ANSWERED. pyte's Screen.write_process_input() is a no-op by default, so a
    CLI (or ConPTY itself) that asks "where is the cursor?" (ESC[6n) or "what are you?" (ESC[c) got
    no reply and could wait forever -> blank tab / hang at start. _make_screen() now routes pyte's
    replies back into the PTY. Every query and reply is logged to logs/terminal_diag.log, so the
    first real Windows run shows whether this was the cause.
  - Rendering is coalesced: output only marks the grid dirty, one repaint per RENDER_INTERVAL_MS.
    Redraw-heavy TUIs (Ink: Claude/Codex) no longer rebuild the whole document per output chunk.
  - 256-colour and truecolour (pyte reports them as "rrggbb") are rendered instead of silently
    falling back to the default colour, with lightness clamped so they stay readable on the
    light/dark background in use.
  - Input: bracketed paste when the CLI enabled it (mode 2004), modifier-aware cursor/edit keys
    (xterm CSI 1;<mod>X), Alt+<key> as ESC-prefix, Shift/Alt+Enter as newline, Ctrl+Space, PageUp/
    PageDown, Ctrl+Shift+C copy / Shift+Insert paste. AltGr characters on German layouts
    (@ \\ [ ] { } | ~ EUR) are passed through as text: Windows reports AltGr as Ctrl+Alt.
  - TERM=xterm-256color is forced for the child (unless the caller passes its own env), COLORTERM=truecolor
    is added when not already defined.
  - Shift+PageUp/Down no longer raises when no session was started yet.
"""
from __future__ import annotations

import ctypes
import functools
import logging
import os
import re
import subprocess
import sys
import time
import weakref
from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeyEvent, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit

IS_WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000


def _pty_imports():
    """Re-checked on every start() (not just once at module load), so a
    successful in-GUI pip-install (see cli_terminal_page._install_deps)
    takes effect immediately - no app restart needed."""
    try:
        import pyte
        from winpty import PtyProcess
        return pyte, PtyProcess
    except ImportError:
        return None, None


def pty_available() -> bool:
    pyte_mod, pty_cls = _pty_imports()
    return pyte_mod is not None and pty_cls is not None


def _kill_tree(pid: int) -> None:
    """cmd.exe /c hands off to node.exe/the CLI's own binary and WAITS - it
    does not exec-replace itself - so killing only the PTY's direct child
    would leave that real process running as an orphan.

    Callers must have verified the PID's identity first (see _ProcHandle)."""
    if not pid:
        return
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                        creationflags=CREATE_NO_WINDOW)
    else:
        try:
            import psutil
            p = psutil.Process(pid)
            for c in p.children(recursive=True):
                c.kill()
            p.kill()
        except Exception:      # noqa: BLE001
            pass


_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _win_creation_time(pid: int) -> int | None:
    """The process' creation timestamp - the only cheap value that makes a PID unique over time.
    None if it cannot be read (process gone, or access denied)."""
    if not IS_WIN or not pid:
        return None
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        created = (ctypes.c_uint64 * 1)()
        rest = (ctypes.c_uint64 * 3)()
        if not k32.GetProcessTimes(h, ctypes.byref(created), ctypes.byref(rest, 0),
                                    ctypes.byref(rest, 8), ctypes.byref(rest, 16)):
            return None
        return int(created[0])
    finally:
        k32.CloseHandle(h)


def _process_alive(pid: int) -> bool:
    if not pid:
        return False
    if IS_WIN:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class _ProcHandle:
    """A PID bound to ONE session.

    A raw PID is not a stable identity - Windows recycles them, so a stale PID kept from a
    session that has already been replaced could, in principle, make taskkill /T /F hit an
    unrelated (and possibly important) process tree. The creation timestamp captured at spawn
    makes the pair unique: kill_tree() only fires when a process with this PID exists AND was
    created at exactly the same instant.

    The handle is created in start() and swapped out of the widget in stop() together with the
    PTY it belongs to, so no code path can ever see a PID from a previous session."""

    def __init__(self, pid: int | None):
        self.pid = int(pid or 0)
        self.created = _win_creation_time(self.pid)

    def is_same_process(self) -> bool:
        if not self.pid:
            return False
        if self.created is None:                 # identity unverifiable (non-Windows, or no access)
            return _process_alive(self.pid)      # then liveness is the best we have
        return _win_creation_time(self.pid) == self.created

    def kill_tree(self) -> bool:
        """Kill the whole tree, but only if this is still OUR process. Returns False when the
        process is already gone or the PID now belongs to someone else - in both cases there is
        nothing for us to kill, and killing would be wrong."""
        if not self.is_same_process():
            return False
        _kill_tree(self.pid)
        return True


# Foreground palette re-tuned for a WHITE terminal background. The source
# CLIs assume a dark console, so their "white / bright white" (used to mark
# the selected menu row) becomes near-black-bold here instead of invisible.
LIGHT_FG = {
    "default": "#172033", "black": "#172033",
    "red": "#B3261E", "brightred": "#D32F2F",
    "green": "#2E7D32", "brightgreen": "#388E3C",
    "brown": "#8A6D00", "yellow": "#8A6D00", "brightyellow": "#B28900",
    "blue": "#146C94", "brightblue": "#0E5B7F",
    "magenta": "#8E24AA", "brightmagenta": "#AB47BC",
    "cyan": "#00796B", "brightcyan": "#00897B",
    "white": "#102A43", "brightwhite": "#102A43",
    "brightblack": "#3B4B63",
}
LIGHT_BG_DEFAULT = "#FFFFFF"
LIGHT_BG_TINT = "#EEF4FA"          # any explicit non-default background -> light accent tint

# Dark palette: the CLIs' own intended colours, near enough to a standard
# terminal - used when the user switches a tab to "dark mode".
DARK_FG = {
    "default": "#D7E3F1", "black": "#0F1B2A",
    "red": "#FF8A80", "brightred": "#FF6E6E",
    "green": "#9ED0A8", "brightgreen": "#B8E6C1",
    "brown": "#F0B429", "yellow": "#F0B429", "brightyellow": "#FFD666",
    "blue": "#7FB3D5", "brightblue": "#A9CCE3",
    "magenta": "#D7A9E3", "brightmagenta": "#E6C6EF",
    "cyan": "#80CBC4", "brightcyan": "#A2DBD6",
    "white": "#F5F7FA", "brightwhite": "#FFFFFF",
    "brightblack": "#8FA3BC",
}
DARK_BG_DEFAULT = "#0F1B2A"
DARK_BG_TINT = "#1C2C40"
CURSOR_BG_LIGHT = "#D6E8F1"
CURSOR_BG_DARK = "#2C4A63"


PTY_TERMINAL_VERSION = "2.0"
RENDER_INTERVAL_MS = 25      # at most ~40 repaints/s; output between two repaints is merged
NO_OUTPUT_WARN_MS = 5000     # log a warning when a started CLI prints nothing for this long
BRACKETED_PASTE_MODE = 2004 << 5      # pyte stores private modes shifted left by 5 bits

_log = logging.getLogger("pty_terminal")
_DIAG_FILE = Path(__file__).resolve().parents[1] / "logs" / "terminal_diag.log"
_DIAG_MAX_BYTES = 512 * 1024
# Terminal queries a CLI/ConPTY may send and expects an answer to: DSR/CPR (...n), device
# attributes (...c), kitty keyboard query (ESC[?u).
_QUERY_RE = re.compile(r"\x1b\[[?>=]?[0-9;]*[cnu]")
_HEX6_RE = re.compile(r"^[0-9a-fA-F]{6}$")


def _diag(msg: str) -> None:
    """Append one line to logs/terminal_diag.log. Diagnostics must NEVER break the terminal, so
    every failure is swallowed; the file is rotated once at _DIAG_MAX_BYTES (one .1 backup)."""
    try:
        _DIAG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _DIAG_FILE.exists() and _DIAG_FILE.stat().st_size > _DIAG_MAX_BYTES:
            _DIAG_FILE.replace(_DIAG_FILE.with_suffix(".log.1"))
        with _DIAG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [v{PTY_TERMINAL_VERSION}] {msg}\n")
    except Exception:      # noqa: BLE001
        pass


@functools.lru_cache(maxsize=2048)
def _adapt_hex(hex6: str, light: bool) -> str:
    """Make an arbitrary 256/true colour readable on the current background by clamping its
    lightness (dark text on the light theme, not-too-dark text on the dark theme)."""
    c = QColor("#" + hex6)
    h, sat, lum, _a = c.getHslF()
    lum = min(lum, 0.40) if light else max(lum, 0.55)
    c.setHslF(max(h, 0.0), sat, lum)
    return c.name()


def _fg_color(fg, bold: bool, pal: dict, light: bool) -> str:
    if fg == "white" and bold:
        fg = "brightwhite"
    if isinstance(fg, str) and _HEX6_RE.match(fg):
        return _adapt_hex(fg.lower(), light)
    return pal.get(fg, pal["default"])


def _make_screen(pyte_mod, rows: int, cols: int, reply):
    """A pyte HistoryScreen that ANSWERS terminal queries (see module docstring, fifth pass).
    `reply(str)` writes into the PTY; pyte calls it for ESC[6n, ESC[c, ESC[5n, ..."""
    class _ReplyingScreen(pyte_mod.HistoryScreen):
        def write_process_input(self, data):      # noqa: D401 - pyte hook
            reply(data)
    return _ReplyingScreen(cols, rows, history=5000, ratio=0.1)


STOP_JOIN_MS = 300          # short, because stop() kills the process first: the read() returns at once
DRAIN_JOIN_MS = 3000        # total budget for any detached readers at application shutdown


class _ReaderThread(QThread):
    """Blocking PTY reads happen off the UI thread; pyte itself is only ever
    touched from the main thread (via the `chunk` signal).

    This thread OWNS the PTY handle for its whole lifetime: it is the only
    place the handle is read from and the only place it is closed (in the
    finally below, after the read loop has certainly left). The UI thread
    never closes it - it only kills the process, which makes the pending
    read() return EOF and the loop exit on its own."""
    chunk = Signal(bytes)
    ended = Signal()

    def __init__(self, pty: "PtyProcess"):
        super().__init__()
        self.pty = pty
        self._stop = False

    def run(self) -> None:
        try:
            while not (self._stop or self.isInterruptionRequested()):
                try:
                    data = self.pty.read(4096)
                except Exception:      # noqa: BLE001  (EOF / pipe closed / process died)
                    break
                if not data or self._stop or self.isInterruptionRequested():
                    break
                self.chunk.emit(data if isinstance(data, bytes) else data.encode("utf-8", "replace"))
        finally:
            pty, self.pty = self.pty, None
            if pty is not None:
                try:
                    pty.close()
                except Exception:      # noqa: BLE001
                    pass
            self.ended.emit()

    def request_stop(self) -> None:
        self._stop = True
        self.requestInterruption()      # Qt's own flag as well, for anything that inspects it


# Readers that did not finish within STOP_JOIN_MS are parked here instead of being
# terminate()d. Holding the reference is the point: "QThread: Destroyed while thread
# is still running" is about the C++ object dying under a live thread, not about the
# thread outliving its widget. They are joined once, with a shared budget, from
# drain_reader_threads() at application shutdown.
_ORPHAN_READERS: set[_ReaderThread] = set()


def _detach_reader(reader: "_ReaderThread") -> None:
    _ORPHAN_READERS.add(reader)
    reader.finished.connect(lambda r=reader: _ORPHAN_READERS.discard(r))


def readers_still_running() -> int:
    """How many detached readers are still inside a winpty read() right now."""
    return sum(1 for r in _ORPHAN_READERS if r.isRunning())


def finalize_process_exit(code: int = 0) -> int:
    """THE documented final-shutdown strategy. Call it as the very last thing in the entry point,
    after QApplication.exec() has returned (everything that must be persisted has been written by
    then - MainWindow.closeEvent runs long before).

    Why this exists: holding the QThread objects in _ORPHAN_READERS only prevents *premature*
    destruction while the app is running. At interpreter shutdown the set is torn down like any
    other module global, and a QThread whose thread is still running aborts the process from its
    destructor with "QThread: Destroyed while thread is still running". Joining is not an option
    (the thread is parked in a winpty read that never returns) and terminate() is exactly the
    unsafe operation this whole design avoids.

    So: if - and only if - a reader is still stuck after drain_reader_threads(), end the process
    with os._exit(). It runs no destructors, no atexit handlers and no Qt teardown, so the abort
    cannot occur; the OS reclaims the thread, its stack and the PTY handle as it does for any
    process. The trade-off is deliberate and narrow: it is reached only in the pathological case,
    and only after the normal shutdown has already completed.

    Returns `code` unchanged in the normal case (no stuck readers), so the caller can just
    `sys.exit(finalize_process_exit(app.exec()))`.

    NOT yet confirmed on Windows against a real ConPTY - see the module docstring."""
    left = readers_still_running()
    if not left:
        return code
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:      # noqa: BLE001
            pass
    os._exit(code)             # noqa: PLR1722 - deliberate: see above


def drain_reader_threads(timeout_ms: int = DRAIN_JOIN_MS) -> int:
    """Join any detached reader threads once, sharing ONE timeout budget across all of
    them (call from the main window's closeEvent, after every tab has been stopped).
    Returns the number still running - non-zero means a winpty read never returned;
    they are left alone deliberately rather than terminated, and the process then ends
    via finalize_process_exit() instead of through normal teardown. Never called per
    tab, so closing many terminal tabs does not multiply the wait."""
    import time
    deadline = time.monotonic() + timeout_ms / 1000.0
    for reader in list(_ORPHAN_READERS):
        left = int(max(0.0, deadline - time.monotonic()) * 1000)
        if reader.wait(left):
            _ORPHAN_READERS.discard(reader)
    return len(_ORPHAN_READERS)


class PtyTerminalWidget(QPlainTextEdit):
    """A live ConPTY-backed terminal: real arrow-key menus, colours, redraws."""
    started = Signal()
    ended = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._light = True
        self.setObjectName("ConsoleLight")
        self.setReadOnly(True)                 # all input goes through keyPressEvent -> the PTY, never the text buffer
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setCursorWidth(0)
        f = QFont("Cascadia Mono" if IS_WIN else "monospace")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(10)
        self.setFont(f)
        self.setFocusPolicy(Qt.StrongFocus)
        self.pty: "PtyProcess | None" = None
        self.screen = None
        self.stream = None
        self._reader: _ReaderThread | None = None
        self._stopping = False                 # re-entrancy guard: Stoppen/Neu starten hammered
        self._proc: _ProcHandle | None = None   # PID+identity of THIS session, never a stale one
        self._cols, self._rows = 100, 32
        self._cell_w, self._cell_h = 1, 1
        self._measure_cell()
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(150)          # debounce drag-resize: one PTY resize, not one per pixel
        self._resize_timer.timeout.connect(self._apply_resize)
        # Output coalescing: _on_chunk only feeds pyte and marks the grid dirty; this timer repaints.
        self._dirty = False
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(RENDER_INTERVAL_MS)
        self._render_timer.timeout.connect(self._flush_render)
        # Diagnostics (see _diag): did the CLI ever print anything, which queries did it send?
        self._t_start = 0.0
        self._got_output = False
        self._diag_budget = 0
        self._queries_seen: set[str] = set()
        self._replies_sent = 0
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.setInterval(NO_OUTPUT_WARN_MS)
        self._watchdog.timeout.connect(self._no_output_check)

    # ------------------------------------------------------------ light/dark
    def set_light(self, light: bool) -> None:
        self._light = light
        self.setObjectName("ConsoleLight" if light else "Console")
        self.style().unpolish(self)
        self.style().polish(self)
        if self.screen is not None:
            self._render()

    # ------------------------------------------------------------ lifecycle
    def start(self, cmd: list[str], cwd: str | None, env: dict[str, str] | None) -> bool:
        pyte_mod, pty_cls = _pty_imports()
        if pyte_mod is None:
            self.setPlainText(
                "Eingebettetes Terminal nicht verfügbar: die Pakete 'pywinpty' und 'pyte' fehlen "
                "im portablen Python.\n\nÜber den Button 'Pakete installieren' oben installieren - "
                "kein Tippen im Terminal nötig.")
            return False
        self.stop()
        self._cols, self._rows = self._size_to_grid()
        me = weakref.ref(self)      # weak: the screen must not keep the widget alive

        def _reply(data: str) -> None:
            w = me()
            if w is not None:
                w._send_reply(data)
        self.screen = _make_screen(pyte_mod, self._rows, self._cols, _reply)
        self.stream = pyte_mod.Stream(self.screen)
        self._dirty = False
        self._got_output = False
        self._diag_budget = 25
        self._queries_seen = set()
        self._replies_sent = 0
        if env:
            full_env = dict(env)                      # caller-supplied environment is respected
            full_env.setdefault("TERM", "xterm-256color")
        else:
            full_env = dict(os.environ)
            # An inherited TERM can be wrong for what pyte emulates (git-bash/cygwin/dumb): force it.
            full_env["TERM"] = "xterm-256color"
        full_env.setdefault("COLORTERM", "truecolor")
        argv = self._wrap_for_shell(cmd)
        try:
            self.pty = pty_cls.spawn(argv, cwd=cwd, env=full_env or None, dimensions=(self._rows, self._cols))
        except Exception as exc:      # noqa: BLE001
            self.setPlainText(f"Konnte Terminal nicht starten:\n{exc}")
            return False
        pid = None
        try:
            pid = self.pty.pid
        except Exception:      # noqa: BLE001
            pass
        self._proc = _ProcHandle(pid)          # captured now, while the process is certainly ours
        self._t_start = time.monotonic()
        _diag(f"start pid={pid} grid={self._cols}x{self._rows} argv={argv!r}")
        self._watchdog.start()
        reader = _ReaderThread(self.pty)
        reader.chunk.connect(self._on_chunk)
        reader.ended.connect(self._on_ended)
        self._reader = reader                  # published only once fully wired
        reader.start()
        self.started.emit()
        return True

    @staticmethod
    def _wrap_for_shell(cmd: list[str]) -> list[str]:
        """Windows cannot CreateProcess a .bat/.cmd directly - route it through
        cmd.exe /d /s /c like the rest of the launcher (core/procs.py) does.
        Passed as an argv LIST so each element (paths with spaces included)
        gets quoted exactly once by the spawn layer's own command-line
        builder - no manual pre-quoting, no double-quoting risk."""
        if IS_WIN and cmd and cmd[0].lower().endswith((".bat", ".cmd")):
            return ["cmd.exe", "/d", "/s", "/c", *cmd]
        return cmd

    def stop(self) -> None:
        """Order matters, and nothing here may block the UI for long.

        1. take reader + pty OUT of the widget first, under a re-entrancy guard, so a second
           click (or a restart racing a stop) can never act on the same handles twice;
        2. disconnect, so a chunk already queued on the event loop cannot land in _on_chunk
           after self.screen/self.pty have been swapped out from under it;
        3. kill the PROCESS tree - this is what unblocks the reader's pty.read(). The PTY
           HANDLE is deliberately not closed here: the reader thread owns it and closes it
           itself once its loop has left (closing it under an in-flight read is a
           use-after-close inside winpty);
        4. join briefly. The process is already dead, so the read returns immediately in
           practice. A reader that still does not finish is DETACHED, never terminate()d -
           terminate() on a thread parked in winpty leaves handle/GIL/signal state undefined,
           which is precisely the crash class this whole sequence exists to avoid.
        """
        if self._stopping:
            return
        self._stopping = True
        try:
            self._render_timer.stop()
            self._watchdog.stop()
            self._dirty = False
            reader, self._reader = self._reader, None
            pty, self.pty = self.pty, None
            proc, self._proc = self._proc, None      # swapped out with its PTY: no stale PID survives
            if reader is not None:
                for sig, slot in ((reader.chunk, self._on_chunk), (reader.ended, self._on_ended)):
                    try:
                        sig.disconnect(slot)
                    except (TypeError, RuntimeError):      # already disconnected / receiver gone
                        pass
                reader.request_stop()
            if pty is not None:
                # Kill the TREE first, while the parent still exists: taskkill /T walks the live
                # tree, and pywinpty's terminate() ends only the process attached to the
                # pseudoconsole (cmd.exe), which would orphan the node.exe/CLI binary underneath
                # it and leave taskkill nothing to walk. kill_tree() fires only if PID *and*
                # creation time still match this session (see _ProcHandle), so a PID that has
                # since been recycled is never touched.
                killed = proc.kill_tree() if proc is not None else False
                if not killed or self._pty_alive(pty):
                    # Fallback only - not a routine second kill: taskkill unavailable/refused,
                    # non-Windows, or the tree survived it.
                    try:
                        if self._pty_alive(pty):
                            pty.terminate(force=True)
                    except Exception:      # noqa: BLE001
                        pass
                if reader is None:
                    # No reader ever owned this handle (spawn succeeded, thread never started):
                    # then and only then is closing from this thread safe.
                    try:
                        pty.close()
                    except Exception:      # noqa: BLE001
                        pass
            if reader is not None and not reader.wait(STOP_JOIN_MS):
                _detach_reader(reader)
        finally:
            self._stopping = False

    def is_running(self) -> bool:
        return bool(self.pty is not None and self._alive())

    @staticmethod
    def _pty_alive(pty) -> bool:
        try:
            return bool(pty.isalive())
        except Exception:      # noqa: BLE001
            return False

    def _alive(self) -> bool:
        pty = self.pty
        if pty is None:
            return False
        try:
            return pty.isalive()
        except Exception:      # noqa: BLE001
            return False

    # ------------------------------------------------------------ output
    def _on_chunk(self, data: bytes) -> None:
        # A chunk can still be in the event queue when the session is torn down (stop()
        # disconnects, but an already-queued emission is delivered) or when the widget is on
        # its way out - both are normal, neither may raise into the event loop.
        if self.stream is None or self._stopping:
            return
        try:
            text = data.decode("utf-8", "replace")
            if not self._got_output:
                self._got_output = True
                self._watchdog.stop()
                _diag(f"first output after {(time.monotonic() - self._t_start) * 1000:.0f} ms: {text[:200]!r}")
            self._note_queries(text)
            self.stream.feed(text)
            self._dirty = True
            if not self._render_timer.isActive():
                self._render_timer.start()      # coalesce: one repaint per RENDER_INTERVAL_MS
        except RuntimeError:          # widget's C++ object deleted between queueing and delivery
            pass

    def _flush_render(self) -> None:
        """Repaint now if output arrived since the last repaint (also used by tests/diagnostics)."""
        if not self._dirty or self.screen is None or self._stopping:
            return
        self._dirty = False
        try:
            self._render()
        except RuntimeError:          # C++ object already gone
            pass

    flush = _flush_render

    def _note_queries(self, text: str) -> None:
        for m in _QUERY_RE.findall(text):
            if m not in self._queries_seen:
                self._queries_seen.add(m)
                _diag(f"terminal query from CLI: {m!r}")

    def _send_reply(self, data: str) -> None:
        """pyte's answer to a terminal query goes back into the PTY (see module docstring)."""
        pty = self.pty
        if pty is None or self._stopping:
            return
        try:
            pty.write(data)
            self._replies_sent += 1
            if self._diag_budget > 0:
                self._diag_budget -= 1
                _diag(f"reply sent to terminal query: {data!r}")
        except Exception as exc:      # noqa: BLE001
            _diag(f"reply FAILED {data!r}: {exc}")

    def _no_output_check(self) -> None:
        if self.pty is not None and not self._got_output and self._alive():
            _diag(f"WARNING: no output {NO_OUTPUT_WARN_MS} ms after start (queries seen: "
                  f"{sorted(self._queries_seen) or 'none'}, replies sent: {self._replies_sent})")

    def diagnostics(self) -> dict:
        return {"version": PTY_TERMINAL_VERSION, "got_output": self._got_output,
                "queries": sorted(self._queries_seen), "replies_sent": self._replies_sent,
                "log": str(_DIAG_FILE)}

    def _on_ended(self) -> None:
        if self._stopping:            # we are the ones ending it; the UI already knows
            return
        self._flush_render()          # last output must not be lost to the coalescing timer
        _diag("session ended (reader reached EOF)")
        try:
            self.ended.emit(0)
        except RuntimeError:
            pass

    def closeEvent(self, e) -> None:      # noqa: N802
        # A widget destroyed with a live session would leave a reader thread emitting into a
        # deleted receiver; stopping here makes that structurally impossible.
        self.stop()
        super().closeEvent(e)

    def _render(self) -> None:
        if self.screen is None:
            return
        fg_pal  = LIGHT_FG if self._light else DARK_FG
        bg_tint = LIGHT_BG_TINT if self._light else DARK_BG_TINT
        cursor_bg = CURSOR_BG_LIGHT if self._light else CURSOR_BG_DARK
        self.setUpdatesEnabled(False)
        cur = QTextCursor(self.document())
        cur.select(QTextCursor.Document)
        cur.removeSelectedText()
        cx, cy = self.screen.cursor.x, self.screen.cursor.y
        show_cursor = not self.screen.cursor.hidden and self._alive()
        for row_i in range(self._rows):
            line = self.screen.buffer[row_i]
            col = 0
            while col < self._cols:
                ch = line[col]
                fg, bg, bold, rev, under = ch.fg, ch.bg, ch.bold, ch.reverse, ch.underscore
                run = ch.data or " "
                col += 1
                while col < self._cols:
                    nxt = line[col]
                    if (nxt.fg, nxt.bg, nxt.bold, nxt.reverse, nxt.underscore) == (fg, bg, bold, rev, under) \
                            and not (show_cursor and row_i == cy and col == cx):
                        run += nxt.data or " "
                        col += 1
                    else:
                        break
                if rev:
                    fg, bg = (bg if bg not in ("default", None) else "white"), "default"
                fmt = QTextCharFormat()
                color = _fg_color(fg, bold, fg_pal, self._light)
                fmt.setForeground(QColor(color))
                if bold:
                    fmt.setFontWeight(QFont.Bold)
                if under:
                    fmt.setFontUnderline(True)
                if rev or bg not in ("default", None):
                    fmt.setBackground(QColor(bg_tint))
                cur.insertText(run, fmt)
            if row_i < self._rows - 1:
                cur.insertText("\n", QTextCharFormat())
        if show_cursor and 0 <= cy < self._rows and 0 <= cx < self._cols:
            block = self.document().findBlockByNumber(cy)
            if block.isValid() and cx < block.length():
                mark = QTextCursor(self.document())
                mark.setPosition(block.position() + cx)
                mark.setPosition(block.position() + min(cx + 1, block.length() - 1), QTextCursor.KeepAnchor)
                fmt = QTextCharFormat()
                fmt.setBackground(QColor(cursor_bg))
                mark.mergeCharFormat(fmt)
        self.setUpdatesEnabled(True)
        self.moveCursor(QTextCursor.End)

    # ------------------------------------------------------------ input
    def keyPressEvent(self, e: QKeyEvent) -> None:      # noqa: N802
        # Local scrollback (view-only, does not touch the PTY): Shift+PageUp/Down.
        # Plain PageUp/PageDown go to the CLI - some menus use them themselves.
        if e.key() == Qt.Key_PageUp and e.modifiers() & Qt.ShiftModifier:
            if self.screen is not None:
                self.screen.prev_page(); self._render()
            return
        if e.key() == Qt.Key_PageDown and e.modifiers() & Qt.ShiftModifier:
            if self.screen is not None:
                self.screen.next_page(); self._render()
            return
        mods = e.modifiers()
        if mods & Qt.ControlModifier and mods & Qt.ShiftModifier and e.key() == Qt.Key_C:
            self._copy_selection()          # Ctrl+C itself must stay an interrupt for the CLI
            return
        if self.pty is None or not self._alive():
            return
        if (mods & Qt.ControlModifier and e.key() == Qt.Key_V) or \
                (mods & Qt.ShiftModifier and e.key() == Qt.Key_Insert):
            self._paste_clipboard()
            return
        seq = self._translate(e)
        if seq is not None:
            try:
                self.pty.write(seq.decode("latin-1") if isinstance(seq, bytes) else seq)
            except Exception:      # noqa: BLE001
                pass

    def _copy_selection(self) -> None:
        text = self.textCursor().selectedText().replace("\u2029", "\n")
        if text:
            QGuiApplication.clipboard().setText(text)

    def _bracketed_paste_enabled(self) -> bool:
        return self.screen is not None and BRACKETED_PASTE_MODE in self.screen.mode

    def _paste_clipboard(self) -> None:
        """Paste as ONE unit when the CLI enabled bracketed paste (mode 2004), so multi-line text
        is not interpreted as typed Enter keys; otherwise plain text with CR line ends."""
        text = QGuiApplication.clipboard().text()
        if not text or self.pty is None:
            return
        text = text.replace("\r\n", "\r").replace("\n", "\r")
        if self._bracketed_paste_enabled():
            text = "\x1b[200~" + text + "\x1b[201~"
        try:
            self.pty.write(text)
        except Exception:      # noqa: BLE001
            pass

    def event(self, e) -> bool:      # noqa: N802
        # Without this, a running CLI never sees Strg+L (readline redraw / clear-screen - one of the
        # most common terminal control chars) because MainWindow registers it as an app-wide QShortcut
        # (log dock toggle) with QMainWindow's default WindowShortcut context, which fires regardless of
        # which child widget has focus; Qt resolves that *before* the key would reach our keyPressEvent,
        # via the ShortcutOverride event below. Same mechanism would swallow any other Strg+<Letter> the
        # global shortcuts define. Claim only the keys we actually forward to the CLI (checked with the
        # same _translate()/PageUp-PageDown logic keyPressEvent uses) so unrelated shortcuts (Strg+1..9
        # tab navigation, F11 fullscreen, ...) keep working normally while a terminal tab has focus.
        if e.type() == QEvent.ShortcutOverride and self.pty is not None and self._alive():
            if e.key() in (Qt.Key_PageUp, Qt.Key_PageDown) or self._translate(e) is not None:
                e.accept()
                return True
        return super().event(e)

    def wheelEvent(self, e) -> None:      # noqa: N802
        # The document is always exactly self._rows tall by design (see
        # _render), so ordinary scrolling has nothing to do - repurpose the
        # wheel for pyte's own history scrollback instead.
        if self.screen is None:
            return
        steps = e.angleDelta().y() // 120
        for _ in range(abs(steps)):
            self.screen.prev_page() if steps > 0 else self.screen.next_page()
        if steps:
            self._render()
        e.accept()

    @staticmethod
    def _translate(e: QKeyEvent) -> bytes | str | None:
        k, mods, t = e.key(), e.modifiers(), e.text()
        ctrl = bool(mods & Qt.ControlModifier)
        alt = bool(mods & Qt.AltModifier)
        shift = bool(mods & Qt.ShiftModifier)
        # AltGr (German layout: @ \\ [ ] { } | ~ EUR): Windows reports AltGr as Ctrl+Alt together with
        # a PRINTABLE text. Pass the character through - treating it as a control key would type
        # Ctrl+Q instead of "@" and break every path with a backslash.
        if ctrl and alt and t and t.isprintable():
            return t
        if ctrl and not alt and Qt.Key_A <= k <= Qt.Key_Z:
            return bytes([k - Qt.Key_A + 1])
        if k == Qt.Key_Backtab or (k == Qt.Key_Tab and shift):
            return b"\x1b[Z"
        if ctrl and k == Qt.Key_Space:
            return b"\x00"
        if k == Qt.Key_Backspace:
            return b"\x17" if ctrl else (b"\x1b\x7f" if alt else b"\x7f")
        if k in (Qt.Key_Return, Qt.Key_Enter):
            return b"\x1b\r" if (shift or alt) else b"\r"      # Shift/Alt+Enter = newline in the CLI input
        # xterm modifier parameter: 1 + shift(1) + alt(2) + ctrl(4); plain keys keep the short form.
        m = 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0)
        csi_final = {Qt.Key_Up: "A", Qt.Key_Down: "B", Qt.Key_Right: "C", Qt.Key_Left: "D",
                     Qt.Key_Home: "H", Qt.Key_End: "F"}
        csi_tilde = {Qt.Key_Insert: 2, Qt.Key_Delete: 3, Qt.Key_PageUp: 5, Qt.Key_PageDown: 6}
        if k in csi_final:
            return f"\x1b[1;{m}{csi_final[k]}".encode() if m > 1 else f"\x1b[{csi_final[k]}".encode()
        if k in csi_tilde:
            return f"\x1b[{csi_tilde[k]};{m}~".encode() if m > 1 else f"\x1b[{csi_tilde[k]}~".encode()
        table = {
            Qt.Key_Escape: b"\x1b", Qt.Key_Tab: b"\t",
            # F11 deliberately left unmapped: CLIs here don't use it, and leaving it alone means the
            # global F11 fullscreen-toggle shortcut still works even while a terminal tab has focus.
            Qt.Key_F1: b"\x1bOP", Qt.Key_F2: b"\x1bOQ", Qt.Key_F3: b"\x1bOR", Qt.Key_F4: b"\x1bOS",
            Qt.Key_F5: b"\x1b[15~", Qt.Key_F6: b"\x1b[17~", Qt.Key_F7: b"\x1b[18~", Qt.Key_F8: b"\x1b[19~",
            Qt.Key_F9: b"\x1b[20~", Qt.Key_F10: b"\x1b[21~", Qt.Key_F12: b"\x1b[24~",
        }
        if k in table:
            return table[k]
        if alt and not ctrl and t and t.isprintable():
            return "\x1b" + t                       # Alt+<key> = ESC prefix (readline/Ink word shortcuts)
        if ctrl:
            return None      # any other Ctrl combo (e.g. Strg+1..9 tab navigation) stays with the application
        return t if t else None

    # ------------------------------------------------------------ sizing
    def _measure_cell(self) -> None:
        fm = self.fontMetrics()
        self._cell_w = max(1, fm.horizontalAdvance("M"))
        self._cell_h = max(1, fm.lineSpacing())

    def _size_to_grid(self) -> tuple[int, int]:
        self._measure_cell()
        w, h = max(1, self.viewport().width()), max(1, self.viewport().height())
        cols = max(40, w // self._cell_w)
        rows = max(10, h // self._cell_h)
        return cols, rows

    def resizeEvent(self, e) -> None:      # noqa: N802
        super().resizeEvent(e)
        if self.screen is None or self.pty is None:
            return
        self._resize_timer.start()         # coalesce: only the last resize in a burst actually touches the PTY

    def _apply_resize(self) -> None:
        if self.screen is None or self.pty is None:
            return
        cols, rows = self._size_to_grid()
        if (cols, rows) == (self._cols, self._rows):
            return
        self._cols, self._rows = cols, rows
        try:
            self.pty.setwinsize(rows, cols)    # PTY first, then the local pyte grid, per pywinpty's own convention
        except Exception:      # noqa: BLE001
            pass
        self.screen.resize(rows, cols)
        self._render()
