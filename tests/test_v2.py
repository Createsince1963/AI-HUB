"""Tests for pty_terminal v2.0 (query replies, coalesced rendering, colours, paste, keys,
spawn environment) and for the max_concurrent_clis group limit in core.procs.
No real ConPTY: FakePty stands in for winpty.PtyProcess."""
import os, sys, time, threading
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent, QGuiApplication, QColor
from PySide6.QtWidgets import QApplication
import pyte
from core import procs as P
from ui import pty_terminal as pt

app = QApplication([])
FAILS = []
def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (("  -> " + str(extra)) if extra else ""))
    if not cond: FAILS.append(name)

pt._DIAG_FILE = pt._DIAG_FILE.with_name("terminal_diag_test.log")     # keep the real log clean
pt._kill_tree = lambda pid: None

class FakePty:
    def __init__(self):
        self.pid = os.getpid(); self.alive = True; self.writes = []; self.kill = threading.Event()
    def read(self, n=4096):
        self.kill.wait(5); raise OSError("EOF")
    def write(self, s): self.writes.append(s)
    def isalive(self): return self.alive
    def terminate(self, force=False): self.alive = False; self.kill.set()
    def close(self): pass
    def setwinsize(self, r, c): pass

spawn_kwargs = {}
def make(fake):
    def spawn(*a, **k):
        spawn_kwargs.clear(); spawn_kwargs.update(k); return fake
    pt._pty_imports = lambda: (pyte, type("S", (), {"spawn": staticmethod(spawn)}))
    w = pt.PtyTerminalWidget()
    assert w.start(["x.bat"], None, None)
    return w

def key(k, mods=Qt.NoModifier, txt=""):
    return QKeyEvent(QEvent.KeyPress, k, mods, txt)
T = pt.PtyTerminalWidget._translate

# ---------------------------------------------------------------- 1) terminal queries are answered
f = FakePty(); w = make(f)
w._on_chunk(b"\x1b[6n")
check("CPR query (ESC[6n) is answered with a position report", any(x.startswith("\x1b[") and x.endswith("R") for x in f.writes), f.writes)
w._on_chunk(b"\x1b[c")
check("device-attributes query (ESC[c) is answered", any(x.endswith("c") for x in f.writes), f.writes)
check("queries are recorded for diagnostics", {"\x1b[6n", "\x1b[c"} <= set(w.diagnostics()["queries"]), w.diagnostics())
check("env gets TERM/COLORTERM defaults", spawn_kwargs["env"].get("TERM") == "xterm-256color"
      and spawn_kwargs["env"].get("COLORTERM") == "truecolor", spawn_kwargs.get("env", {}).get("TERM"))

# ---------------------------------------------------------------- 2) render coalescing
renders = []
orig = w._render
w._render = lambda: (renders.append(1), orig())[1]
for i in range(300):
    w._on_chunk(f"line {i}\r\n".encode())
check("300 chunks do NOT trigger 300 repaints before the timer fires", len(renders) == 0, len(renders))
time.sleep(0.08); app.processEvents()
check("one coalesced repaint afterwards", 1 <= len(renders) <= 3, len(renders))
check("screen content is complete after the repaint", "line 299" in w.toPlainText())
w._on_chunk(b"tail"); w._on_ended()
check("session end flushes pending output", "tail" in w.toPlainText())
w.stop()

# ---------------------------------------------------------------- 3) colours
def lum(c): return QColor(c).lightnessF()
check("truecolour white is darkened on the light theme", lum(pt._fg_color("ffffff", False, pt.LIGHT_FG, True)) <= 0.41)
check("truecolour black is lifted on the dark theme", lum(pt._fg_color("000000", False, pt.DARK_FG, False)) >= 0.54)
check("named colours still use the palette", pt._fg_color("red", False, pt.LIGHT_FG, True) == pt.LIGHT_FG["red"])
check("unknown names fall back to default", pt._fg_color("nonsense", False, pt.LIGHT_FG, True) == pt.LIGHT_FG["default"])
check("hue survives clamping (pure green stays greenish)", QColor(pt._fg_color("00ff00", False, pt.LIGHT_FG, True)).green()
      > QColor(pt._fg_color("00ff00", False, pt.LIGHT_FG, True)).red())
f = FakePty(); w = make(f)
w._on_chunk(b"\x1b[38;5;196mred256\x1b[0m \x1b[38;2;10;200;30mtrue\x1b[0m"); w.flush()
check("256/truecolour output renders without error", "red256" in w.toPlainText() and "true" in w.toPlainText())

# ---------------------------------------------------------------- 4) bracketed paste
cb = QGuiApplication.clipboard(); cb.setText("a\nb")
f.writes.clear(); w._paste_clipboard()
check("no bracketed paste unless the CLI enabled it", f.writes == ["a\rb"], f.writes)
w._on_chunk(b"\x1b[?2004h")
check("mode 2004 detected", w._bracketed_paste_enabled())
f.writes.clear(); w._paste_clipboard()
check("bracketed paste wraps the text", f.writes == ["\x1b[200~a\rb\x1b[201~"], f.writes)
w._on_chunk(b"\x1b[?2004l")
check("mode 2004 reset detected", not w._bracketed_paste_enabled())
w.stop()

# ---------------------------------------------------------------- 5) keys
check("Ctrl+Left -> CSI 1;5D", T(key(Qt.Key_Left, Qt.ControlModifier)) == b"\x1b[1;5D")
check("plain Left unchanged", T(key(Qt.Key_Left)) == b"\x1b[D")
check("Shift+Up -> CSI 1;2A", T(key(Qt.Key_Up, Qt.ShiftModifier)) == b"\x1b[1;2A")
check("Ctrl+Delete -> CSI 3;5~", T(key(Qt.Key_Delete, Qt.ControlModifier)) == b"\x1b[3;5~")
check("PageUp/PageDown reach the CLI", T(key(Qt.Key_PageUp)) == b"\x1b[5~" and T(key(Qt.Key_PageDown)) == b"\x1b[6~")
check("Alt+b -> ESC b", T(key(Qt.Key_B, Qt.AltModifier, "b")) == "\x1bb")
check("Shift+Enter / Alt+Enter -> ESC CR", T(key(Qt.Key_Return, Qt.ShiftModifier)) == b"\x1b\r" == T(key(Qt.Key_Return, Qt.AltModifier)))
check("plain Enter -> CR", T(key(Qt.Key_Return)) == b"\r")
check("Ctrl+Space -> NUL", T(key(Qt.Key_Space, Qt.ControlModifier, " ")) == b"\x00")
check("Ctrl+Backspace -> ^W", T(key(Qt.Key_Backspace, Qt.ControlModifier)) == b"\x17")
check("Ctrl+Q (no text) -> 0x11", T(key(Qt.Key_Q, Qt.ControlModifier)) == b"\x11")
AltGr = Qt.ControlModifier | Qt.AltModifier
check("AltGr+Q -> '@' (German layout), NOT Ctrl+Q", T(key(Qt.Key_Q, AltGr, "@")) == "@")
check("AltGr+ss -> backslash", T(key(Qt.Key_ssharp, AltGr, "\\")) == "\\")
check("AltGr+7 -> '{'", T(key(Qt.Key_7, AltGr, "{")) == "{")
check("Ctrl+1 stays with the app (tab navigation)", T(key(Qt.Key_1, Qt.ControlModifier, "1")) is None)
check("F11 still unmapped", T(key(Qt.Key_F11)) is None)
w9 = pt.PtyTerminalWidget()
try:
    w9.keyPressEvent(key(Qt.Key_PageUp, Qt.ShiftModifier)); ok = True
except Exception as exc:
    ok = False
check("Shift+PageUp before any session does not raise", ok)

# ---------------------------------------------------------------- 6) group limit (core.procs)
class Owner:
    def __init__(self): self.running = True
    def embedded_is_running(self): return self.running
    def embedded_stop(self): self.running = False
pm = P.ProcessManager()
check("default limit is 2", pm.max_concurrent_clis == 2)
oc, ox, op = Owner(), Owner(), Owner()
pm.register_embedded("cli:Claude", oc)
check("1 running: second CLI may start", pm.cli_limit_reason("cli:Codex") is None)
check("1 running: other_cli_holder (compat) is None", pm.other_cli_holder("cli:Codex") is None)
pm.register_embedded("cli:Codex", ox)
check("2 running: third CLI is refused", pm.cli_limit_reason("cli:Copilot") is not None)
reason = pm.cli_limit_reason("cli:Copilot")
check("refusal names BOTH running tools and the limit", "Claude" in reason and "Codex" in reason and "2" in reason, reason)
check("a running CLI may restart itself at the limit", pm.cli_limit_reason("cli:Claude") is None)
check("running_cli_keys is sorted", pm.running_cli_keys() == ["cli:Claude", "cli:Codex"])
check("running_cli_keys(exclude=) leaves one out", pm.running_cli_keys(exclude="cli:Claude") == ["cli:Codex"])
check("non-CLI keys are never limited", pm.cli_limit_reason("comfyui") is None)
check("other_cli_holder (compat) reports a blocker when full", pm.other_cli_holder("cli:Copilot") is not None)
blocked = []; pm.blocked.connect(lambda k, r: blocked.append((k, r)))
check("start() refuses when the group is full", pm.start("cli:Copilot", ["x.bat"], None, None) is False and blocked and blocked[0][0] == "cli:Copilot", blocked)
check("takeover does not cross the group limit", pm.start("cli:Copilot", ["x.bat"], None, None, takeover=True) is False)
ox.running = False
check("a freed slot allows the third CLI again", pm.cli_limit_reason("cli:Copilot") is None)
pm.set_max_concurrent_clis(1)
check("limit 1 restores 'one at a time'", pm.cli_limit_reason("cli:Codex") is not None)
pm.set_max_concurrent_clis(0); check("limit is clamped to >= 1", pm.max_concurrent_clis == 1)
pm.set_max_concurrent_clis("garbage"); check("garbage falls back to the default", pm.max_concurrent_clis == 2)
check("any_cli_running still returns a key", pm.any_cli_running() == "cli:Claude")

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
