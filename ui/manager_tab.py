"""Model Manager tab: two panes (Total Commander style) on top of the SQLite model index.

Left/right pane each show one source: old stock (imported from the audit CSV), X_DOWNLOADS or the portable library.
Mark files with Insert / Space (or click), then F5 = copy, F6 = move, F8 = delete (to the other pane's library).
Ollama models are marked (diamond) and get an extra confirmation / options in the dialogs.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QItemSelectionModel, Qt, QThread, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QRadioButton, QSplitter, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from core import ollama_ops
from core.models import AREAS, STATUSES, effective_area, fmt_age, parse_quant, scan_library, suggest_category
from core.modeldb import ROOT_ALT, ROOT_DOWNLOADS, ROOT_PORTABLE, ModelDB
from core.system import fmt_size, vram_verdict
from .theme import ACCENT, BAD, MUTED, OK, WARN
from .widgets import NumItem, button, run_async

if TYPE_CHECKING:
    from .context import Ctx

SOURCES = [(ROOT_ALT, "Old stock (audit)"), (ROOT_DOWNLOADS, "Downloads (X_DOWNLOADS)"), (ROOT_PORTABLE, "Portable library")]
HEADERS = ["", "Name", "Einsatzbereich", "Quant", "Kategorie", "Size", "Type", "Modified", "=", "Ollama", "Status", "Path"]
C_EQ, C_OLLAMA, C_STATUS, C_PATH = 8, 9, 10, 11
APPROVE_DIR = "gguf"                        # library sub folder that approved LLM GGUFs are moved to
USER_PREFIX = "user:"                       # root id of a user folder = "user:" + tokenized path ({ROOT}/... when portable)
ADD_FOLDER, REMOVE_FOLDER = "__add__", "__remove__"
CHUNK = 8 * 1024 * 1024
DIAMOND, DIAMOND_OPEN = "◆", "◇"


def _item(text: str, colour: str | None = None) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    if colour:
        it.setForeground(QColor(colour))
    return it


def _reveal(path: str) -> None:
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", path])
    else:
        import webbrowser
        webbrowser.open(f"file://{os.path.dirname(path)}")


# =============================================================== file operations (worker thread)
class Cancelled(Exception):
    pass


def sha256_file(path: str, cancel=None, on_bytes=None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            if cancel and cancel():
                raise Cancelled()
            h.update(chunk)
            if on_bytes:
                on_bytes(len(chunk))
    return h.hexdigest().upper()


def _same_volume(a: str, b_dir: str) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b_dir).st_dev
    except OSError:
        return False


def _existing_ancestor(p: str) -> str:
    p = os.path.abspath(p)
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


class TransferJob(QThread):
    """Copies / moves files one after another. Never overwrites, never leaves a half-written target (.part + rename).

    Move = same volume: plain rename. Other volume: copy with SHA256, verify the target by re-reading it, then delete the source."""
    progress = Signal(str, object, object)          # text, bytes done, bytes total
    finished_all = Signal(list)

    def __init__(self, items: list[dict], move: bool, verify: bool):
        super().__init__()
        self.items, self.move, self.verify = items, move, verify
        self._cancel = False
        self._done = 0
        self._label = ""
        self._factor = 2 if (move or verify) else 1
        self._total = sum(i["size"] for i in items) * self._factor

    def cancel(self) -> None:
        self._cancel = True

    def _tick(self, n: int) -> None:
        self._done += n
        self.progress.emit(self._label, self._done, self._total)

    def _res(self, it: dict, status: str, msg: str = "", hash_: str | None = None) -> dict:
        return {"id": it["id"], "status": status, "msg": msg, "hash": hash_}

    def run(self) -> None:                                  # noqa: D102
        results: list[dict] = []
        # free space check for everything that really has to be copied
        try:
            need = sum(i["size"] for i in self.items if not (self.move and _same_volume(i["src"], _existing_ancestor(os.path.dirname(i["dst"])))
                                                                  if os.path.exists(i["src"]) else False))
            free = __import__("shutil").disk_usage(_existing_ancestor(os.path.dirname(self.items[0]["dst"]))).free
            if need > free:
                msg = f"not enough free space on the target drive (need {fmt_size(need)}, free {fmt_size(free)})"
                self.finished_all.emit([self._res(i, "error", msg) for i in self.items])
                return
        except Exception:        # noqa: BLE001 - the check is a convenience, the copy itself reports real errors
            pass
        for it in self.items:
            if self._cancel:
                results.append(self._res(it, "skipped", "cancelled"))
                continue
            self._label = os.path.basename(it["src"])
            try:
                results.append(self._one(it))
            except Cancelled:
                results.append(self._res(it, "skipped", "cancelled"))
            except Exception as exc:        # noqa: BLE001 - reported per file
                results.append(self._res(it, "error", str(exc)))
        self.finished_all.emit(results)

    def _one(self, it: dict) -> dict:
        src, dst, size = it["src"], it["dst"], it["size"]
        if not os.path.isfile(src):
            return self._res(it, "error", "source file not found")
        if os.path.exists(dst):
            return self._res(it, "skipped", "target already exists")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if self.move and _same_volume(src, os.path.dirname(dst)):
            os.replace(src, dst)
            self._tick(size * self._factor)
            return self._res(it, "ok")
        part = dst + ".part"
        h = hashlib.sha256()
        try:
            with open(src, "rb") as fi, open(part, "wb") as fo:
                while True:
                    chunk = fi.read(CHUNK)
                    if not chunk:
                        break
                    if self._cancel:
                        raise Cancelled()
                    fo.write(chunk)
                    h.update(chunk)
                    self._tick(len(chunk))
            if os.path.getsize(part) != size:
                raise RuntimeError("size mismatch after copy")
            os.replace(part, dst)
            try:
                st = os.stat(src)
                os.utime(dst, (st.st_atime, st.st_mtime))          # keep the modification date
            except OSError:
                pass
        except BaseException:
            try:
                os.remove(part)
            except OSError:
                pass
            raise
        digest = h.hexdigest().upper()
        if self.move or self.verify:
            try:
                if sha256_file(dst, lambda: self._cancel, self._tick) != digest:
                    raise RuntimeError("SHA256 mismatch after copy - target removed, source kept")
            except BaseException:
                try:
                    os.remove(dst)
                except OSError:
                    pass
                raise
        if self.move:
            os.remove(src)
        return self._res(it, "ok", "", digest)


# =============================================================== widgets
class MarkTable(QTableWidget):
    """Table with Total-Commander keys: Insert / Space mark the current row and step down, Tab switches the pane."""
    switch_pane = Signal()
    got_focus = Signal()

    def keyPressEvent(self, e) -> None:            # noqa: N802
        k = e.key()
        if k in (Qt.Key_Insert, Qt.Key_Space) and not e.modifiers():
            r = self.currentRow()
            if r >= 0:
                self.selectionModel().select(self.model().index(r, 0),
                                             QItemSelectionModel.SelectionFlag.Toggle | QItemSelectionModel.SelectionFlag.Rows)
                if r + 1 < self.rowCount():
                    self.setCurrentCell(r + 1, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            e.accept()
            return
        if k in (Qt.Key_Tab, Qt.Key_Backtab):
            self.switch_pane.emit()
            e.accept()
            return
        super().keyPressEvent(e)

    def focusInEvent(self, e) -> None:            # noqa: N802
        super().focusInEvent(e)
        self.got_focus.emit()


class ConfirmDialog(QDialog):
    """Confirmation with a details list and optional radio choices / check box."""

    def __init__(self, parent, title: str, text: str, lines: list[str], ok_text: str, danger: bool = False,
                 options: list[str] | None = None, checkbox: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lab = QLabel(text)
        lab.setWordWrap(True)
        lay.addWidget(lab)
        box = QPlainTextEdit()
        box.setReadOnly(True)
        box.setPlainText("\n".join(lines[:200]) + (f"\n... +{len(lines) - 200} more" if len(lines) > 200 else ""))
        box.setMaximumHeight(170)
        lay.addWidget(box)
        self.radios: list[QRadioButton] = []
        for i, o in enumerate(options or []):
            rb = QRadioButton(o)
            rb.setChecked(i == 0)
            self.radios.append(rb)
            lay.addWidget(rb)
        self.cb = QCheckBox(checkbox) if checkbox else None
        if self.cb:
            lay.addWidget(self.cb)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton(ok_text)
        ok.setObjectName("Danger" if danger else "Primary")
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        (cancel if danger else ok).setDefault(True)
        (cancel if danger else ok).setFocus()

    def choice(self) -> int:
        return next((i for i, r in enumerate(self.radios) if r.isChecked()), 0)

    def checked(self) -> bool:
        return bool(self.cb and self.cb.isChecked())


class HistoryDialog(QDialog):
    """Read-only audit trail from the database (newest first). With one marked file: only that file's history."""

    def __init__(self, parent, db: ModelDB, row: dict | None):
        super().__init__(parent)
        self.setWindowTitle("History" + (f" - {row['name']}" if row else " - all actions"))
        self.resize(1100, 520)
        lay = QVBoxLayout(self)
        data = db.history(2000, row["id"] if row else None)
        t = QTableWidget(len(data), 8)
        t.setHorizontalHeaderLabels(["Time", "Action", "Name", "Size", "From", "To", "Status", "Note"])
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setAlternatingRowColors(True)
        t.verticalHeader().setVisible(False)
        for i, h in enumerate(data):
            vals = [h["ts"], h["action"], h["name"] or "", fmt_size(h["size"]) if h["size"] is not None else "",
                    h["from_loc"] or "", h["to_loc"] or "", h["status"] or "", h["note"] or ""]
            for c, v in enumerate(vals):
                it = _item(v, BAD if (c == 6 and v == "error") else None)
                it.setToolTip(v)
                t.setItem(i, c, it)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for c, w in enumerate((140, 100, 240, 80, 260, 260, 60, 240)):
            t.setColumnWidth(c, w)
        lay.addWidget(t, 1)
        lay.addWidget(QLabel(f"{len(data)} entries (newest first)"))


class Pane(QFrame):
    """One list (source selectable) with filter, marker column and selection summary."""

    def __init__(self, mgr: "ManagerTab", root: str):
        super().__init__()
        self.setObjectName("Pane")
        self.mgr, self.root = mgr, root
        self.all_rows: list[dict] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        top = QHBoxLayout()
        self.combo = QComboBox()
        self.fill_combo()
        self.combo.currentIndexChanged.connect(self._source_changed)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by name...")
        self.filter.textChanged.connect(self.fill)
        self.only_ollama = QCheckBox("Ollama only")
        self.only_ollama.toggled.connect(self.fill)
        top.addWidget(self.combo)
        top.addWidget(self.filter, 1)
        top.addWidget(self.only_ollama)
        lay.addLayout(top)
        t = self.table = MarkTable(0, len(HEADERS))
        t.setHorizontalHeaderLabels(HEADERS)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.ExtendedSelection)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setAlternatingRowColors(True)
        t.setShowGrid(False)
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(24)
        hh = t.horizontalHeader()
        for c, w in enumerate((26, 320, 90, 70, 110, 80, 60, 70, 24, 100, 80, 220)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
            t.setColumnWidth(c, w)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setMinimumSectionSize(20)
        t.horizontalHeaderItem(0).setToolTip("Diamond = Ollama model")
        t.horizontalHeaderItem(C_EQ).setToolTip("= same file (hash) exists in the other pane, ~ same name and size")
        t.setSortingEnabled(True)
        t.setContextMenuPolicy(Qt.CustomContextMenu)
        t.customContextMenuRequested.connect(self._menu)
        t.got_focus.connect(lambda: mgr.set_active(self))
        t.switch_pane.connect(mgr.switch_pane)
        t.itemSelectionChanged.connect(self._summary)
        t.doubleClicked.connect(lambda _i: mgr.reveal())
        lay.addWidget(t, 1)
        self.info = QLabel("")
        self.info.setObjectName("Muted")
        lay.addWidget(self.info)
        self.set_active(False)

    def set_active(self, on: bool) -> None:
        self.setStyleSheet("QFrame#Pane{border:2px solid %s; border-radius:8px;}" % (ACCENT if on else "transparent"))

    def fill_combo(self) -> None:
        """Fixed sources + user folders + add/remove entries. Keeps the current root selected."""
        c = self.combo
        c.blockSignals(True)
        c.clear()
        for k, t in self.mgr.sources():
            c.addItem(t, k)
        c.insertSeparator(c.count())
        c.addItem("+ Add user folder...", ADD_FOLDER)
        if self.root.startswith(USER_PREFIX):
            c.addItem("- Remove this user folder from the list", REMOVE_FOLDER)
        idx = c.findData(self.root)
        c.setCurrentIndex(idx if idx >= 0 else c.findData(ROOT_PORTABLE))
        self.root = c.currentData()
        c.blockSignals(False)

    def _source_changed(self, _i) -> None:
        data = self.combo.currentData()
        if data == ADD_FOLDER:
            new = self.mgr.add_user_folder()
            if new:
                self.root = new
            self.mgr.refresh_combos()
            if new:
                self.mgr.rescan()
            return
        if data == REMOVE_FOLDER:
            self.mgr.remove_user_folder(self.root)
            self.root = ROOT_PORTABLE
            self.mgr.refresh_combos()
            self.reload()
            return
        self.root = data
        self.fill_combo()
        self.reload()

    def reload(self) -> None:
        self.all_rows = self.mgr.db.rows(self.root)
        self.fill()

    def selected_ids(self) -> list[int]:
        return [self.table.item(ix.row(), 1).data(Qt.UserRole) for ix in self.table.selectionModel().selectedRows()]

    def selected_rows(self) -> list[dict]:
        ids = set(self.selected_ids())
        return [r for r in self.all_rows if r["id"] in ids]

    def fill(self) -> None:
        text = self.filter.text().lower()
        hashes, pairs = self.mgr.other_keys(self)
        rows = [r for r in self.all_rows if text in r["name"].lower() and (not self.only_ollama.isChecked() or r["ollama"])]
        keep = set(self.selected_ids())
        vram = self.mgr.ctx.vram_gb()
        t = self.table
        t.blockSignals(True)
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            mark = _item("")
            if r["ollama"]:
                inst = self.mgr.installed
                nm = r["ollama_name"]
                missing = inst is not None and nm and ollama_ops.normalize(nm) not in inst
                mark = _item(DIAMOND_OPEN if missing else DIAMOND, WARN if missing else ACCENT)
                mark.setToolTip(f"Ollama model: {nm or '(no name set)'}" + ("\nnot installed in Ollama (any more)" if missing else ""))
                mark.setTextAlignment(Qt.AlignCenter)
            t.setItem(i, 0, mark)
            name = _item(r["name"], None if r["present"] else BAD)
            name.setData(Qt.UserRole, r["id"])
            name.setToolTip(self.mgr.full_path(r) + ("" if r["present"] else "\nfile not found on disk"))
            t.setItem(i, 1, name)
            area = effective_area(r["area"], r["name"], r["category"])
            ai = _item(area, None if r["area"] else MUTED)
            ai.setToolTip("set by you" if r["area"] else "automatic suggestion (Set area... to override)")
            t.setItem(i, 2, ai)
            t.setItem(i, 3, _item(parse_quant(r["name"]), MUTED))
            t.setItem(i, 4, _item(r["category"], MUTED))
            t.setItem(i, 5, NumItem(fmt_size(r["size"]), r["size"]))
            t.setItem(i, 6, _item(Path(r["name"]).suffix.lower().lstrip(".")))
            t.setItem(i, 7, NumItem(fmt_age(r["mtime"]) if r["mtime"] else "", r["mtime"]))
            eq = _item("")
            if hashes or pairs:
                if r["hash"] and r["hash"] in hashes:
                    eq = _item("=", OK)
                    eq.setToolTip("identical file (SHA256) exists in the other pane")
                elif (r["name"].lower(), r["size"]) in pairs:
                    eq = _item("~", WARN)
                    eq.setToolTip("file with the same name and size exists in the other pane")
            eq.setTextAlignment(Qt.AlignCenter)
            t.setItem(i, C_EQ, eq)
            st, tip = self.mgr.ollama_state(r)
            oi = _item(st, {"linked": OK, "copy": WARN, "installed": ACCENT, "not installed": BAD}.get(st, MUTED))
            oi.setToolTip(tip)
            t.setItem(i, C_OLLAMA, oi)
            full = self.mgr.full_path(r)
            pi = _item(os.path.dirname(r["relpath"]).replace("/", "\\") or ".", MUTED)
            pi.setToolTip(full)
            t.setItem(i, C_PATH, pi)
            st = self.mgr.status_of(r)
            si = _item(st, {"approved": OK, "tested": ACCENT, "new": WARN}.get(st, MUTED))
            si.setToolTip("new = just downloaded, tested = ran fine in llama.cpp, approved = in the library and offered in the pickers")
            t.setItem(i, C_STATUS, si)
            if vram_verdict(r["size"], vram)[0] == "fits":
                for c in range(len(HEADERS)):
                    it = t.item(i, c)
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
        t.setSortingEnabled(True)
        sm = t.selectionModel()
        for i in range(t.rowCount()):
            if t.item(i, 1).data(Qt.UserRole) in keep:
                sm.select(t.model().index(i, 0), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        t.blockSignals(False)
        self._summary()

    def _summary(self) -> None:
        sel = self.selected_rows()
        shown = self.table.rowCount()
        total = sum(r["size"] for r in self.all_rows)
        self.info.setText(f"{shown} of {len(self.all_rows)} files ({fmt_size(total)})   |   "
                          f"marked: {len(sel)} ({fmt_size(sum(r['size'] for r in sel))})")

    def _menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is not None and not self.table.item(item.row(), 1).isSelected():
            self.table.clearSelection()
            self.table.selectRow(item.row())
        self.mgr.set_active(self)
        if self.selected_rows():
            self.mgr.row_menu().exec(self.table.viewport().mapToGlobal(pos))


# =============================================================== the tab
class ManagerTab(QWidget):
    def __init__(self, ctx: "Ctx"):
        super().__init__()
        self.ctx = ctx
        self.db = ModelDB()
        self.job: TransferJob | None = None
        self.installed: set[str] | None = None        # Ollama models found on disk, None = not synced yet
        self.mf: dict[str, list[str]] = {}            # installed model -> model blob digests (hex)
        self.digest_names: dict[str, list[str]] = {}  # blob digest -> model names
        self._loaded = False
        lay = QVBoxLayout(self)
        hint = QLabel("Mark files with Insert / Space or click.   F5 copy   |   F6 move   |   F8 delete   |   Tab switch pane   |   "
                      "diamond = Ollama model")
        hint.setObjectName("Muted")
        lay.addWidget(hint)
        split = QSplitter(Qt.Horizontal)
        self.left = Pane(self, ROOT_ALT)
        self.right = Pane(self, ROOT_PORTABLE)
        split.addWidget(self.left)
        split.addWidget(self.right)
        lay.addWidget(split, 1)
        self.active = self.left
        self.left.set_active(True)

        bar = QHBoxLayout()
        bar.addWidget(button("F5  Copy  >", "Go", lambda: self.transfer(False), "Copy the marked files to the other pane's library"))
        bar.addWidget(button("F6  Move  >", "Primary", lambda: self.transfer(True), "Move the marked files (source deleted after verification)"))
        bar.addWidget(button("F8  Delete", "Danger", self.delete, "Permanently delete the marked files"))
        bar.addSpacing(16)
        bar.addWidget(button("Set area...", "", self.set_area))
        bar.addWidget(button("Approve  >", "Go", self.approve,
                             "Move the marked GGUF(s) into the library (AI_Modells\\gguf), set status approved, "
                             "optionally create them in Ollama - one step"))
        self.oll_btn = button("Ollama", "")
        m = QMenu(self.oll_btn)
        m.addAction("Activate selected GGUF in Ollama...").triggered.connect(self.ollama_activate)
        m.addAction("Mark as Ollama model").triggered.connect(lambda: self.ollama_mark(True))
        m.addAction("Remove Ollama marker").triggered.connect(lambda: self.ollama_mark(False))
        m.addAction("Remove from Ollama (ollama rm, file stays)").triggered.connect(self.ollama_remove)
        m.addAction("Deduplicate: replace Ollama copy by hard link").triggered.connect(self.ollama_dedupe)
        m.addSeparator()
        m.addAction("Sync with Ollama (installed models)").triggered.connect(self.ollama_sync)
        m.addAction("Storage overview...").triggered.connect(lambda: self.update_storage(show=True))
        self.oll_btn.setMenu(m)
        bar.addWidget(self.oll_btn)
        bar.addWidget(button("Compute hash", "", self.hash_selected, "SHA256 of the marked files (for exact duplicate detection)"))
        bar.addStretch(1)
        self.verify_cb = QCheckBox("Verify copies (SHA256)")
        self.verify_cb.setToolTip("Re-read every copied file and compare its hash. Moves to another drive are always verified.")
        bar.addWidget(self.verify_cb)
        bar.addWidget(button("History...", "", self.show_history, "Audit trail of all copy / move / delete / Ollama actions"))
        bar.addWidget(button("Rescan", "", self.rescan))
        bar.addWidget(button("Import CSV...", "", self.import_csv))
        lay.addLayout(bar)

        prow = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("Muted")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setVisible(False)
        self.cancel_btn = button("Cancel", "Danger", self.cancel_job)
        self.cancel_btn.setVisible(False)
        self.storage_lbl = QLabel("")
        self.storage_lbl.setObjectName("Muted")
        self.storage_lbl.setToolTip("Library = indexed model files, Ollama = blobs store, Duplicate = Ollama copies of library files "
                                    "(free them with Ollama > Deduplicate), Shared = hard-linked (no extra space)")
        prow.addWidget(self.storage_lbl)
        prow.addSpacing(12)
        prow.addWidget(self.status, 1)
        prow.addWidget(self.bar, 1)
        prow.addWidget(self.cancel_btn)
        lay.addLayout(prow)

        for key, fn in (("F5", lambda: self.transfer(False)), ("F6", lambda: self.transfer(True)), ("F8", self.delete)):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
        dm = getattr(ctx, "downloads", None)
        if dm is not None and hasattr(dm, "finished"):
            dm.finished.connect(lambda _j: self.rescan() if self._loaded else None)     # new downloads show up by themselves

    # ---------------------------------------------------------------- helpers
    def say(self, text: str) -> None:
        self.status.setText(text)

    def set_active(self, pane: Pane) -> None:
        if pane is self.active:
            return
        self.active.set_active(False)
        self.active = pane
        pane.set_active(True)

    def switch_pane(self) -> None:
        p = self.right if self.active is self.left else self.left
        self.set_active(p)
        p.table.setFocus()

    def other(self, pane: Pane) -> Pane:
        return self.right if pane is self.left else self.left

    def other_keys(self, pane: Pane):
        o = self.other(pane)
        if o.root == pane.root:
            return set(), set()
        return self.db.match_keys(o.root)

    def base_dir(self, root: str) -> Path:
        if root.startswith(USER_PREFIX):
            return Path(self.ctx.cfg.expand(root[len(USER_PREFIX):]))
        return self.ctx.cfg.path("models" if root == ROOT_PORTABLE else "downloads")

    def user_folders(self) -> list[str]:
        return list(self.ctx.cfg.data.get("manager_folders", []) or [])

    def sources(self) -> list[tuple[str, str]]:
        out = list(SOURCES)
        for tok in self.user_folders():
            out.append((USER_PREFIX + tok, "Folder: " + self.ctx.cfg.expand(tok)))
        return out

    def add_user_folder(self) -> str | None:
        d = QFileDialog.getExistingDirectory(self, "Add a model folder (shown as its own source in both panes)",
                                             str(self.ctx.cfg.root))
        if not d:
            return None
        tok = self.ctx.cfg.tokenize(d)
        folders = self.user_folders()
        if tok not in folders:
            folders.append(tok)
            self.ctx.cfg.data["manager_folders"] = folders
            self._save_cfg()
            self.ctx.log(f"Model manager: user folder added {d}")
        return USER_PREFIX + tok

    def remove_user_folder(self, root: str) -> None:
        tok = root[len(USER_PREFIX):]
        self.ctx.cfg.data["manager_folders"] = [f for f in self.user_folders() if f != tok]
        self._save_cfg()
        self.db.drop_root(root)                    # index rows only - files are not touched
        self.ctx.log(f"Model manager: user folder removed from list {self.ctx.cfg.expand(tok)} (files untouched)")

    def refresh_combos(self) -> None:
        for p in (self.left, self.right):
            p.fill_combo()
            p.reload()

    def ollama_state(self, r: dict) -> tuple[str, str]:
        """(status text, tooltip) of one file relative to the Ollama store."""
        if self.installed is None:
            return "", "Ollama not synced yet"
        h = (r["hash"] or "").lower()
        if h and h in self.digest_names:
            names = ", ".join(self.digest_names[h])
            bp = ollama_ops.blob_path(self.ctx.cfg, h)
            if ollama_ops.is_linked(bp, self.full_path(r)):
                return "linked", f"Ollama: {names}\nblob is a hard link to this file - no extra space"
            return "copy", f"Ollama: {names}\nOllama holds a separate copy ({fmt_size(r['size'])}) - Ollama > Deduplicate frees it"
        nm = r["ollama_name"]
        if nm and ollama_ops.normalize(nm) in self.installed:
            return "installed", f"Ollama: {nm} (compute the hash to check copy / link)"
        if r["ollama"]:
            return "not installed", f"marked as {nm or '(no name)'}, but not installed in Ollama"
        return "", ""

    def full_path(self, r: dict) -> str:
        base = r["base"] if r["root"] == ROOT_ALT else self.ctx.cfg.expand(r["base"])
        return os.path.normpath(os.path.join(base, *r["relpath"].split("/")))

    def _busy(self) -> bool:
        if self.job is not None:
            self.say("A transfer is still running - wait for it or press Cancel.")
            return True
        return False

    def refresh_view(self) -> None:
        for p in (self.left, self.right):
            p.reload()

    def _save_cfg(self) -> None:
        try:
            self.ctx.cfg.save()
        except OSError as exc:
            self.ctx.log(f"Could not save config: {exc}")

    # ---------------------------------------------------------------- loading / scanning
    def on_show(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        csv_file = self.ctx.cfg.root / "audit-reports" / "files-complete.csv"
        if self.db.count(ROOT_ALT) == 0 and csv_file.is_file():
            try:
                n = self.db.import_csv(csv_file)
                self.ctx.log(f"Model index: imported {n} old-stock files from {csv_file.name}")
            except Exception as exc:        # noqa: BLE001
                self.ctx.log(f"CSV import failed: {exc}")
        self.refresh_view()
        self.rescan()

    def rescan(self) -> None:
        cfg = self.ctx.cfg
        models, dl = cfg.path("models"), cfg.path("downloads")
        alt = [(r["id"], self.full_path(r)) for r in self.db.rows(ROOT_ALT)]
        users = [USER_PREFIX + t for t in self.user_folders()]
        self.say("Scanning ...")

        def work():
            out = {ROOT_PORTABLE: scan_library(models)}
            if os.path.normcase(str(dl)) != os.path.normcase(str(models)):
                out[ROOT_DOWNLOADS] = scan_library(dl, skip=frozenset({"old"}))
            for root in users:
                d = self.base_dir(root)
                if d.is_dir():
                    out[root] = scan_library(d)
            present = {i: os.path.isfile(p) for i, p in alt}
            return out, present

        run_async(work, on_result=self._scanned, on_error=lambda e: self.say(f"Scan failed: {e}"))

    def _scanned(self, res) -> None:
        out, present = res
        cfg = self.ctx.cfg
        legacy = cfg.data.get("model_areas", {}) or {}
        added = removed = 0
        for root, models in out.items():
            base = self.base_dir(root)
            a, r = self.db.sync_scan(root, cfg.tokenize(str(base)), models, str(base), legacy, cfg.tokenize)
            added += a
            removed += r
        self.db.set_present(present)
        missing = sum(1 for v in present.values() if not v)
        self.refresh_view()
        self.ollama_sync(quiet=True)
        self.say(f"Scan done: {added} new, {removed} gone" + (f"   |   old stock: {missing} of {len(present)} files not found on disk" if present else ""))

    def import_csv(self) -> None:
        start = str(self.ctx.cfg.root / "audit-reports")
        f, _ = QFileDialog.getOpenFileName(self, "Import audit CSV (old stock)", start, "CSV (*.csv)")
        if not f:
            return
        try:
            n = self.db.import_csv(Path(f))
        except Exception as exc:        # noqa: BLE001
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        self.ctx.log(f"Model index: imported {n} files from {f}")
        self.rescan()

    # ---------------------------------------------------------------- F5 / F6
    def _dest_rel(self, r: dict) -> str:
        rel = r["relpath"]
        return rel if "/" in rel else f"{suggest_category(r['name'])}/{r['name']}"

    def transfer(self, move: bool, rows: list[dict] | None = None, dst_root: str | None = None,
                 rel_fn=None, on_done=None, title: str | None = None) -> None:
        """F5 / F6 to the other pane. Approve passes rows, target root, target relpath and a callback (ok ids)."""
        if self._busy():
            return
        src = self.active
        dst_root = dst_root or self.other(src).root
        rows = src.selected_rows() if rows is None else rows
        rel_fn = rel_fn or self._dest_rel
        if not rows:
            self.say("Nothing marked - mark files with Insert / Space or by clicking.")
            return
        if dst_root == ROOT_ALT:
            QMessageBox.information(self, "Old stock is read-only", "The old stock is only a source. Switch the other pane to "
                                    "'Portable library' or 'Downloads' as target.")
            return
        if all(r["root"] == dst_root for r in rows):
            QMessageBox.information(self, "Same list", "Both panes show the same source. Switch the other pane to the target library.")
            return
        base = self.base_dir(dst_root)
        items, lines = [], []
        for r in rows:
            rel = rel_fn(r)
            items.append({"id": r["id"], "src": self.full_path(r), "dst": os.path.normpath(str(base / rel)),
                          "size": r["size"], "rel": rel})
            lines.append(f"{r['name']}   ->   {rel.rsplit('/', 1)[0] if '/' in rel else ''}/   ({fmt_size(r['size'])})")
        total = sum(r["size"] for r in rows)
        orows = [r for r in rows if r["ollama"]]
        text = ((title + "\n\n" if title else "") +
                f"{'Move' if move else 'Copy'} {len(rows)} file(s) ({fmt_size(total)})\nfrom  {src.combo.currentText()}\nto      {base}\n\n"
                "Existing files are never overwritten.")
        if move:
            text += " The source is deleted only after the target has been verified (SHA256) or renamed on the same drive."
        if orows:
            text += (f"\n\n{len(orows)} of the files are Ollama models. Ollama imports GGUF files into its own store, "
                     "so models that are already registered keep working after the file has been moved.")
        dlg = ConfirmDialog(self, "Move files" if move else "Copy files", text, lines, "Move" if move else "Copy",
                            checkbox="Re-register the moved Ollama model(s) in Ollama afterwards (ollama create)" if move and orows else None)
        if dlg.exec() != QDialog.Accepted:
            return
        rereg = dlg.checked()
        self.job = TransferJob(items, move, self.verify_cb.isChecked())
        self.job.progress.connect(self._progress)
        self.job.finished_all.connect(lambda res: self._transfer_done(res, move, dst_root, rows, items, rereg, on_done))
        self.bar.setValue(0)
        self.bar.setVisible(True)
        self.cancel_btn.setVisible(True)
        self.say(f"{'Moving' if move else 'Copying'} {len(rows)} file(s) ...")
        self.job.start()

    def _progress(self, text: str, done, total) -> None:
        self.bar.setValue(int(done * 1000 / total) if total else 0)
        self.bar.setFormat(f"{text}  {fmt_size(done)} / {fmt_size(total)}")
        self.bar.setTextVisible(True)

    def cancel_job(self) -> None:
        if self.job:
            self.job.cancel()
            self.say("Cancelling after the current chunk ...")

    def _transfer_done(self, results: list[dict], move: bool, dst_root: str, rows: list[dict], items: list[dict], rereg: bool,
                       on_done=None) -> None:
        self.job = None
        self.bar.setVisible(False)
        self.cancel_btn.setVisible(False)
        by_id = {r["id"]: r for r in rows}
        rel_by_id = {i["id"]: i for i in items}
        base_tok = self.ctx.cfg.tokenize(str(self.base_dir(dst_root)))
        ok, notes = 0, []
        reg: list[tuple[str, str]] = []
        for res in results:
            r, it = by_id[res["id"]], rel_by_id[res["id"]]
            self.db.log("move" if move else "copy", r["id"], r["name"], r["size"], res["hash"] or r["hash"], it["src"], it["dst"],
                        res["status"], res["msg"] or ("verified" if res["hash"] and (move or self.verify_cb.isChecked()) else ""))
            if res["status"] == "ok":
                ok += 1
                if move:
                    self.db.move_row(r["id"], dst_root, base_tok, it["rel"], res["hash"])
                else:
                    self.db.copy_row(r, dst_root, base_tok, it["rel"], res["hash"])
                    if res["hash"] and not r["hash"]:
                        self.db.set_hash(r["id"], res["hash"])
                self.ctx.log(f"{'Moved' if move else 'Copied'} {it['src']} -> {it['dst']}")
                if move and rereg and r["ollama"] and r["ollama_name"]:
                    reg.append((r["ollama_name"], it["dst"]))
            else:
                notes.append(f"{r['name']}: {res['msg']}")
                self.ctx.log(f"{'Skipped' if res['status'] == 'skipped' else 'FAILED'} {it['src']}: {res['msg']}")
        if on_done is not None:
            on_done([res["id"] for res in results if res["status"] == "ok"])
        self.refresh_view()
        self.say(f"{'Moved' if move else 'Copied'} {ok} of {len(results)} file(s)" + (f", {len(notes)} not done (see log)" if notes else ""))
        if notes:
            QMessageBox.warning(self, "Not everything was transferred", "\n".join(notes[:20]) + (f"\n... +{len(notes) - 20} more" if len(notes) > 20 else ""))
        if reg:
            cfg = self.ctx.cfg

            def work():
                out = []
                for name, path in reg:
                    try:
                        ollama_ops.create_from_gguf(cfg, name, path)
                        out.append(f"Ollama: re-registered {name}")
                    except Exception as exc:        # noqa: BLE001
                        out.append(f"Ollama: re-register of {name} failed: {exc}")
                return out

            run_async(work, on_result=lambda out: [self.ctx.log(x) for x in out], on_error=lambda e: self.ctx.log(f"Ollama: {e}"))

    # ---------------------------------------------------------------- F8
    def delete(self) -> None:
        if self._busy():
            return
        pane = self.active
        rows = pane.selected_rows()
        if not rows:
            self.say("Nothing marked - mark files with Insert / Space or by clicking.")
            return
        orows = [r for r in rows if r["ollama"]]
        options = None
        if orows:
            options = ["Delete the file(s) only", "Remove from Ollama only (ollama rm) - keep the file(s)",
                       "Delete the file(s) AND remove from Ollama"]
        text = (f"Permanently delete {len(rows)} file(s) ({fmt_size(sum(r['size'] for r in rows))}) from "
                f"'{pane.combo.currentText()}'?\nThere is no recycle bin - deleted files cannot be restored.")
        if orows:
            text += f"\n\n{len(orows)} of them are marked as Ollama models. Choose what to remove:"
        dlg = ConfirmDialog(self, "Delete models", text, [f"{r['name']}   ({fmt_size(r['size'])})   {self.full_path(r)}" for r in rows],
                            "Delete", danger=True, options=options)
        if dlg.exec() != QDialog.Accepted:
            return
        choice = dlg.choice() if options else 0
        del_files, rm_ollama = choice in (0, 2), bool(options) and choice in (1, 2)
        jobs = [(r["id"], self.full_path(r)) for r in rows] if del_files else []
        oll = [(r["id"], r["ollama_name"]) for r in orows if r["ollama_name"]] if rm_ollama else []
        cl = self.ctx.ollama()
        self.say("Deleting ...")

        def work():
            gone, errs, odone = [], [], []
            for i, p in jobs:
                try:
                    os.remove(p)
                    gone.append(i)
                except FileNotFoundError:
                    gone.append(i)                     # already missing - just drop the index row
                except OSError as exc:
                    errs.append(f"{os.path.basename(p)}: {exc}")
            for i, name in oll:
                try:
                    cl.delete(name)
                    odone.append(i)
                except Exception as exc:        # noqa: BLE001
                    errs.append(f"ollama rm {name}: {exc}")
            return gone, errs, odone

        by_id = {r["id"]: r for r in rows}

        def done(res) -> None:
            gone, errs, odone = res
            for i in gone:
                r = by_id[i]
                self.db.log("delete", i, r["name"], r["size"], r["hash"], self.full_path(r), "", "ok", "file deleted from disk")
            for i in odone:
                r = by_id[i]
                self.db.log("ollama_rm", i, r["name"], r["size"], r["hash"], r["ollama_name"] or "", "", "ok", "removed from Ollama")
            for e in errs:
                self.db.log("delete", None, "", None, None, "", "", "error", e)
            self.db.delete(gone)
            if not del_files or choice == 1:
                self.db.set_ollama(odone, False)
            for i in gone:
                self.ctx.log(f"Deleted file (id {i})")
            for e in errs:
                self.ctx.log(f"Delete failed: {e}")
            self.refresh_view()
            self.say(f"Deleted {len(gone)} file(s)" + (f", removed {len(odone)} from Ollama" if odone else "")
                     + (f", {len(errs)} error(s) - see log" if errs else ""))
            if errs:
                QMessageBox.warning(self, "Delete errors", "\n".join(errs[:20]))

        run_async(work, on_result=done, on_error=lambda e: self.say(f"Delete failed: {e}"))

    # ---------------------------------------------------------------- history
    def show_history(self) -> None:
        sel = self.active.selected_rows()
        HistoryDialog(self, self.db, sel[0] if len(sel) == 1 else None).exec()

    # ---------------------------------------------------------------- area / hash / reveal
    def row_menu(self) -> QMenu:
        m = QMenu(self)
        m.addAction("Copy (F5)").triggered.connect(lambda: self.transfer(False))
        m.addAction("Move (F6)").triggered.connect(lambda: self.transfer(True))
        m.addAction("Delete (F8)").triggered.connect(self.delete)
        m.addSeparator()
        m.addAction("Set area...").triggered.connect(self.set_area)
        sm = m.addMenu("Set status")
        for st in STATUSES:
            sm.addAction(st).triggered.connect(lambda _c=False, st=st: self.set_status(st))
        sm.addAction("(automatic)").triggered.connect(lambda: self.set_status(None))
        m.addAction("Approve (move to library + Ollama)...").triggered.connect(self.approve)
        m.addAction("Activate in Ollama...").triggered.connect(self.ollama_activate)
        m.addAction("Mark as Ollama model").triggered.connect(lambda: self.ollama_mark(True))
        m.addAction("Remove Ollama marker").triggered.connect(lambda: self.ollama_mark(False))
        m.addAction("Remove from Ollama (file stays)").triggered.connect(self.ollama_remove)
        m.addAction("Deduplicate (hard link Ollama copy)").triggered.connect(self.ollama_dedupe)
        m.addSeparator()
        m.addAction("Compute hash").triggered.connect(self.hash_selected)
        m.addAction("Show in Explorer").triggered.connect(self.reveal)
        return m

    def reveal(self) -> None:
        rows = self.active.selected_rows()
        if rows:
            _reveal(self.full_path(rows[0]))

    def set_area(self) -> None:
        rows = self.active.selected_rows()
        if not rows:
            self.say("Nothing marked.")
            return
        choices = list(dict.fromkeys(AREAS + sorted({r["area"] for r in self.db.rows(ROOT_PORTABLE) if r["area"]}, key=str.lower)))
        area, ok = QInputDialog.getItem(self, "Einsatzbereich", "Area for the marked file(s) - pick one or type a new name:", choices, 0, True)
        if not ok or not area.strip():
            return
        area = area.strip()
        for r in rows:
            self.db.log("area", r["id"], r["name"], r["size"], r["hash"], "", "", "ok", f"{r['area'] or '(automatic)'} -> {area}")
        self.db.set_area([r["id"] for r in rows], area)
        cfg = self.ctx.cfg
        legacy = cfg.data.setdefault("model_areas", {})          # keep the Local library tab in sync
        for r in rows:
            if r["root"] != ROOT_ALT:
                legacy[cfg.tokenize(self.full_path(r))] = area
        self._save_cfg()
        self.refresh_view()

    # ---------------------------------------------------------------- status / approve (test gate)
    @staticmethod
    def status_of(r: dict) -> str:
        """Stored status, or derived: downloads / user folders = new, library = approved, old stock = ''."""
        if r.get("status"):
            return r["status"]
        if r["root"] == ROOT_PORTABLE:
            return "approved"
        return "" if r["root"] == ROOT_ALT else "new"

    def set_status(self, status: str | None) -> None:
        rows = self.active.selected_rows()
        if not rows:
            self.say("Nothing marked.")
            return
        for r in rows:
            self.db.log("status", r["id"], r["name"], r["size"], r["hash"], "", "", "ok",
                        f"{self.status_of(r) or '-'} -> {status or '(automatic)'}")
        self.db.set_status([r["id"] for r in rows], status)
        self.refresh_view()
        self.say(f"Status of {len(rows)} file(s): {status or 'automatic'}")

    def approve(self) -> None:
        """One step: (move into AI_Modells/gguf) + area LLM + status approved + optional `ollama create`."""
        if self._busy():
            return
        rows = [r for r in self.active.selected_rows() if r["name"].lower().endswith(".gguf") and "mmproj" not in r["name"].lower()]
        if not rows:
            self.say("Approve works on .gguf model files - mark at least one (mmproj adapters are skipped).")
            return
        not_llm = [r["name"] for r in rows if effective_area(r["area"], r["name"], r["category"]) != "LLM"]
        if not_llm and QMessageBox.question(
                self, "Approve", "These files are not LLMs by their area:\n" + "\n".join(not_llm[:10]) +
                "\n\nApprove anyway (area is set to LLM)?") != QMessageBox.Yes:
            return
        to_ollama = QMessageBox.question(
            self, "Approve", f"Approve {len(rows)} GGUF file(s).\n\nAlso create them in Ollama (hard link, no second copy)?\n"
            "The Ollama server must be running for that.",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, QMessageBox.No)
        if to_ollama == QMessageBox.Cancel:
            return
        to_ollama = to_ollama == QMessageBox.Yes
        by_id = {r["id"]: r for r in rows}

        def finish(ids: list[int]) -> None:
            self.db.set_area(ids, "LLM")
            self.db.set_status(ids, "approved")
            for i in ids:
                r = by_id[i]
                self.db.log("status", i, r["name"], r["size"], r["hash"], "", "", "ok", f"{self.status_of(r) or '-'} -> approved")
            self.refresh_view()
            self.ctx.log(f"Model manager: approved {len(ids)} file(s)")
            if to_ollama and ids:
                self._ollama_create_many([i for i in ids])

        moving = [r for r in rows if r["root"] != ROOT_PORTABLE]
        stay = [r["id"] for r in rows if r["root"] == ROOT_PORTABLE]
        if stay:
            finish(stay)
        if moving:
            self.transfer(True, rows=moving, dst_root=ROOT_PORTABLE, rel_fn=lambda r: f"{APPROVE_DIR}/{r['name']}",
                          on_done=finish, title="Approve: move the tested file(s) into the model library")

    def _ollama_create_many(self, ids: list[int]) -> None:
        rows = self.db.get(ids)
        cfg = self.ctx.cfg
        jobs = [(r["id"], r["ollama_name"] or ollama_ops.sanitize_name(Path(r["name"]).stem), self.full_path(r), r) for r in rows]
        self.say(f"ollama create for {len(jobs)} model(s) ... (this can take a while)")

        def work():
            out = []
            for rid, name, path, r in jobs:
                try:
                    sha, state = ollama_ops.create_linked(cfg, name, path, (r["hash"] or "").lower() or None)
                    out.append((rid, name, path, sha, state, None))
                except Exception as exc:        # noqa: BLE001
                    out.append((rid, name, path, None, None, str(exc)[:500]))
            return out

        def done(res) -> None:
            bad = 0
            for rid, name, path, sha, state, err in res:
                r = next(x for _i, _n, _p, x in jobs if x["id"] == rid)
                if err:
                    bad += 1
                    self.db.log("ollama_create", rid, r["name"], r["size"], r["hash"], path, name, "error", err)
                    self.ctx.log(f"Ollama create {name} failed: {err}")
                    continue
                self.db.set_hash(rid, sha)
                self.db.set_ollama([rid], True, name)
                self.db.log("ollama_create", rid, r["name"], r["size"], sha, path, name, "ok", f"approve, blob {state}")
                self.ctx.log(f"Ollama: created {name} ({state})")
            self.refresh_view()
            self.ollama_sync(quiet=True)
            self.say(f"Ollama: {len(res) - bad} created" + (f", {bad} failed - see log (is the Ollama server running?)" if bad else ""))

        run_async(work, on_result=done, on_error=lambda e: self.say(f"Ollama create failed: {e}"))

    def hash_selected(self) -> None:
        rows = [r for r in self.active.selected_rows() if r["present"]]
        if not rows:
            self.say("Nothing to hash.")
            return
        paths = [(r["id"], self.full_path(r)) for r in rows]
        self.say(f"Computing SHA256 of {len(paths)} file(s) ({fmt_size(sum(r['size'] for r in rows))}) ...")

        def work():
            return {i: sha256_file(p) for i, p in paths if os.path.isfile(p)}

        def done(res) -> None:
            for i, h in res.items():
                self.db.set_hash(i, h)
            self.refresh_view()
            self.say(f"Hash computed for {len(res)} file(s)")

        run_async(work, on_result=done, on_error=lambda e: self.say(f"Hashing failed: {e}"))

    # ---------------------------------------------------------------- Ollama
    def ollama_mark(self, flag: bool) -> None:
        rows = self.active.selected_rows()
        if not rows:
            self.say("Nothing marked.")
            return
        for r in rows:
            self.db.set_ollama([r["id"]], flag, r["ollama_name"])
            self.db.log("marker", r["id"], r["name"], r["size"], r["hash"], "", "", "ok", "Ollama marker " + ("set" if flag else "removed"))
        self.refresh_view()
        self.say(f"{'Marked' if flag else 'Unmarked'} {len(rows)} file(s) as Ollama model")

    def ollama_activate(self) -> None:
        rows = [r for r in self.active.selected_rows() if r["name"].lower().endswith(".gguf")]
        if len(rows) != 1:
            self.say("Select exactly one .gguf file to activate it in Ollama.")
            return
        r = rows[0]
        if "mmproj" in r["name"].lower():
            QMessageBox.information(self, "Vision adapter", "mmproj files are vision adapters, not models - they cannot be created in Ollama on their own.")
            return
        name, ok = QInputDialog.getText(self, "Activate in Ollama", "Name of the Ollama model:",
                                        text=r["ollama_name"] or ollama_ops.sanitize_name(Path(r["name"]).stem))
        name = name.strip()
        if not ok or not name:
            return
        vram = self.ctx.vram_gb()
        text = f"Create the Ollama model '{name}' from\n{self.full_path(r)}\n({fmt_size(r['size'])}).\n\nThe Ollama server must be running. " \
               "The file is linked into Ollama's store as a hard link (no second copy). Only if that is not possible " \
               "(other drive / not NTFS) Ollama stores a copy."
        if vram_verdict(r["size"], vram)[0] != "fits":
            text += f"\n\nNote: the file is larger than 80 % of your {vram:.0f} GB VRAM - the model will run partly on CPU / RAM (slow)."
        if ConfirmDialog(self, "Activate in Ollama", text, [r["name"]], "Activate").exec() != QDialog.Accepted:
            return
        cfg, path, rid = self.ctx.cfg, self.full_path(r), r["id"]
        self.say(f"ollama create {name} ... (this can take a while)")

        def done(_out) -> None:
            self.db.log("ollama_create", rid, r["name"], r["size"], r["hash"], path, name, "ok", "ollama create")
            self.db.set_ollama([rid], True, name)
            self.ctx.log(f"Ollama: created {name} from {path}")
            self.refresh_view()
            self.say(f"Ollama model '{name}' created")

        def fail(e) -> None:
            self.db.log("ollama_create", rid, r["name"], r["size"], r["hash"], path, name, "error", str(e)[:500])
            self.ctx.log(f"Ollama create failed: {e}")
            self.say("Ollama create failed - see log")
            QMessageBox.warning(self, "Ollama create failed", str(e)[:1500])

        def work():
            sha, state = ollama_ops.create_linked(cfg, name, path, (r["hash"] or "").lower() or None)
            return sha, state

        def done2(res) -> None:
            sha, state = res
            self.db.set_hash(rid, sha)
            self.ctx.log(f"Ollama: blob {state} for {name}")
            done(None)
            self.ollama_sync(quiet=True)

        run_async(work, on_result=done2, on_error=fail)

    def ollama_sync(self, quiet: bool = False) -> None:
        cfg = self.ctx.cfg
        if not quiet:
            self.say("Reading installed Ollama models ...")

        def done(mf) -> None:
            self.mf = mf
            names = set(mf)
            self.digest_names = {}
            for n, ds in mf.items():
                for dg in ds:
                    self.digest_names.setdefault(dg, []).append(n)
            self.installed = names
            if quiet:
                self.refresh_view()
                self.update_storage()
                return
            marked = [r for r in self.db.rows(ROOT_PORTABLE) + self.db.rows(ROOT_DOWNLOADS) + self.db.rows(ROOT_ALT) if r["ollama"]]
            known = {ollama_ops.normalize(r["ollama_name"]) for r in marked if r["ollama_name"]}
            no_file = sorted(n for n in names if n not in known)
            missing = [r["name"] for r in marked if r["ollama_name"] and ollama_ops.normalize(r["ollama_name"]) not in names]
            for n in no_file:
                self.ctx.log(f"Ollama: '{n}' is installed but not linked to a file in the index")
            for n in missing:
                self.ctx.log(f"Ollama: marked file {n} is not installed in Ollama")
            self.refresh_view()
            self.say(f"Ollama: {len(names)} installed, {len(missing)} marked file(s) not installed, "
                     f"{len(no_file)} installed model(s) without a file entry (see log)")

        run_async(ollama_ops.manifests, cfg, on_result=done, on_error=lambda e: self.say(f"Ollama sync failed: {e}"))
        if not quiet:
            self.update_storage()

    # ---------------------------------------------------------------- Ollama: remove / dedupe / storage
    def _library_files(self) -> list[dict]:
        out = []
        roots = [ROOT_PORTABLE, ROOT_DOWNLOADS] + [USER_PREFIX + t for t in self.user_folders()]
        seen = set()
        for root in roots:
            for r in self.db.rows(root):
                p = self.full_path(r)
                if r["present"] and os.path.normcase(p) not in seen:
                    seen.add(os.path.normcase(p))
                    out.append({"path": p, "size": r["size"], "hash": r["hash"]})
        return out

    def update_storage(self, show: bool = False) -> None:
        cfg, files = self.ctx.cfg, self._library_files()

        def done(s) -> None:
            self.storage_lbl.setText(f"Library {fmt_size(s['library'])}  |  Ollama {fmt_size(s['ollama'])}  |  "
                                     f"Duplicate {fmt_size(s['duplicate'])}  |  Shared {fmt_size(s['shared'])}")
            if show:
                lines = [f"{p}   ({fmt_size(sz)}, match by {how})" for p, sz, how in s["dup_items"]] or ["(no duplicates)"]
                QMessageBox.information(self, "Storage overview",
                                        f"Portable library / downloads / user folders: {fmt_size(s['library'])}\n"
                                        f"Ollama store (blobs): {fmt_size(s['ollama'])}\n"
                                        f"Duplicate usage (Ollama copies of library files): {fmt_size(s['duplicate'])}\n"
                                        f"Shared via hard link (no extra space): {fmt_size(s['shared'])}\n\n"
                                        "Duplicates:\n" + "\n".join(lines[:30]))

        run_async(ollama_ops.storage, cfg, files, on_result=done, on_error=lambda e: self.say(f"Storage overview failed: {e}"))

    def _ollama_names_of(self, r: dict) -> list[str]:
        h = (r["hash"] or "").lower()
        names = list(self.digest_names.get(h, [])) if h else []
        if r["ollama_name"] and ollama_ops.normalize(r["ollama_name"]) in (self.installed or set()):
            names.append(ollama_ops.normalize(r["ollama_name"]))
        return list(dict.fromkeys(names))

    def ollama_remove(self) -> None:
        rows = self.active.selected_rows()
        pairs = [(r, n) for r in rows for n in self._ollama_names_of(r)]
        if not pairs:
            self.say("None of the marked files is installed in Ollama (sync first / compute hash).")
            return
        if QMessageBox.question(self, "Remove from Ollama", "ollama rm for:\n" + "\n".join(n for _r, n in pairs) +
                                "\n\nThe model files in the library are NOT deleted.") != QMessageBox.Yes:
            return
        cfg = self.ctx.cfg

        def work():
            res = []
            for r, n in pairs:
                try:
                    ollama_ops.remove_model(cfg, n)
                    res.append((r, n, None))
                except Exception as exc:        # noqa: BLE001
                    res.append((r, n, str(exc)))
            return res

        def done(res) -> None:
            for r, n, err in res:
                self.db.log("ollama_rm", r["id"], r["name"], r["size"], r["hash"], n, "", "error" if err else "ok", err or "ollama rm, file kept")
                self.ctx.log(f"Ollama rm {n}: {err or 'ok'}")
                if not err:
                    self.db.set_ollama([r["id"]], False)
            self.ollama_sync(quiet=True)

        run_async(work, on_result=done, on_error=lambda e: self.say(f"ollama rm failed: {e}"))

    def ollama_dedupe(self) -> None:
        rows = [r for r in self.active.selected_rows() if r["present"]]
        if not rows:
            self.say("Nothing marked.")
            return
        if QMessageBox.question(self, "Deduplicate", f"Replace Ollama's separate copy of {len(rows)} file(s) by a hard link "
                                "to the library file (frees the space of the copy).\n\nThe SHA256 is computed first if missing; "
                                "only byte-identical blobs are replaced. Stop generating with these models meanwhile.") != QMessageBox.Yes:
            return
        cfg = self.ctx.cfg
        jobs = [(r, self.full_path(r)) for r in rows]
        self.say("Deduplicating ...")

        def work():
            out = []
            for r, p in jobs:
                try:
                    sha = (r["hash"] or "").lower() or ollama_ops.sha256_of(p)
                    ok, msg = ollama_ops.dedupe_blob(cfg, p, sha)
                    out.append((r, sha, ok, msg))
                except Exception as exc:        # noqa: BLE001
                    out.append((r, None, False, str(exc)))
            return out

        def done(res) -> None:
            for r, sha, ok, msg in res:
                if sha:
                    self.db.set_hash(r["id"], sha)
                self.db.log("dedupe", r["id"], r["name"], r["size"], sha, "", "", "ok" if ok else "skipped", msg)
                self.ctx.log(f"Deduplicate {r['name']}: {msg}")
            self.say(f"Deduplicate: {sum(1 for x in res if x[2])} of {len(res)} linked (see log)")
            self.ollama_sync(quiet=True)

        run_async(work, on_result=done, on_error=lambda e: self.say(f"Deduplicate failed: {e}"))
