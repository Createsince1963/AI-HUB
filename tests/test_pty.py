"""Simulated tests for the ConPTY terminal lifecycle. No real ConPTY (Linux container):
a FakePty stands in for winpty.PtyProcess, including the pathological case of a read()
that never returns until the process is 'killed'."""
import os, sys, threading, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QEvent, Qt, QCoreApplication
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication
import pyte
from ui import pty_terminal as pt

app = QApplication([])
FAILS = []
def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (("  -> " + str(extra)) if extra else ""))
    if not cond: FAILS.append(name)

class FakePty:
    """block=True -> read() blocks forever until kill_event is set (simulates a winpty read
    that only returns when the process dies). closed/close_calls track handle ownership."""
    def __init__(self, block=False, data=None):
        self.pid = os.getpid()          # a PID that really exists: _ProcHandle verifies identity
        self.alive = True
        self.closed = False
        self.close_calls = []          # (thread name) per close()
        self.read_thread = None
        self._block = block
        self._data = list(data or [])
        self.kill_event = threading.Event()
        self.terminated = False
    def read(self, n=4096):
        self.read_thread = threading.current_thread().name
        if self.closed:
            raise OSError("closed")
        if self._data:
            time.sleep(0.01)
            return self._data.pop(0)
        if self._block:
            self.kill_event.wait(10)
            raise OSError("EOF after kill")
        return b""
    def write(self, s): pass
    def isalive(self): return self.alive
    def terminate(self, force=False):
        self.terminated = True
        self.alive = False
        self.kill_event.set()
    def close(self):
        self.closed = True
        self.close_calls.append(threading.current_thread().name)
    def setwinsize(self, r, c): pass

killed = []
pt._kill_tree = lambda pid: killed.append(pid)

def make_widget(fake):
    w = pt.PtyTerminalWidget()
    pt._pty_imports = lambda: (pyte, type("S", (), {"spawn": staticmethod(lambda *a, **k: fake)}))
    ok = w.start(["x.bat"], None, None)
    return w, ok

# ---------------------------------------------------------------- 1) normal stop
fake = FakePty(data=[b"hello\r\n"])
w, ok = make_widget(fake)
check("start() spawns and returns True", ok)
time.sleep(0.2); app.processEvents()
check("output reached the pyte screen", "hello" in w.toPlainText(), repr(w.toPlainText()[:20]))
t0 = time.monotonic(); w.stop(); dt = (time.monotonic()-t0)*1000
check("stop() joins quickly (< STOP_JOIN_MS)", dt < pt.STOP_JOIN_MS, f"{dt:.0f} ms")
check("stop() killed the process tree (identity verified)", killed == [os.getpid()], killed)
check("PTY closed exactly once", fake.close_calls.count(threading.current_thread().name)==0 and len(fake.close_calls)==1, fake.close_calls)
check("PTY closed by the READER thread, not the UI thread", fake.close_calls and fake.close_calls[0] != threading.main_thread().name, fake.close_calls)
check("no orphan reader left", len(pt._ORPHAN_READERS) == 0)
check("stop() is idempotent", (w.stop() or True) and len(fake.close_calls) == 1)
check("is_running() False after stop", not w.is_running())

# ---------------------------------------------------------------- 2) reader that refuses to end
killed.clear()
fake2 = FakePty(block=True)
fake2.kill_event = threading.Event()
orig_term = fake2.terminate
fake2.terminate = lambda force=False: setattr(fake2, "alive", False)   # kill does NOT release read()
w2, _ = make_widget(fake2)
time.sleep(0.1)
t0 = time.monotonic(); w2.stop(); dt = (time.monotonic()-t0)*1000
check("stop() does not hang on a stuck reader", dt < pt.STOP_JOIN_MS + 250, f"{dt:.0f} ms")
check("stuck reader was DETACHED, not terminated", len(pt._ORPHAN_READERS) == 1, len(pt._ORPHAN_READERS))
check("detached reader object is still referenced (no 'Destroyed while running')",
      all(not r.isFinished() for r in pt._ORPHAN_READERS))
check("widget dropped its own references", w2.pty is None and w2._reader is None)
t0 = time.monotonic(); left = pt.drain_reader_threads(300); dt=(time.monotonic()-t0)*1000
check("drain uses ONE shared budget when the thread stays stuck", left == 1 and dt < 500, f"left={left} {dt:.0f} ms")
fake2.kill_event.set(); time.sleep(0.2); app.processEvents()
left = pt.drain_reader_threads(1000)
check("drain joins the reader once it finally returns", left == 0, left)
check("detached reader still closed its own PTY handle", fake2.closed and fake2.close_calls[0] != threading.main_thread().name, fake2.close_calls)

# ---------------------------------------------------------------- 3) hammered stop/restart
fake3 = FakePty(block=True)
w3, _ = make_widget(fake3)
time.sleep(0.05)
for _ in range(5):
    w3.stop()
check("5x stop in a row -> one close, no crash", fake3.close_calls and len(fake3.close_calls) == 1, fake3.close_calls)
pt.drain_reader_threads(1000)

# restart storm
fakes = []
for i in range(4):
    f = FakePty(data=[b"x"]); fakes.append(f)
    pt._pty_imports = lambda f=f: (pyte, type("S", (), {"spawn": staticmethod(lambda *a, **k: f)}))
    w3.start(["x.bat"], None, None)
    time.sleep(0.02)
w3.stop()
check("restart storm: every PTY handle closed exactly once",
      all(len(f.close_calls) == 1 for f in fakes), [len(f.close_calls) for f in fakes])
check("restart storm leaves no orphans", pt.drain_reader_threads(1000) == 0)

# ---------------------------------------------------------------- 4) late signals after teardown
fake4 = FakePty(data=[b"a", b"b", b"c"])
w4, _ = make_widget(fake4)
time.sleep(0.02)
reader = w4._reader
w4.stop()
reader.chunk.emit(b"late")          # a queued chunk delivered after teardown (nothing connected)
w4._on_chunk(b"late")               # and the worst case: slot invoked directly
app.processEvents()
check("late chunk after stop() does not raise", True)
w4.screen = None
w4._on_chunk(b"late")
check("chunk with no screen/stream is ignored", True)

# ---------------------------------------------------------------- 5) key handling (regression)
def key(k, mods=Qt.NoModifier, txt=""):
    return QKeyEvent(QEvent.KeyPress, k, mods, txt)
check("Strg+L -> 0x0c", pt.PtyTerminalWidget._translate(key(Qt.Key_L, Qt.ControlModifier)) == b"\x0c")
check("F5 -> \\x1b[15~", pt.PtyTerminalWidget._translate(key(Qt.Key_F5)) == b"\x1b[15~")
check("Insert -> \\x1b[2~", pt.PtyTerminalWidget._translate(key(Qt.Key_Insert)) == b"\x1b[2~")
check("F11 stays unmapped (global fullscreen)", pt.PtyTerminalWidget._translate(key(Qt.Key_F11)) is None)

# ShortcutOverride only claimed while a session is live
fake5 = FakePty(block=True)
w5, _ = make_widget(fake5)
ev = QKeyEvent(QEvent.ShortcutOverride, Qt.Key_L, Qt.ControlModifier, "")
w5.event(ev)
check("ShortcutOverride for Strg+L IS claimed while running", ev.isAccepted())
w5.stop(); pt.drain_reader_threads(1000)
ev2 = QKeyEvent(QEvent.ShortcutOverride, Qt.Key_L, Qt.ControlModifier, "")
w5.event(ev2)
check("ShortcutOverride NOT claimed after stop", not ev2.isAccepted())

# ---------------------------------------------------------------- 6) closeEvent stops the session
fake6 = FakePty(block=True)
w6, _ = make_widget(fake6)
time.sleep(0.05)
w6.close()
check("closeEvent() tears the session down", w6.pty is None and w6._reader is None)
pt.drain_reader_threads(1000)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
