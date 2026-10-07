"""AI Workspace Control Center - central GUI for the portable AI toolchain.

Usage:  python main.py [--root <dir>]
The root defaults to the parent folder of AI_Launcher, so the tree can move between drives.
Startup errors are written to launcher_error.log (pythonw has no console).

This file is a thin duplicate of launcher.py, kept only because README.md/QUICKSTART.md
document "python main.py" as the manual test command. Start_AI_Launcher.bat calls
launcher.py directly - if you change one entry point, change the other too.
"""
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _quiet_windows() -> None:
    """No stray console / mini windows: pythonw has no stdio (log to file) and every child process is started hidden."""
    if sys.platform != "win32":
        return
    if sys.stdout is None or sys.stderr is None:
        try:
            (HERE / "logs").mkdir(exist_ok=True)
            log = open(HERE / "logs" / "launcher_start.log", "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stdout or log
            sys.stderr = sys.stderr or log
        except OSError:
            pass
    import subprocess
    CREATE_NO_WINDOW, CREATE_NEW_CONSOLE = 0x08000000, 0x00000010
    orig = subprocess.Popen.__init__

    def patched(self, *a, **kw):                      # noqa: ANN001
        flags = kw.get("creationflags", 0)
        if not flags & CREATE_NEW_CONSOLE:            # interactive consoles are wanted on purpose
            kw["creationflags"] = flags | CREATE_NO_WINDOW
            if kw.get("startupinfo") is None:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0                    # SW_HIDE
                kw["startupinfo"] = si
        orig(self, *a, **kw)

    subprocess.Popen.__init__ = patched


_quiet_windows()


def _report(exc_text: str) -> None:
    try:
        (HERE / "launcher_error.log").write_text(exc_text, encoding="utf-8")
    except OSError:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, exc_text[-1500:] + "\n\nFull log: launcher_error.log", "AI Launcher - startup error", 0x10)
    except Exception:       # noqa: BLE001
        pass
    print(exc_text, file=sys.stderr)


def main() -> int:
    import argparse
    from PySide6.QtWidgets import QApplication
    from core.config import Config, detect_root

    ap = argparse.ArgumentParser()
    ap.add_argument("--root", help="override the auto-detected root folder")
    args = ap.parse_args()
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    # Qt 6: high-DPI scaling is always on (AA_EnableHighDpiScaling / AA_UseHighDpiPixmaps are deprecated no-ops).
    # PassThrough keeps fractional Windows scaling (125 %, 150 %) exact instead of rounding it - must be set
    # before the QApplication exists.
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("AI Workspace Control Center")
    cfg = Config.load(detect_root(args.root))
    from ui import theme
    theme.set_mode(str(cfg.data.get("theme", "light")))     # before the pages import theme constants (OK, BAD, ...)
    from ui.main_window import MainWindow
    from core.runtime_tools import apply_to_process
    apply_to_process(cfg)          # portable ExifTool / FFmpeg on PATH for every child process
    win = MainWindow(cfg)
    win.show_initial()
    code = app.exec()
    # Last statement of the app: if a terminal reader thread is still stuck inside winpty's
    # read(), normal teardown would destroy a running QThread and abort. finalize_process_exit()
    # ends the process with os._exit() in that case and returns `code` unchanged otherwise.
    # Everything that needed saving was written in MainWindow.closeEvent, long before this.
    from ui.pty_terminal import finalize_process_exit
    return finalize_process_exit(code)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        _report(traceback.format_exc())
        sys.exit(1)
