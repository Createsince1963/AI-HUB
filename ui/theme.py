"""Look & feel - same palette as the PDF Scanner / P3D Data Manager control launchers."""
from __future__ import annotations

import re

ACCENT, ACCENT_DARK = "#146C94", "#0E5B7F"
OK, WARN, BAD, MUTED = "#2e9e4f", "#c58a00", "#d93025", "#61758A"
_LIGHT_CONST = (ACCENT, ACCENT_DARK, OK, WARN, BAD, MUTED)


def _sync_constants() -> None:
    """Module constants (imported by the pages as `from .theme import OK, ...`) follow the mode. The pages import them
    once, so main.py sets the mode from config.json BEFORE importing the UI; a later switch needs a restart for them."""
    global ACCENT, ACCENT_DARK, OK, WARN, BAD, MUTED
    if _MODE == "dark":
        ACCENT, ACCENT_DARK, OK, WARN, BAD, MUTED = DARK["series_a"], DARK["accent_dark"], DARK["ok"], DARK["series_a"], DARK["bad"], DARK["muted"]
    else:
        ACCENT, ACCENT_DARK, OK, WARN, BAD, MUTED = _LIGHT_CONST


# ---------------------------------------------------------------- palettes (light = original look, dark = Copilot/mockup look)
# Every colour of the stylesheet and of the custom-painted widgets comes from one of these tokens.
LIGHT = {
    "text": "#172033", "bg": "#F4F7FB", "title": "#102A43", "muted": "#61758A", "ok": "#218A63", "bad": "#A61B1B",
    "card": "#FFFFFF", "border": "#DCE3EE", "nav": "#33475b", "hover": "#EEF4FA", "accent": "#146C94", "accent_dark": "#0E5B7F",
    "ctl_border": "#C8D3E0", "ctl_border_hover": "#91A7BE", "disabled": "#9AA8B8", "accent_disabled": "#9DBFD0",
    "danger_border": "#E5B6B6", "danger_hover": "#FBEAEA", "go_bg": "#DDF3E4", "go_border": "#A9D9B8", "go_text": "#14532d",
    "go_hover": "#C9EBD3", "alt": "#F7F9FC", "console_bg": "#0F1B2A", "console_text": "#D7E3F1", "console_border": "#0B1420",
    "grid": "#EDF1F6", "sel_bg": "#D6E8F1", "header": "#F1F5FA", "header_border": "#E6ECF3", "track": "#E6ECF3", "chunk": "#2A9D8F",
    "tip_bg": "#102A43", "series_a": "#2F6FED", "series_b": "#1F9D6B", "series_c": "#C2410C",
}
# Dark = strictly three colours: slate grey (surfaces), lime (good / active / primary), light blue (values, info,
# warnings); text white. No red / orange / yellow - warnings and errors use light blue plus a bold marker.
SLATE, LIME, SKY = "#3A4664", "#8BD12B", "#8FB8F0"
DARK = {
    "text": "#FFFFFF", "bg": "#2A3550", "title": "#FFFFFF", "muted": "#C9D2E0", "ok": LIME, "bad": SKY,
    "card": SLATE, "border": "#56637F", "nav": "#FFFFFF", "hover": "#465375", "accent": LIME, "accent_dark": "#76B91C",
    "ctl_border": "#5E6B88", "ctl_border_hover": "#8794B0", "disabled": "#8D98AD", "accent_disabled": "#55663A",
    "danger_border": SKY, "danger_hover": "#465375", "go_bg": "#46553F", "go_border": LIME, "go_text": "#FFFFFF",
    "go_hover": "#52634A", "alt": "#34405C", "console_bg": "#222B40", "console_text": "#FFFFFF", "console_border": "#1B2334",
    "grid": "#4A5672", "sel_bg": "#4F6A99", "header": "#334060", "header_border": "#4C5875", "track": "#4A5672", "chunk": LIME,
    "tip_bg": "#222B40", "series_a": SKY, "series_b": LIME, "series_c": "#C3D9F7",
}
_MODE = "light"


def mode() -> str:
    return _MODE


def set_mode(m: str) -> str:
    global _MODE
    _MODE = "dark" if m == "dark" else "light"
    _sync_constants()
    return _MODE


def palette(m: str | None = None) -> dict:
    return DARK if (m or _MODE) == "dark" else LIGHT


def col(name: str) -> str:
    """Token of the active palette (custom-painted widgets call this at paint time, so a theme switch needs only update())."""
    return palette()[name]

QSS = r"""
* { font-family: "Segoe UI"; font-size: 10pt; color: #172033; }
QMainWindow, QDialog { background: #F4F7FB; }
QWidget#Page { background: #F4F7FB; }
QLabel#Title { font-size: 17pt; font-weight: 700; color: #102A43; }
QLabel#Subtitle { color: #61758A; }
QLabel#Section { font-size: 8.5pt; font-weight: 800; color: #61758A; letter-spacing: 1px; margin-top: 8px; }
QLabel#Big { font-size: 20pt; font-weight: 700; color: #102A43; }
QLabel#Muted { color: #61758A; }
QLabel#Ok { color: #218A63; font-weight: 700; }
QLabel#Bad { color: #A61B1B; font-weight: 700; }
QFrame#Card { background: #FFFFFF; border: 1px solid #DCE3EE; border-radius: 12px; }
QFrame#Sidebar { background: #FFFFFF; border-right: 1px solid #DCE3EE; }
QListWidget#Nav { background: transparent; border: none; outline: none; }
QListWidget#Nav::item { padding: 10px 14px; margin: 2px 8px; border-radius: 8px; color: #33475b; font-weight: 600; }
QListWidget#Nav::item:hover { background: #EEF4FA; }
QListWidget#Nav::item:selected { background: #146C94; color: white; }
QPushButton { background: #FFFFFF; border: 1px solid #C8D3E0; border-radius: 7px; padding: 6px 13px; font-weight: 600; }
QPushButton:hover { background: #EEF4FA; border-color: #91A7BE; }
QPushButton:disabled { color: #9AA8B8; background: #F4F7FB; border-color: #DCE3EE; }
QPushButton#Primary { background: #146C94; color: white; border: 1px solid #146C94; }
QPushButton#Primary:hover { background: #0E5B7F; }
QPushButton#Primary:disabled { background: #9DBFD0; border-color: #9DBFD0; color: white; }
QPushButton#Danger { color: #A61B1B; border: 1px solid #E5B6B6; }
QPushButton#Danger:hover { background: #FBEAEA; }
QPushButton#Go { background: #DDF3E4; border: 1px solid #A9D9B8; color: #14532d; }
QPushButton#Go:hover { background: #C9EBD3; }
QLineEdit, QComboBox { background: #FFFFFF; border: 1px solid #C8D3E0; border-radius: 7px; padding: 6px 9px; }
QLineEdit:focus, QComboBox:focus { border-color: #146C94; }
QComboBox::drop-down { border: none; width: 22px; }
QTabWidget::pane { border: 1px solid #DCE3EE; border-radius: 10px; background: #FFFFFF; top: -1px; }
QTabBar::tab { background: #F4F7FB; border: 1px solid #DCE3EE; border-bottom: none; border-top-left-radius: 8px; border-top-right-radius: 8px; padding: 7px 16px; margin-right: 2px; }
QTabBar::tab:selected { background: #FFFFFF; font-weight: 700; color: #102A43; }
QPlainTextEdit, QTextEdit { background: #F7F9FC; font-family: Consolas, "Cascadia Mono", monospace; font-size: 9pt; border: 1px solid #DCE3EE; border-radius: 8px; }
QPlainTextEdit#Console { background: #0F1B2A; color: #D7E3F1; border: 1px solid #0B1420; }
QPlainTextEdit#ConsoleLight { background: #FFFFFF; color: #172033; border: 1px solid #DCE3EE; border-radius: 8px; }
QTableWidget, QTreeWidget { background: #FFFFFF; alternate-background-color: #F7F9FC; border: 1px solid #DCE3EE; border-radius: 8px; gridline-color: #EDF1F6; selection-background-color: #D6E8F1; selection-color: #102A43; }
QHeaderView::section { background: #F1F5FA; border: none; border-bottom: 1px solid #DCE3EE; border-right: 1px solid #E6ECF3; padding: 6px 8px; font-weight: 700; color: #33475b; }
QProgressBar { border: none; border-radius: 5px; background: #E6ECF3; height: 10px; text-align: center; font-size: 8pt; }
QProgressBar::chunk { background: #2A9D8F; border-radius: 5px; }
QScrollBar:vertical { background: transparent; width: 11px; } QScrollBar::handle:vertical { background: #C8D3E0; border-radius: 5px; min-height: 30px; }
QScrollBar:horizontal { background: transparent; height: 11px; } QScrollBar::handle:horizontal { background: #C8D3E0; border-radius: 5px; min-width: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QSplitter::handle { background: #DCE3EE; }
QStatusBar { background: #FFFFFF; border-top: 1px solid #DCE3EE; }
QToolTip { background: #102A43; color: white; border: none; padding: 5px; }
QCheckBox { spacing: 6px; }
"""


def _derive(qss_light: str, m: str) -> str:
    """Dark stylesheet = the light one with every palette hex replaced by the matching token of the other palette."""
    if m == "light":
        return qss_light
    rev: dict[str, str] = {}
    for k, v in LIGHT.items():              # first token wins: "title" and "tip_bg" share #102A43 in the light set
        rev.setdefault(v.lower(), DARK[k])
    return re.sub(r"#[0-9A-Fa-f]{6}", lambda mo: rev.get(mo.group(0).lower(), mo.group(0)), qss_light)


def qss(m: str | None = None) -> str:
    m = m or _MODE
    extra = f"""
QLabel#Brand {{ font-size: 14pt; font-weight: 800; color: {palette(m)['title']}; padding-left: 16px; }}
QLabel#Banner {{ border-radius: 6px; padding: 7px 10px; font-weight: 600; }}
QToolTip {{ background: {palette(m)['tip_bg']}; color: {'white' if m == 'light' else palette(m)['text']}; border: none; padding: 5px; }}
"""
    if m == "dark":
        p = DARK
        extra += f"""
QMainWindow, QDialog, QMessageBox, QInputDialog {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #26314A, stop:1 #46526F); }}
QWidget#Page, QScrollArea, QScrollArea > QWidget > QWidget#Page {{ background: transparent; }}
QScrollArea > QWidget {{ background: transparent; }}
QFrame#Sidebar {{ background: rgba(30, 38, 56, 0.55); border-right: 1px solid {p['border']}; }}
QFrame#Card, QFrame#FactTile {{ background: rgba(70, 83, 115, 0.55); border: 1px solid {p['border']}; }}
QLabel {{ background: transparent; }}
QLabel#Title, QLabel#Big {{ color: {p['title']}; }}
QTabBar::tab:selected, QHeaderView::section {{ font-weight: 700; }}
QPushButton#Primary {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #A6E23F, stop:1 #7DC21E); color: #18240A; border: 1px solid #9BD43A; font-weight: 700; }}
QPushButton#Primary:hover {{ background: #98D735; }}
QPushButton#Primary:disabled {{ background: {p['accent_disabled']}; color: #A9B58F; border-color: {p['accent_disabled']}; }}
QListWidget#Nav::item:selected {{ background: rgba(139, 209, 43, 0.22); color: #FFFFFF; border-left: 3px solid {p['accent']}; }}
QComboBox QAbstractItemView, QListView, QAbstractItemView {{ background: {p['card']}; color: {p['text']}; border: 1px solid {p['border']};
    selection-background-color: {p['sel_bg']}; selection-color: #FFFFFF; outline: none; }}
QComboBox QAbstractItemView::item {{ min-height: 24px; padding: 2px 8px; }}
QHeaderView {{ background: {p['header']}; }}
QHeaderView::section {{ background: {p['header']}; color: {p['text']}; }}
QTableCornerButton::section {{ background: {p['header']}; border: none; }}
QMenu {{ background: {p['card']}; color: {p['text']}; border: 1px solid {p['border']}; padding: 4px; }}
QMenu::item {{ padding: 5px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {p['sel_bg']}; }}
QSpinBox, QDoubleSpinBox {{ background: {p['card']}; color: {p['text']}; border: 1px solid {p['ctl_border']}; border-radius: 7px; padding: 4px 6px; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border: 1px solid {p['ctl_border']}; border-radius: 3px; background: {p['card']}; }}
QCheckBox::indicator:checked {{ background: {p['accent']}; border-color: {p['accent']}; }}
QSlider::groove:horizontal {{ height: 4px; background: {p['track']}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {p['accent']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: #FFFFFF; border: 2px solid {p['accent']}; width: 12px; margin: -6px 0; border-radius: 7px; }}
QTabBar::tab {{ color: {p['muted']}; }}
QTabBar::tab:selected {{ color: #FFFFFF; border-bottom: 2px solid {p['accent']}; }}
QStatusBar {{ background: rgba(30, 38, 56, 0.6); }}
"""
    return _derive(QSS, m) + extra


def _hsl(h: str) -> tuple[float, float, float]:
    import colorsys
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
    hh, ll, ss = colorsys.rgb_to_hls(r, g, b)
    return hh * 360, ss, ll


def to_dark(hex_col: str, prop: str = "color") -> str:
    """Any hard-coded colour -> one of the three dark tones. prop = color | background | border."""
    if _MODE != "dark" or not re.fullmatch(r"#[0-9A-Fa-f]{6}", hex_col or ""):
        return hex_col
    h, s, l = _hsl(hex_col)
    green = s > 0.25 and 70 <= h <= 170
    if prop == "color":
        if green:
            return DARK["ok"]
        if s > 0.25 and l < 0.9:                  # red / orange / yellow / blue text -> light blue
            return DARK["series_a"]
        return DARK["text"]                       # greys, dark text, white -> white
    if prop == "background":
        if green and l < 0.85:
            return DARK["go_bg"] if l < 0.5 else DARK["ok"]
        return DARK["card"] if l > 0.5 else DARK["bg"]     # light panels / banners -> slate
    if green:
        return DARK["ok"]
    return DARK["series_a"] if s > 0.35 else DARK["border"]


_PROP_RE = re.compile(r"((?:background(?:-color)?)|(?:border[a-z-]*)|(?<![a-z-])color)\s*:\s*([^;}\n]*)", re.I)


def remap_qss(text: str) -> str:
    if _MODE != "dark" or not text or "#" not in text:
        return text

    def one(mo):
        prop = mo.group(1).lower()
        kind = "background" if prop.startswith("background") else "border" if prop.startswith("border") else "color"
        val = re.sub(r"#[0-9A-Fa-f]{6}", lambda h: to_dark(h.group(0), kind), mo.group(2))
        return f"{mo.group(1)}:{val}"
    return _PROP_RE.sub(one, text)


_orig_set_ss = None


def _install_ss_hook() -> None:
    """Widgets that set their own inline stylesheet with fixed colours (badges, banners, status labels) are mapped
    to the three dark tones on the fly - no need to touch every call site."""
    global _orig_set_ss
    from PySide6.QtWidgets import QWidget
    if _orig_set_ss is not None:
        return
    _orig_set_ss = QWidget.setStyleSheet

    def hooked(self, text):
        return _orig_set_ss(self, remap_qss(text))
    QWidget.setStyleSheet = hooked


def apply(m: str | None = None) -> None:
    """Theme for the WHOLE application (not only the main window): popups of combo boxes, menus, message boxes
    and input dialogs are separate top-level windows and stayed light when only the window had the stylesheet.
    The QPalette covers what a stylesheet does not reach (native parts, empty view areas)."""
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        return
    m = m or _MODE
    p = palette(m)
    pal = QPalette()
    if m == "dark":
        for role, key in ((QPalette.Window, "bg"), (QPalette.Base, "card"), (QPalette.AlternateBase, "alt"),
                          (QPalette.Button, "card"), (QPalette.ToolTipBase, "tip_bg")):
            pal.setColor(role, QColor(p[key]))
        for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText, QPalette.ToolTipText):
            pal.setColor(role, QColor(p["text"]))
        pal.setColor(QPalette.Highlight, QColor(p["sel_bg"]))
        pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
        pal.setColor(QPalette.PlaceholderText, QColor(p["muted"]))
        pal.setColor(QPalette.Disabled, QPalette.Text, QColor(p["disabled"]))
        pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(p["disabled"]))
    else:
        pal = app.style().standardPalette()
    app.setPalette(pal)
    _install_ss_hook()
    app.setStyleSheet(qss(m))          # QApplication.setStyleSheet - not affected by the QWidget hook

