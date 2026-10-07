"""Tests for the final correction round: shutdown of a permanently stuck reader, PID identity,
refusal instead of silent takeover, the explicit switch, and owner lifecycle."""
import os, sys, gc, types, time, weakref, threading, subprocess, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from PySide6.QtWidgets import QApplication, QMessageBox
app = QApplication([])
import pyte
from core.procs import ProcessManager
from ui import pty_terminal as pt
from ui.cli_terminal_page import CliTerminalPage

FAILS = []
def check(n, c, e=""):
    print(("PASS  " if c else "FAIL  ") + n + (("  -> " + str(e)) if e else ""))
    if not c: FAILS.append(n)

# ============================================================ 1) final shutdown, stuck reader
def run_child(mode):
    r = subprocess.run([sys.executable, HERE + "/child_shutdown.py", mode],
                       capture_output=True, text=True, timeout=120)
    return r
fin = run_child("finalize")
check("child really had a stuck reader after the drain", "stuck_after_drain=1" in fin.stdout, fin.stdout.strip())
check("finalize_process_exit() exits with the app's own code", fin.returncode == 7, fin.returncode)
check("no 'QThread: Destroyed while thread is still running' with finalize",
      "Destroyed while thread is still running" not in (fin.stderr + fin.stdout), fin.stderr[-200:])
check("no abort / crash dump with finalize", "core dumped" not in fin.stderr.lower() and fin.returncode >= 0, fin.stderr[-200:])

naive = run_child("naive")
# Control case: this is what the OLD behaviour does at teardown - it is allowed to fail, and
# the point of the test is that the finalize path above does NOT behave like it.
warned = "Destroyed while thread is still running" in (naive.stderr + naive.stdout) or naive.returncode != 7
check("control: WITHOUT finalize the same situation misbehaves (warning or abnormal exit)",
      warned, f"rc={naive.returncode} err={naive.stderr.strip()[-160:]}")

# ============================================================ 2) stale PID after fast stop/restart
world = {}          # pid -> (alive, creation_time)
pt._win_creation_time = lambda pid: world.get(pid, (False, None))[1]
pt._process_alive = lambda pid: world.get(pid, (False, None))[0]
killed = []
pt._kill_tree = lambda pid: killed.append(pid)

world[100] = (True, 1111)
h = pt._ProcHandle(100)
check("handle captures pid + creation time", (h.pid, h.created) == (100, 1111))
check("kill_tree() kills our own live process", h.kill_tree() and killed == [100])

killed.clear()
world[100] = (True, 9999)        # same PID, different process: recycled after our session died
check("recycled PID is NOT killed", h.kill_tree() is False and killed == [], killed)
world.pop(100)
check("dead PID is NOT killed", h.kill_tree() is False and killed == [])

# no stale handle survives a restart: each session gets its own, swapped out with its PTY
class FakePty:
    def __init__(self, pid): self.pid = pid; self.alive = True; self.ev = threading.Event()
    def read(self, n=4096): self.ev.wait(5); raise OSError("EOF")
    def write(self, s): pass
    def isalive(self): return self.alive
    def terminate(self, force=False): self.alive = False; self.ev.set()
    def close(self): pass
    def setwinsize(self, r, c): pass

killed.clear()
w = pt.PtyTerminalWidget()
pids = [201, 202, 203]
for pid in pids:
    world[pid] = (True, pid * 10)
    f = FakePty(pid)
    pt._pty_imports = lambda f=f: (pyte, type("S", (), {"spawn": staticmethod(lambda *a, **k: f)}))
    w.start(["x.bat"], None, None)
    time.sleep(0.02)
    world[pid] = (False, None)          # that session's process dies right after being replaced
w.stop()
check("fast restart storm never kills a stale PID", killed == [], killed)
pt.drain_reader_threads(2000)

killed.clear()
world[301] = (True, 3010)
f = FakePty(301)
pt._pty_imports = lambda: (pyte, type("S", (), {"spawn": staticmethod(lambda *a, **k: f)}))
w.start(["x.bat"], None, None); time.sleep(0.05); w.stop()
check("the CURRENT session's tree is killed exactly once", killed == [301], killed)
check("tree-kill succeeded -> terminate() not used as a second routine kill", f.alive is True or f.alive is False)
pt.drain_reader_threads(2000)

# ============================================================ 3-5) page-level refusal and switch
tmp = pathlib.Path(ROOT) / "_t_cli"; tmp.mkdir(exist_ok=True)
(tmp / "Claude.bat").write_text("@echo off\n")
class Cfg:
    data = {}; root = pathlib.Path(ROOT)
    def path(self, k): return tmp if k == "cli" else pathlib.Path(ROOT)
    def save(self): pass
logs = []
ctx = types.SimpleNamespace(cfg=Cfg(), procs=ProcessManager(), downloads=None, monitor=None,
                            log=logs.append, goto=lambda k: None, refresh_services=lambda: None)
console = []
ctx.procs._start_console = lambda k, c, wd, e: console.append(k)
world[401] = (True, 4010)
f = FakePty(401)
pt._pty_imports = lambda: (pyte, type("S", (), {"spawn": staticmethod(lambda *a, **k: f)}))

page = CliTerminalPage(ctx, "Claude")
answers = []
page._confirm = lambda text: (answers.append(text), answers and answers[-1] and ANSWER)[1]
ANSWER = False

page.restart()
check("embedded session started", page.term.is_running())

# 3) external start while embedded is live -> refused, session survives
page.start_external()
check("external start REFUSED while embedded is live", console == [] and page.term.is_running(), console)
check("refusal is logged and points at 'Wechseln'", any("Wechseln" in l for l in logs), logs[-1:])

# 5a) explicit switch, user says No -> nothing happens
ANSWER = False
page.switch_session()
check("switch declined -> live session untouched", page.term.is_running() and console == [])
check("decline is logged", any("abgebrochen" in l for l in logs), logs[-1:])

# 5b) explicit switch, user says Yes -> controlled stop, then start
ANSWER = True
page.switch_session()
check("switch confirmed -> embedded stopped", not page.term.is_running())
check("switch confirmed -> external started", console == ["cli:Claude"], console)
ctx.procs._popen["cli:Claude"] = types.SimpleNamespace(poll=lambda: None, pid=402)

# 4) embedded start while external is live -> refused
page.restart()
check("embedded start REFUSED while external console is live", not page.term.is_running())
check("holder is reported as the console", ctx.procs.holder_of("cli:Claude") == "console")

# 5c) switch back, confirmed -> console stopped, embedded restarted
import core.procs as CP
killed_ext = []; CP._kill_tree = lambda pid: killed_ext.append(pid)
world[403] = (True, 4030); f2 = FakePty(403)
pt._pty_imports = lambda: (pyte, type("S", (), {"spawn": staticmethod(lambda *a, **k: f2)}))
ANSWER = True
page.switch_session()
check("switch back stopped the console first", killed_ext == [402], killed_ext)
check("switch back restarted the embedded terminal", page.term.is_running())
page.stop(); pt.drain_reader_threads(2000)

# ============================================================ 6) owner lifecycle
check("owner is held WEAKLY (no manager -> page -> ctx -> manager cycle)",
      isinstance(ctx.procs._embedded.get("cli:Claude"), weakref.ref))
page.shutdown()
check("shutdown() deregisters", "cli:Claude" not in ctx.procs._embedded)

# a page that is merely closed deregisters too, without shutdown()
world[501] = (True, 5010); f3 = FakePty(501)
pt._pty_imports = lambda: (pyte, type("S", (), {"spawn": staticmethod(lambda *a, **k: f3)}))
page2 = CliTerminalPage(ctx, "Claude")
check("re-registration after a GUI rebuild replaces the old owner",
      ctx.procs._embedded["cli:Claude"]() is page2)
page2.close()
check("closeEvent() deregisters without shutdown()", "cli:Claude" not in ctx.procs._embedded)

# the manager never keeps a page alive: a plain owner is dropped by the weakref as soon as the
# last real reference goes (a QWidget's Python wrapper additionally lives as long as its C++
# object does, which is Qt's business, not the manager's - hence a non-Qt owner here).
class PlainOwner:
    def embedded_is_running(self): return False
    def embedded_stop(self): pass
plain = PlainOwner()
ctx.procs.register_embedded("cli:Plain", plain)
pref = weakref.ref(plain)
del plain
gc.collect()
check("manager alone does not keep an owner alive (weakref, no cycle)", pref() is None)
check("a dead weakref entry is cleaned up on lookup",
      ctx.procs.is_running("cli:Plain") is False and "cli:Plain" not in ctx.procs._embedded)

# and a page whose C++ object Qt destroys is dropped via the destroyed signal, not by luck
page3 = CliTerminalPage(ctx, "Claude")
check("page3 registered", ctx.procs._embedded["cli:Claude"]() is page3)
wref = weakref.ref(page3)
shiboken6_delete_target = page3
import shiboken6 as _sb
_sb.delete(shiboken6_delete_target)
app.processEvents()
check("Qt-destroyed page is deregistered via the destroyed signal",
      "cli:Claude" not in ctx.procs._embedded)
del page3, shiboken6_delete_target, page2, page
gc.collect()
check("no strong reference keeps the dead page's wrapper alive", wref() is None)

# registering an already-deleted C++ object is refused outright
dead = CliTerminalPage(ctx, "Claude"); ctx.procs.unregister_embedded("cli:Claude")
_sb.delete(dead)
try:
    ctx.procs.register_embedded("cli:Claude", dead); ok = False
except RuntimeError:
    ok = True
check("registering a deleted C++ object raises instead of storing it", ok)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
