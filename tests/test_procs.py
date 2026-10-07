"""Simulated tests for the central double-start protection in core.procs, plus the
multi-tab shutdown cost. Fake owner stands in for CliTerminalPage."""
import os, sys, time, threading
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication
app = QApplication([])
from core import procs as P
import pyte
from ui import pty_terminal as pt

FAILS=[]
def check(n,c,e=""):
    print(("PASS  " if c else "FAIL  ")+n+(("  -> "+str(e)) if e else ""))
    if not c: FAILS.append(n)

started = []
P._kill_tree = lambda pid: started.append(("kill", pid))

class Owner:
    def __init__(self): self.running=False; self.stops=0
    def embedded_is_running(self): return self.running
    def embedded_stop(self): self.stops += 1; self.running=False

pm = P.ProcessManager()
o = Owner()
pm.register_embedded("cli:Claude", o)
check("protocol check accepts the owner", isinstance(o, P.EmbeddedOwner))
check("unregistered key not running", not pm.is_running("cli:Codex"))
o.running = True
check("embedded terminal counts as running", pm.is_running("cli:Claude"))
check("include_embedded=False hides own terminal", not pm.is_running("cli:Claude", include_embedded=False))
check("running_keys() excludes embedded tabs by default (no exit prompt for open tabs)",
      pm.running_keys() == [], pm.running_keys())
check("running_keys(include_embedded=True) lists it", pm.running_keys(True) == ["cli:Claude"])

# Dashboard / external console starting the same tool must stop the embedded terminal first
calls=[]
pm._start_console = lambda k,c,w,e: calls.append(("console",k))
blocked = []
pm.blocked.connect(lambda k, r: blocked.append((k, r)))
started = pm.start("cli:Claude", ["Claude.bat"], None, None, interactive=True)
check("start() REFUSES instead of killing the live embedded session",
      started is False and o.stops == 0 and o.running)
check("refusal names what is in the way", blocked == [("cli:Claude", "läuft bereits im eingebetteten Terminal")], blocked)
check("nothing was started", calls == [], calls)
check("holder_of() reports the embedded terminal", pm.holder_of("cli:Claude") == "embedded")

# the deliberate switch
started = pm.start("cli:Claude", ["Claude.bat"], None, None, interactive=True, takeover=True)
check("takeover=True stops the embedded terminal, then starts", started and o.stops == 1 and not o.running)
check("external start then happened", calls == [("console","cli:Claude")], calls)

# stop() covers both
o.running = True
pm.stop("cli:Claude")
check("stop() stops the embedded terminal too", o.stops == 2 and not o.running)

# stop_all() covers embedded-only keys, unconditionally
o.running = True
pm.stop_all()
check("stop_all() stops embedded-only keys without asking", o.stops == 3)

# dead widget must not break anything
class Dead(Owner):
    def embedded_is_running(self): raise RuntimeError("C++ object deleted")
    def embedded_stop(self): raise RuntimeError("C++ object deleted")
pm.register_embedded("cli:Codex", Dead())
check("deleted widget: is_running() survives and forgets it", pm.is_running("cli:Codex") is False)
pm.register_embedded("cli:Codex", Dead())
pm.stop("cli:Codex")
check("deleted widget: stop() survives", True)

pm.unregister_embedded("cli:Claude", o)
o.running = True
check("after unregister the owner is invisible", not pm.is_running("cli:Claude"))
pm.register_embedded("cli:Claude", o)
pm.unregister_embedded("cli:Claude", Owner())    # different owner -> must NOT unregister
check("unregister with a foreign owner is a no-op", pm.is_running("cli:Claude"))

# ------------------------------------------------------- multi-tab shutdown cost
class FakePty:
    def __init__(self): self.pid=1; self.alive=True; self.ev=threading.Event()
    def read(self,n=4096): self.ev.wait(10); raise OSError("EOF")
    def isalive(self): return self.alive
    def terminate(self, force=False): self.alive=False      # does NOT release read()
    def close(self): pass
    def setwinsize(self,r,c): pass
pt._kill_tree = lambda pid: None
ws=[]
for i in range(4):
    f=FakePty()
    pt._pty_imports = lambda f=f: (pyte, type("S",(),{"spawn": staticmethod(lambda *a,**k: f)}))
    w = pt.PtyTerminalWidget(); w.start(["x.bat"],None,None); ws.append((w,f))
time.sleep(0.1)
t0=time.monotonic()
for w,_ in ws: w.stop()
per_tab=(time.monotonic()-t0)*1000
t0=time.monotonic(); left=pt.drain_reader_threads(600); drain=(time.monotonic()-t0)*1000
check("4 stuck tabs: per-tab stop cost stays ~4x300ms, not 4x2000ms", per_tab < 1500, f"{per_tab:.0f} ms")
check("drain for all 4 shares ONE budget", drain < 800, f"{drain:.0f} ms, left={left}")
for _,f in ws: f.ev.set()
pt.drain_reader_threads(2000)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
