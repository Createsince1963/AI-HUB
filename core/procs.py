"""Process control.

Three ways a tool can be running, all tracked under ONE key per tool so that no two of them
can start the same CLI twice (the key is e.g. "cli:Claude" for Dashboard row, terminal tab and
external-console fallback alike):
  * captured (default): the process runs without a console window; stdout+stderr are streamed to
    the Processes page (one tab per process) through Qt signals.
  * interactive: own console window (needed for interactive CLIs such as Claude / Codex).
  * embedded: not a child of this manager at all - a ConPTY terminal owned by a UI widget (see
    ui/cli_terminal_page.py). The widget REGISTERS itself here (weakly) under the same key; the
    manager then reports it in is_running() and shuts it down in stop()/stop_all().

start() NEVER takes a running key over silently - it refuses and returns False, because the
thing it would have killed is usually a live CLI conversation or a running agent task. A
deliberate switch goes through start(..., takeover=True), which is only ever reached from a UI
path the user explicitly chose (and confirmed). stop_all() and the GUI shutdown stay
unconditional: there, ending everything IS the intent.

Cross-tool concurrency limit: Claude / Codex / Copilot / Antigravity (keys "cli:<Name>") share
a GROUP capacity of MAX_CONCURRENT_CLIS (default 2), on top of the per-key duplicate-start
guard above. cli_limit_reason() answers "is the group full for this key" so a UI can refuse the
start and name the tools in the way (OnHold). A tool that is already running never counts
against itself, so restarting your own session is always allowed. Non-CLI modules (ComfyUI,
Ollama, ...) are unaffected; only keys under CLI_KEY_PREFIX participate. The limit lives in ONE
place (max_concurrent_clis); set_max_concurrent_clis() changes it at runtime.

Stopping kills the whole process tree.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys

import weakref
from typing import Protocol, runtime_checkable

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

IS_WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def decode(data: bytes) -> str:
    """UTF-8 first (llama.cpp, python), OEM/ANSI code page as fallback (cmd built-ins)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp850" if IS_WIN else "latin-1", "replace")
    return ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def _kill_tree(pid: int) -> None:
    if not pid:
        return
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            import psutil
            p = psutil.Process(pid)
            for c in p.children(recursive=True):
                c.kill()
            p.kill()
        except Exception:      # noqa: BLE001
            pass


def _cpp_alive(obj: object) -> bool:
    """False once Qt has deleted the underlying C++ object of a Python wrapper. shiboken6 ships
    with PySide6; if it is somehow missing, fall back to 'assume alive' (the RuntimeError guards
    still catch the deleted case)."""
    try:
        from shiboken6 import isValid
    except ImportError:      # pragma: no cover - PySide6 always ships shiboken6
        return True
    try:
        return bool(isValid(obj))
    except (TypeError, RuntimeError):
        return True          # not a Qt wrapper at all (plain Python owner, e.g. in tests)


@runtime_checkable
class EmbeddedOwner(Protocol):
    """A UI widget that runs a tool itself (embedded ConPTY terminal) instead of through this
    manager. Deliberately NOT named is_running()/stop(): the owner's own stop() button routes
    through ProcessManager.stop(), so distinct method names make re-entrancy structurally
    impossible instead of relying on a guard flag."""

    def embedded_is_running(self) -> bool: ...
    def embedded_stop(self) -> None: ...


class ProcessManager(QObject):
    spawned = Signal(str, str)        # key, title      (a captured process was started)
    output = Signal(str, str)         # key, text
    ended = Signal(str, int)          # key, exit code
    blocked = Signal(str, str)        # key, reason     (a start was refused: something already holds the key)

    # Key prefix shared by the four CLI-tool rows/tabs ("cli:Claude", "cli:Codex", ...).
    # Only keys under this prefix count toward the concurrent-CLI limit
    # (cli_limit_reason) - non-CLI modules such as ComfyUI or Ollama are unaffected and can
    # run alongside a CLI tool or alongside each other as before.
    CLI_KEY_PREFIX = "cli:"
    # How many CLI tools may run side by side. 2 = e.g. Claude + Codex together; 1 restores the
    # old strict "one at a time" behaviour.
    MAX_CONCURRENT_CLIS = 2

    def __init__(self) -> None:
        super().__init__()
        self.max_concurrent_clis: int = self.MAX_CONCURRENT_CLIS
        self._procs: dict[str, QProcess] = {}
        self._popen: dict[str, subprocess.Popen] = {}     # interactive (own console)
        # weakref.ref, never a strong one: the manager lives in Ctx, the page holds Ctx, so a
        # strong entry here would close the cycle manager -> page -> ctx -> manager and keep a
        # closed tab (and its ConPTY widget) alive for the life of the app.
        self._embedded: dict[str, "weakref.ref[EmbeddedOwner]"] = {}

    # ------------------------------------------------------------------ embedded owners
    def register_embedded(self, key: str, owner: EmbeddedOwner) -> None:
        """A widget that runs `key` itself announces itself here, so every other start path sees
        it as running instead of starting the same tool a second time.

        Lifecycle is handled properly rather than by catching RuntimeError later:
          * a weakref drops the entry when the Python object goes,
          * Qt's own destroyed signal drops it when the C++ object goes (the earlier of the two),
          * an already-deleted C++ object is refused outright.
        The RuntimeError guards elsewhere are only a last line of defence for the window between
        C++ deletion and the destroyed signal being delivered."""
        if not _cpp_alive(owner):
            raise RuntimeError(f"register_embedded({key!r}): owner's C++ object is already deleted")
        self._embedded[key] = weakref.ref(owner)
        destroyed = getattr(owner, "destroyed", None)
        if destroyed is not None:      # QObject: Qt-level deregistration, independent of Python GC
            destroyed.connect(lambda *_a, k=key: self._embedded.pop(k, None))

    def unregister_embedded(self, key: str, owner: EmbeddedOwner | None = None) -> None:
        if owner is None or self._embedded_owner(key) is owner:
            self._embedded.pop(key, None)

    def _embedded_owner(self, key: str) -> "EmbeddedOwner | None":
        ref = self._embedded.get(key)
        if ref is None:
            return None
        owner = ref()
        if owner is None or not _cpp_alive(owner):
            self._embedded.pop(key, None)
            return None
        return owner

    def _embedded_running(self, key: str) -> bool:
        owner = self._embedded_owner(key)
        if owner is None:
            return False
        try:
            return bool(owner.embedded_is_running())
        except RuntimeError:          # deleted between the check above and this call
            self._embedded.pop(key, None)
            return False

    # ------------------------------------------------------------------ state
    def is_running(self, key: str, include_embedded: bool = True) -> bool:
        """include_embedded=False answers "is this tool held by someone OTHER than its embedded
        terminal" - what the terminal tab itself needs before (re)starting its own session."""
        holder = self.holder_of(key)
        return holder is not None and (include_embedded or holder != "embedded")

    def running_keys(self, include_embedded: bool = False) -> list[str]:
        """Background services started BY this manager. Embedded terminal tabs are excluded by
        default: they are visible UI, not a service the user needs to be warned about on exit."""
        keys = list(self._procs) + list(self._popen)
        if include_embedded:
            keys += list(self._embedded)
        seen: list[str] = []
        for k in keys:
            if k not in seen and self.is_running(k, include_embedded=include_embedded):
                seen.append(k)
        return seen

    # ------------------------------------------------------------------ cross-tool CLI limit
    def set_max_concurrent_clis(self, n: int) -> None:
        """Clamp to >= 1 so a bad config value can never make every CLI unstartable."""
        try:
            self.max_concurrent_clis = max(1, int(n))
        except (TypeError, ValueError):
            self.max_concurrent_clis = self.MAX_CONCURRENT_CLIS

    def running_cli_keys(self, exclude: str | None = None) -> list[str]:
        """cli:<Name> keys currently running in ANY mode (embedded / console / captured), sorted
        for stable messages. `exclude` leaves one key out - the one about to (re)start."""
        candidate_keys = set(self._procs) | set(self._popen) | set(self._embedded)
        return sorted(k for k in candidate_keys
                      if k.startswith(self.CLI_KEY_PREFIX) and k != exclude and self.holder_of(k) is not None)

    def cli_limit_reason(self, key: str) -> str | None:
        """None if `key` may start (not a CLI key, or the group has room). Otherwise the user-facing
        reason, naming the tools that fill the group - the single source for every UI message."""
        if not key.startswith(self.CLI_KEY_PREFIX):
            return None
        others = self.running_cli_keys(exclude=key)
        if len(others) < self.max_concurrent_clis:
            return None
        names = ", ".join(k.split(":", 1)[-1] for k in others)
        verb = "läuft" if len(others) == 1 else "laufen"
        return f"{names} {verb} bereits - maximal {self.max_concurrent_clis} CLI gleichzeitig (OnHold)"

    def other_cli_holder(self, key: str) -> tuple[str, str] | None:
        """Kept for existing callers: (blocking_key, holder_type) if the group is FULL for `key`,
        else None. With a limit above 1 a single other running CLI is no longer a blocker."""
        if self.cli_limit_reason(key) is None:
            return None
        k = self.running_cli_keys(exclude=key)[0]
        return (k, self.holder_of(k) or "")

    def any_cli_running(self) -> str | None:
        """First running cli:<Name> key (or None). Prefer running_cli_keys() for a count."""
        keys = self.running_cli_keys()
        return keys[0] if keys else None

    # ------------------------------------------------------------------ start
    def start(self, key: str, cmd: list[str], cwd: str | None, env_extra: dict[str, str] | None,
              title: str | None = None, interactive: bool = False, takeover: bool = False) -> bool:
        """Returns True if a process was started, False if the start was REFUSED because `key` is
        already running somewhere (embedded terminal, own console or captured process), OR
        because the CLI group is full (see cli_limit_reason: at most max_concurrent_clis CLI tools
        run at once).

        Refusing is the point: the previous version stopped the holder silently, which meant a
        Dashboard click could wipe out a live CLI conversation or a running agent task in the
        terminal tab without a word. takeover=True is the deliberate switch, and only UI paths
        the user explicitly chose (and confirmed) pass it. takeover never crosses the group
        limit - when the group is full, freeing a slot is a conscious "stop one first" action the UI
        must ask for explicitly, never an implicit side effect of starting another tool."""
        limit = self.cli_limit_reason(key)
        if limit is not None:
            self.blocked.emit(key, limit)
            return False
        holder = self.holder_of(key)
        if holder is not None:
            if not takeover:
                reason = {"embedded": "läuft bereits im eingebetteten Terminal",
                          "console": "läuft bereits in einer externen Konsole",
                          "captured": "läuft bereits (gestartet über das Dashboard)"}[holder]
                self.blocked.emit(key, reason)
                return False
            self.stop(key)
        if interactive:
            self._start_console(key, cmd, cwd, env_extra)
        else:
            self._start_captured(key, cmd, cwd, env_extra, title or key.split(":")[-1])
        return True

    def holder_of(self, key: str) -> str | None:
        """Which of the three run modes currently holds `key`: 'captured', 'console', 'embedded'
        or None. Used to say WHAT is in the way instead of just refusing."""
        p = self._procs.get(key)
        if p is not None:
            if p.state() != QProcess.NotRunning:
                return "captured"
            self._procs.pop(key, None)
        q = self._popen.get(key)
        if q is not None:
            if q.poll() is None:
                return "console"
            self._popen.pop(key, None)
        return "embedded" if self._embedded_running(key) else None

    def _start_console(self, key, cmd, cwd, env_extra) -> None:
        env = os.environ.copy()
        env.update(env_extra or {})
        flags = subprocess.CREATE_NEW_CONSOLE if IS_WIN else 0
        if IS_WIN and cmd and cmd[0].lower().endswith((".bat", ".cmd")):
            # keep the console open. One quoted command line + /s: with several quoted arguments (paths with spaces,
            # e.g. --mcp-config "F:\@AI Tools\...") plain "cmd /k" strips the wrong quotes and the call breaks.
            line = " ".join(f'"{c}"' for c in cmd)
            self._popen[key] = subprocess.Popen(f'cmd.exe /d /s /k "{line}"', cwd=cwd, env=env, creationflags=flags)
            return
        self._popen[key] = subprocess.Popen(cmd, cwd=cwd, env=env, creationflags=flags)

    def _start_captured(self, key, cmd, cwd, env_extra, title) -> None:
        proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONUTF8", "1")
        for k, v in (env_extra or {}).items():
            env.insert(k, v)
        proc.setProcessEnvironment(env)
        if cwd:
            proc.setWorkingDirectory(cwd)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        # Console window: Qt already starts children with CREATE_NO_WINDOW when this GUI process has no console of
        # its own, and children share the parent's console otherwise - output is captured through the pipe either way.
        # PySide6 does not expose QProcess.setCreateProcessArgumentsModifier (C++ only), so it is used only if present.
        if IS_WIN and hasattr(proc, "setCreateProcessArgumentsModifier"):
            proc.setCreateProcessArgumentsModifier(lambda a: setattr(a, "flags", a.flags | CREATE_NO_WINDOW))
        proc.readyReadStandardOutput.connect(lambda: self.output.emit(key, decode(bytes(proc.readAllStandardOutput()))))
        proc.finished.connect(lambda code, _st: self._finished(key, proc, code))
        proc.errorOccurred.connect(lambda err: self._error(key, proc, err))
        self._procs[key] = proc
        self.spawned.emit(key, title)
        self.output.emit(key, f"$ {' '.join(cmd)}\n")
        if IS_WIN and cmd and cmd[0].lower().endswith((".bat", ".cmd")):
            quoted = " ".join(f'"{c}"' for c in cmd)
            proc.setProgram("cmd.exe")
            proc.setNativeArguments(f'/d /s /c "{quoted}"')
            proc.start()
        else:
            proc.start(cmd[0], cmd[1:])

    def _finished(self, key: str, proc: QProcess, code: int) -> None:
        self.output.emit(key, decode(bytes(proc.readAllStandardOutput())))
        if self._procs.get(key) is proc:
            self._procs.pop(key, None)
        self.ended.emit(key, code)

    def _error(self, key: str, proc: QProcess, err) -> None:
        if err == QProcess.FailedToStart:
            self.output.emit(key, f"ERROR: failed to start ({proc.errorString()})\n")
            if self._procs.get(key) is proc:
                self._procs.pop(key, None)
            self.ended.emit(key, -1)

    # ------------------------------------------------------------------ stop
    def stop(self, key: str) -> None:
        """Stops EVERY way `key` can currently be running - captured process, external console and
        embedded terminal - so a single stop path cannot leave one of the three behind."""
        p = self._procs.pop(key, None)
        if p is not None and p.state() != QProcess.NotRunning:
            self.output.emit(key, "--- stopping ---\n")
            _kill_tree(int(p.processId()))
        q = self._popen.pop(key, None)
        if q is not None and q.poll() is None:
            _kill_tree(q.pid)
        owner = self._embedded_owner(key)
        if owner is not None:
            try:
                owner.embedded_stop()
            except RuntimeError:          # deleted between lookup and call
                self._embedded.pop(key, None)

    def stop_all(self) -> None:
        for k in list(self._procs) + list(self._popen) + list(self._embedded):
            self.stop(k)
