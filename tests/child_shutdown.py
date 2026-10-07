"""Child process: builds a reader thread that is permanently stuck inside read(), then does the
real application shutdown. argv[1] = 'finalize' (the implemented strategy) or 'naive' (what
happens WITHOUT it: normal Python/Qt teardown destroys a running QThread)."""
import os, sys, threading
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication
import pyte
app = QApplication([])
from ui import pty_terminal as pt

class StuckPty:
    def __init__(self): self.pid = os.getpid(); self.ev = threading.Event()
    def read(self, n=4096): self.ev.wait(120); raise OSError("EOF")   # never returns in time
    def isalive(self): return True
    def terminate(self, force=False): pass        # kill does NOT release the read
    def close(self): pass
    def setwinsize(self, r, c): pass

f = StuckPty()
pt._kill_tree = lambda pid: None
pt._pty_imports = lambda: (pyte, type("S", (), {"spawn": staticmethod(lambda *a, **k: f)}))
w = pt.PtyTerminalWidget(); w.start(["x.bat"], None, None)
import time; time.sleep(0.2)
w.stop()                                   # detaches the stuck reader
left = pt.drain_reader_threads(200)
print(f"stuck_after_drain={left}", flush=True)
code = 7
if sys.argv[1] == "finalize":
    sys.exit(pt.finalize_process_exit(code))
sys.exit(code)                             # naive: normal teardown with a live QThread
