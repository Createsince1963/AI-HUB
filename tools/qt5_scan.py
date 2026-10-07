"""Qt5 -> Qt6 (PySide6) porting scanner.

Usage:
  python qt5_scan.py <folder-or-file> [--report report.md] [--apply-safe]

Scans .py files for PyQt5 / Qt5-only constructs and reports them per file and line, with a
suggested Qt6/PySide6 replacement. --apply-safe rewrites only purely mechanical items
(and writes <file>.bak first): PyQt5 -> PySide6 imports, pyqtSignal/Slot/Property, .exec_().
Everything else is REPORT ONLY because it needs a human decision.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "build", "dist", "node_modules", "site-packages"}


@dataclass(frozen=True)
class Rule:
    key: str
    pattern: re.Pattern
    advice: str
    safe: bool = False
    repl: str | None = None       # regex replacement for --apply-safe
    severity: str = "manual"      # manual | safe | info


def R(key, pat, advice, safe=False, repl=None, severity=None):
    return Rule(key, re.compile(pat), advice, safe, repl, severity or ("safe" if safe else "manual"))


RULES = [
    R("import-pyqt5", r"\bPyQt5\b", "Use PySide6 (same module names: QtCore/QtGui/QtWidgets).", True, "PySide6"),
    R("pyqtSignal", r"\bpyqtSignal\b", "PySide6: Signal", True, "Signal"),
    R("pyqtSlot", r"\bpyqtSlot\b", "PySide6: Slot", True, "Slot"),
    R("pyqtProperty", r"\bpyqtProperty\b", "PySide6: Property", True, "Property"),
    R("exec_", r"\.exec_\(", "Use .exec() (exec_ is removed in Qt6/PySide6).", True, ".exec("),
    R("sip", r"\bfrom\s+PyQt5\s+import\s+sip\b|\bimport\s+sip\b", "No sip in PySide6; use shiboken6 (isValid, wrapInstance)."),
    R("QAction-widgets", r"QtWidgets\s*\.\s*QAction\b|from\s+PyQt5\.QtWidgets\s+import[^\n]*\bQAction\b|from\s+PySide6\.QtWidgets\s+import[^\n]*\bQAction\b",
      "QAction/QActionGroup/QShortcut moved from QtWidgets to QtGui."),
    R("QShortcut-widgets", r"from\s+(?:PyQt5|PySide6)\.QtWidgets\s+import[^\n]*\bQShortcut\b", "QShortcut moved to QtGui."),
    R("QRegExp", r"\bQRegExp(?:Validator)?\b", "Removed: use QRegularExpression / QRegularExpressionValidator."),
    R("QDesktopWidget", r"\bQDesktopWidget\b|\.desktop\(\)", "Removed: use QScreen (QGuiApplication.primaryScreen(), screen.availableGeometry())."),
    R("QTextCodec", r"\bQTextCodec\b", "Removed in Qt6: use Python str/bytes or QStringConverter."),
    R("QtWebKit", r"\bQtWebKit(Widgets)?\b", "QtWebKit is gone: use QtWebEngineWidgets (PySide6-Addons)."),
    R("WebEngine-import", r"\bPyQt5\.QtWebEngineWidgets\b|\bPySide6\.QtWebEngineWidgets\b.*QWebEngineSettings",
      "Check: QWebEngineProfile/Page/Settings moved to QtWebEngineCore in Qt6.", severity="info"),
    R("width()", r"\.\s*(?:fontMetrics\(\)|QFontMetrics\([^)]*\))\s*\.\s*width\(|\bQFontMetrics(?:F)?\b.*\.width\(",
      "QFontMetrics.width() removed: use horizontalAdvance()."),
    R("delta()", r"\.delta\(\)", "QWheelEvent.delta() removed: use angleDelta().y()."),
    R("pos-events", r"\bevent\s*\.\s*(?:pos|globalPos|x|y)\(\)", "Mouse events: prefer position()/globalPosition() (old calls deprecated).", severity="info"),
    R("setResizeMode", r"\.setResizeMode\(", "QHeaderView.setResizeMode -> setSectionResizeMode."),
    R("toTime_t", r"\.toTime_t\(\)|\.fromTime_t\(", "QDateTime: toSecsSinceEpoch()/fromSecsSinceEpoch()."),
    R("scoped-enums", r"\bQt\.(?:Align|Key_|Horizontal\b|Vertical\b|AlignLeft|AlignRight|AlignCenter|Checked|Unchecked|UserRole|DisplayRole|ItemIs|Window(?:Modal|StaysOnTopHint)|LeftButton|RightButton)",
      "Qt6 uses scoped enums (Qt.AlignmentFlag.AlignLeft, Qt.ItemDataRole.UserRole, ...). PySide6 still accepts the short form via its compat layer, but write the long form in new code.", severity="info"),
    R("QMessageBox-enums", r"\bQMessageBox\.(?:Yes|No|Ok|Cancel|Save|Discard|Warning|Critical|Information|Question)\b|\bQFileDialog\.(?:DontUse|ShowDirsOnly)",
      "Scoped enums: QMessageBox.StandardButton.Yes, QMessageBox.Icon.Warning, ...", severity="info"),
    R("loadUi", r"\bloadUi\b|\buic\.loadUi\b|PyQt5\.uic", "PyQt5.uic.loadUi -> PySide6.QtUiTools.QUiLoader, or compile with pyside6-uic."),
    R("QVariant", r"\bQVariant\b|\.toPyObject\(\)|\.toString\(\)\s*$", "QVariant is not needed in Python; remove wrappers/toPyObject().", severity="info"),
    R("QtMultimedia", r"\bQMediaPlayer\b|\bQMediaPlaylist\b|\bQSound\b|\bQCamera\b", "QtMultimedia was redesigned in Qt6 (QMediaPlayer + QAudioOutput, no QMediaPlaylist/QSound)."),
    R("QTableWidget-sel", r"\bQItemSelection\b.*\bcontains\b", "Check selection model API differences.", severity="info"),
    R("high-dpi", r"\bAA_EnableHighDpiScaling\b|\bAA_UseHighDpiPixmaps\b", "High-DPI is always on in Qt6: remove these attributes."),
    R("QApplication.exec-static", r"\bQApplication\.exec_\(\)|sys\.exit\(\s*\w+\.exec_\(\)", "Use app.exec().", True, None),
]


def iter_files(target: Path):
    if target.is_file():
        yield target
        return
    for p in target.rglob("*.py"):
        if not any(part in SKIP_DIRS for part in p.parts):
            yield p


def scan_file(path: Path):
    hits = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return hits
    for no, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for rule in RULES:
            if rule.pattern.search(line):
                hits.append((no, rule, stripped[:110]))
    return hits


def apply_safe(path: Path) -> int:
    text = path.read_text(encoding="utf-8", errors="replace")
    new, count = text, 0
    for rule in RULES:
        if rule.safe and rule.repl:
            new, n = rule.pattern.subn(rule.repl, new)
            count += n
    if count:
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        path.write_text(new, encoding="utf-8")
    return count


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target")
    ap.add_argument("--report", help="write a Markdown report")
    ap.add_argument("--apply-safe", action="store_true", help="apply mechanical fixes (creates .bak files)")
    args = ap.parse_args()

    target = Path(args.target)
    if not target.exists():
        print(f"not found: {target}")
        return 2

    out: list[str] = []
    totals = {"manual": 0, "safe": 0, "info": 0}
    files_hit = 0
    n_files = 0
    for f in sorted(iter_files(target)):
        n_files += 1
        hits = scan_file(f)
        if not hits:
            continue
        files_hit += 1
        out.append(f"\n## {f}")
        for no, rule, code in hits:
            totals[rule.severity] += 1
            out.append(f"- L{no} [{rule.severity}] **{rule.key}**: {rule.advice}  \n  `{code}`")
        if args.apply_safe:
            done = apply_safe(f)
            out.append(f"- applied {done} safe replacement(s), backup: {f.name}.bak")

    header = [f"# Qt5 -> Qt6 scan: {target}",
              f"Files scanned: {n_files}, files with findings: {files_hit}",
              f"Findings: {totals['manual']} manual, {totals['safe']} mechanical, {totals['info']} info",
              "Levels: manual = needs a decision, mechanical = auto-fixable with --apply-safe, info = check/style"]
    text = "\n".join(header + out) + "\n"
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
        print(f"report written: {args.report}")
    print("\n".join(header))
    if not args.report:
        print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
