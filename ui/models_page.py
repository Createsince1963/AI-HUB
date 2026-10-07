"""AI Models page: local library, Hugging Face search, Civitai search, Ollama manager, download queue."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QButtonGroup, QPushButton, QCheckBox, QHeaderView, QComboBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
                               QProgressBar, QSpinBox, QSplitter, QTabBar, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from core import net
from core import gpus as gpu_info
from collections import Counter

from core.models import base_area, AREAS, CATEGORIES, MODEL_EXT, fmt_age, find_duplicates, scan_library, suggest_area, suggest_category
from core.procs import port_open
from core.secrets import get_secret
from core.system import fmt_num, fmt_size, vram_verdict
from .context import Ctx
from .manager_tab import ManagerTab
from .theme import ACCENT, BAD, MUTED, OK, WARN
from .widgets import NumItem, button, make_table, open_folder, page_header, run_async


def _item(text: str, colour: str | None = None) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    if colour:
        it.setForeground(Qt.GlobalColor.black)
        from PySide6.QtGui import QColor
        it.setForeground(QColor(colour))
    return it


def _set_bold_row(t, row: int, on: bool) -> None:
    """Models that fit into the selected VRAM are shown in bold."""
    for c in range(t.columnCount()):
        it = t.item(row, c)
        if it is not None:
            f = it.font()
            f.setBold(on)
            it.setFont(f)


_TOKEN = re.compile(r"^([\d.]+)\s*(gb|mb|b|m)$", re.I)


def est_gb(tokens: list[str]) -> list[float]:
    """Size hints such as '5.2 GB', '800 MB' or parameter counts '14b' (~0.6 GB per billion at Q4) -> GB."""
    out = []
    for tok in tokens:
        m = _TOKEN.match(tok.strip())
        if not m:
            continue
        v, unit = float(m.group(1)), m.group(2).lower()
        out.append({"gb": v, "mb": v / 1024, "b": v * 0.6, "m": v / 1000 * 0.6}[unit])
    return out


def _set_vram_header(table, col: int, vram_gb: float) -> None:
    """Column header follows the GPU VRAM selector (no hard-coded size)."""
    table.horizontalHeaderItem(col).setText(f"{vram_gb:.0f} GB VRAM")


def fits_est(tokens: list[str], vram_gb: float) -> bool:
    e = est_gb(tokens)
    return bool(e) and min(e) <= vram_gb * 0.80          # same threshold as vram_verdict "fits"


# =============================================================== local library
GROUP_COLS = {1: "area", 2: "category", 4: "type"}       # table column -> value that can be shown as tabs


class LocalTab(QWidget):
    def __init__(self, ctx: Ctx):
        super().__init__()
        self.ctx = ctx
        self.models = []
        self.dups: set[str] = set()
        cfg = ctx.cfg
        self.areas: dict[str, str] = cfg.data.setdefault("model_areas", {})       # portable path -> area chosen by the user
        for k, v in list(self.areas.items()):          # old mixed values ('LLM - QT5', 'Ollama - ...') -> plain area
            nv = base_area(v)
            if nv != v:
                self.areas[k] = nv
        self.cats: dict[str, str] = cfg.data.setdefault("model_categories", {})   # portable path -> category override by the user
        g = cfg.data.setdefault("ui", {}).get("library_group")
        self.group: str | None = g if g in GROUP_COLS.values() else None            # column shown as tabs
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by name...")
        self.filter.textChanged.connect(self.apply_filter)
        self.cat = QComboBox()
        self.cat.addItem("All categories")
        self.cat.currentIndexChanged.connect(self.apply_filter)
        self.only_dups = QCheckBox("Duplicates only")
        self.only_dups.toggled.connect(self.apply_filter)
        top.addWidget(self.filter, 1)
        top.addWidget(self.cat)
        top.addWidget(self.only_dups)
        top.addWidget(button("Rescan", "Primary", self.scan))
        lay.addLayout(top)
        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setDrawBase(True)
        self.tabs.setVisible(False)
        self.tabs.currentChanged.connect(lambda _i: self.apply_filter())
        lay.addWidget(self.tabs)
        self.table = make_table(["Name", "Einsatzbereich", "Kategorie", "Size", "Type", "Modified"], 0)
        self._setup_columns()
        self.table.setSortingEnabled(True)
        self.table.doubleClicked.connect(self._double_clicked)
        hh = self.table.horizontalHeader()
        hh.setContextMenuPolicy(Qt.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._header_menu)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
        self.table.horizontalHeaderItem(1).setToolTip("What the file is used for (ComfyUI, LLM, LoRA, Video ...). Double-click a cell to edit.")
        self.table.horizontalHeaderItem(2).setToolTip("Folder / category of the file. Double-click a cell to overwrite the value (metadata only, the file is not moved).")
        lay.addWidget(self.table, 1)
        bot = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setObjectName("Muted")
        bot.addWidget(self.summary, 1)
        bot.addWidget(button("Show in Explorer", "", self.reveal))
        bot.addWidget(button("Copy path", "", self.copy_path))
        bot.addWidget(button("Copy to...", "", lambda: self.transfer(move=False)))
        bot.addWidget(button("Move to...", "", lambda: self.transfer(move=True)))
        bot.addWidget(button("Delete...", "Danger", self.delete))
        lay.addLayout(bot)

    # ---------------------------------------------------------------- resizable columns
    _DEFAULT_W = [380, 130, 130, 90, 70, 100]

    def _setup_columns(self) -> None:
        """All columns can be resized by dragging the header border; widths are remembered in the config."""
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        hh.setMinimumSectionSize(40)
        saved = self.ctx.cfg.data.setdefault("ui", {}).get("library_col_widths") or []
        for c in range(self.table.columnCount()):
            w = saved[c] if c < len(saved) and isinstance(saved[c], int) and saved[c] >= 40 else self._DEFAULT_W[c]
            self.table.setColumnWidth(c, w)
        hh.sectionResized.connect(self._col_resized)
        self._col_timer = None

    def _col_resized(self, _i, _old, _new) -> None:
        from PySide6.QtCore import QTimer
        if self._col_timer is None:
            self._col_timer = QTimer(self)
            self._col_timer.setSingleShot(True)
            self._col_timer.timeout.connect(self._save_widths)
        self._col_timer.start(600)          # save once after dragging stops

    def _save_widths(self) -> None:
        self.ctx.cfg.data.setdefault("ui", {})["library_col_widths"] = [self.table.columnWidth(c) for c in range(self.table.columnCount())]
        self._save_cfg()

    def scan(self) -> None:
        root = self.ctx.cfg.path("models")
        dl = self.ctx.cfg.path("downloads")
        self.summary.setText(f"Scanning {root} ...")

        def scan_all():
            found = scan_library(root)
            if os.path.normcase(str(dl)) != os.path.normcase(str(root)):
                found += scan_library(dl, skip=frozenset({"old"}))        # X_DOWNLOADS: the "old" folder is ignored
            return found

        run_async(scan_all, on_result=self._scanned, on_error=lambda e: self.summary.setText(e))

    def _scanned(self, models) -> None:
        self.models = models
        self.dups = find_duplicates(models)
        cats = sorted({self._cat(m) for m in models})
        cur = self.cat.currentText()
        self.cat.blockSignals(True)
        self.cat.clear()
        self.cat.addItem("All categories")
        self.cat.addItems(cats)
        i = self.cat.findText(cur)
        self.cat.setCurrentIndex(max(i, 0))
        self.cat.blockSignals(False)
        self.apply_filter()

    # ---------------------------------------------------------------- areas + grouping
    def _key(self, m) -> str:
        return self.ctx.cfg.tokenize(m.path)

    def _cat(self, m) -> str:
        """Category shown in the table: the user's override, else the folder name."""
        return self.cats.get(self._key(m)) or m.category

    def _area(self, m) -> str:
        return self.areas.get(self._key(m)) or suggest_area(m.name, m.category, m.ext)

    def _value(self, m, col: str) -> str:
        v = {"area": self._area, "category": self._cat, "type": lambda x: x.ext.lstrip(".")}[col](m)
        return v or "(none)"

    def _rebuild_tabs(self, base: list) -> str | None:
        """Tab per distinct value of the grouped column (with file count). Returns the selected value or None = all."""
        if not self.group:
            self.tabs.setVisible(False)
            return None
        cur = self.tabs.tabData(self.tabs.currentIndex()) if self.tabs.count() else ""
        counts = Counter(self._value(m, self.group) for m in base)
        self.tabs.blockSignals(True)
        while self.tabs.count():
            self.tabs.removeTab(0)
        self.tabs.addTab(f"All ({len(base)})")
        self.tabs.setTabData(0, "")
        for v in sorted(counts, key=str.lower):
            k = self.tabs.addTab(f"{v} ({counts[v]})")
            self.tabs.setTabData(k, v)
        idx = next((k for k in range(self.tabs.count()) if self.tabs.tabData(k) == cur), 0)
        self.tabs.setCurrentIndex(idx)
        self.tabs.blockSignals(False)
        self.tabs.setVisible(True)
        return self.tabs.tabData(idx) or None

    def apply_filter(self) -> None:
        text = self.filter.text().lower()
        cat = self.cat.currentText()
        base = [m for m in self.models
                if (cat == "All categories" or self._cat(m) == cat) and text in m.name.lower()
                and (not self.only_dups.isChecked() or m.path in self.dups)]
        sel = self._rebuild_tabs(base)
        rows = [m for m in base if sel is None or self._value(m, self.group) == sel]
        t = self.table
        vram = self.ctx.vram_gb()
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for i, m in enumerate(rows):
            name = _item(m.name, BAD if m.path in self.dups else None)
            name.setData(Qt.UserRole, m.path)
            name.setToolTip(m.path)
            t.setItem(i, 0, name)
            own = self._key(m) in self.areas
            area = _item(self._area(m), None if own else MUTED)         # grey = automatic default, black = set by you
            area.setToolTip("set by you" if own else "automatic default (from name / folder / type) - double-click to change")
            t.setItem(i, 1, area)
            ownc = self._key(m) in self.cats
            catit = _item(self._cat(m), None if ownc else MUTED)          # grey = folder name, black = overwritten by you
            catit.setToolTip(f"set by you (folder: {m.category})" if ownc else "taken from the folder - double-click to overwrite")
            t.setItem(i, 2, catit)
            t.setItem(i, 3, NumItem(fmt_size(m.size), m.size))
            t.setItem(i, 4, _item(m.ext.lstrip(".")))
            t.setItem(i, 5, NumItem(fmt_age(m.mtime), m.mtime))
            _set_bold_row(t, i, vram_verdict(m.size, vram)[0] == "fits")
        t.setSortingEnabled(True)
        total = sum(m.size for m in rows)
        nfit = sum(1 for m in rows if vram_verdict(m.size, vram)[0] == "fits")
        self.summary.setText(f"{len(rows)} of {len(self.models)} files  |  {fmt_size(total)} shown  |  "
                             f"{len(self.dups)} duplicate files (red)  |  bold = fits in {vram:.0f} GB VRAM ({nfit})")

    def _save_cfg(self) -> None:
        try:
            self.ctx.cfg.save()
        except OSError as exc:
            self.ctx.log(f"Could not save config: {exc}")

    def _header_menu(self, pos) -> None:
        """Right click on a column header: turn the values of that column into tabs."""
        col = self.table.horizontalHeader().logicalIndexAt(pos)
        menu = QMenu(self)
        key = GROUP_COLS.get(col)
        if key:
            title = self.table.horizontalHeaderItem(col).text()
            act = menu.addAction(f'Show "{title}" values as tabs')
            act.setCheckable(True)
            act.setChecked(self.group == key)
            act.triggered.connect(lambda _c=False, k=key: self.set_group(None if self.group == k else k))
        else:
            hint = menu.addAction("Tabs are available for Einsatzbereich, Kategorie and Type")
            hint.setEnabled(False)
        if self.group:
            menu.addSeparator()
            menu.addAction("Remove tabs").triggered.connect(lambda: self.set_group(None))
        menu.exec(self.table.horizontalHeader().mapToGlobal(pos))

    def set_group(self, key: str | None) -> None:
        self.group = key
        self.ctx.cfg.data.setdefault("ui", {})["library_group"] = key
        self._save_cfg()
        if self.tabs.count():
            self.tabs.setCurrentIndex(0)
        self.apply_filter()

    def _selected_models(self) -> list:
        paths = {self.table.item(ix.row(), 0).data(Qt.UserRole) for ix in self.table.selectionModel().selectedRows()}
        return [m for m in self.models if m.path in paths]

    def _area_choices(self) -> list[str]:
        return list(dict.fromkeys(AREAS + sorted(set(self.areas.values()), key=str.lower)))

    def set_area(self, models: list, area: str | None) -> None:
        """area None = back to the automatic default."""
        for m in models:
            if area:
                self.areas[self._key(m)] = area
            else:
                self.areas.pop(self._key(m), None)
        self._save_cfg()
        self.apply_filter()

    def _cat_choices(self) -> list[str]:
        folders = {m.category for m in self.models}
        return list(dict.fromkeys(CATEGORIES + sorted(folders | set(self.cats.values()), key=str.lower)))

    def set_category(self, models: list, cat: str | None) -> None:
        """cat None = back to the folder name."""
        for m in models:
            if cat:
                self.cats[self._key(m)] = cat
            else:
                self.cats.pop(self._key(m), None)
        self._save_cfg()
        self._scanned(self.models)              # refresh the filter list + table

    def _new_area(self, models: list) -> None:
        name, ok = QInputDialog.getText(self, "New area", "Name of the new area (e.g. Audio, Face, Depth):")
        if ok and name.strip():
            self.set_area(models, name.strip())

    def _row_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is not None and not self.table.item(item.row(), 0).isSelected():
            self.table.selectRow(item.row())
        models = self._selected_models()
        if not models:
            return
        menu = QMenu(self)
        sub = menu.addMenu(f"Set area ({len(models)} file{'s' if len(models) > 1 else ''})")
        for a in self._area_choices():
            sub.addAction(a).triggered.connect(lambda _c=False, a=a: self.set_area(models, a))
        sub.addSeparator()
        sub.addAction("New area...").triggered.connect(lambda: self._new_area(models))
        menu.addAction("Reset area to automatic default").triggered.connect(lambda: self.set_area(models, None))
        csub = menu.addMenu(f"Set category ({len(models)} file{'s' if len(models) > 1 else ''})")
        for c in self._cat_choices():
            csub.addAction(c).triggered.connect(lambda _c=False, c=c: self.set_category(models, c))
        menu.addAction("Reset category to folder name").triggered.connect(lambda: self.set_category(models, None))
        menu.addSeparator()
        menu.addAction("Show in Explorer").triggered.connect(self.reveal)
        menu.addAction("Copy path").triggered.connect(self.copy_path)
        menu.addSeparator()
        menu.addAction("Copy to folder...").triggered.connect(lambda: self.transfer(move=False))
        menu.addAction("Move to folder...").triggered.connect(lambda: self.transfer(move=True))
        menu.addAction("Delete...").triggered.connect(self.delete)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _double_clicked(self, index) -> None:
        if index.column() not in (1, 2):
            self.reveal()
            return
        models = self._selected_models()
        if not models:
            return
        if index.column() == 2:
            choices = self._cat_choices()
            cur = self._cat(models[0])
            cat, ok = QInputDialog.getItem(self, "Kategorie", "Kategorie for the selected file(s) - pick one or type a new value:",
                                           choices, choices.index(cur) if cur in choices else 0, True)
            if ok and cat.strip():
                self.set_category(models, cat.strip())
            return
        choices = self._area_choices()
        cur = self._area(models[0])
        area, ok = QInputDialog.getItem(self, "Area", "Area for the selected file(s) - pick one or type a new name:",
                                        choices, choices.index(cur) if cur in choices else 0, True)
        if ok and area.strip():
            self.set_area(models, area.strip())

    def _sel_path(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole) if rows else None

    def reveal(self) -> None:
        p = self._sel_path()
        if p:
            open_folder(p, select=True)

    def copy_path(self) -> None:
        p = self._sel_path()
        if p:
            QGuiApplication.clipboard().setText(p)

    def delete(self) -> None:
        models = self._selected_models()
        if not models:
            return
        names = "\n".join(m.name for m in models[:15]) + (f"\n... +{len(models) - 15} more" if len(models) > 15 else "")
        total = sum(m.size for m in models)
        if QMessageBox.warning(self, "Delete model", f"Permanently delete {len(models)} file(s) ({fmt_size(total)})?\n\n{names}",
                               QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
            return
        for m in models:
            try:
                os.remove(m.path)
                self.areas.pop(self._key(m), None)
                self.cats.pop(self._key(m), None)
                self.ctx.log(f"Deleted {m.path}")
            except OSError as exc:
                self.ctx.log(f"Delete failed: {m.path}: {exc}")
        self._save_cfg()
        self.scan()

    def transfer(self, move: bool) -> None:
        """Copy or move the selected files to a folder chosen in the file dialog (existing files are never overwritten)."""
        models = self._selected_models()
        if not models:
            return
        start = str(Path(models[0].path).parent)
        dest = QFileDialog.getExistingDirectory(self, "Move to folder" if move else "Copy to folder", start)
        if not dest:
            return
        dest_dir = Path(dest)
        pairs = [(m, dest_dir / m.name) for m in models]
        pairs = [(m, t) for m, t in pairs if os.path.normcase(str(t)) != os.path.normcase(m.path)]
        clash = [t.name for _m, t in pairs if t.exists()]
        if clash:
            QMessageBox.warning(self, "Already exists", "Skipped, target file already exists:\n\n" + "\n".join(clash[:15]))
            pairs = [(m, t) for m, t in pairs if not t.exists()]
        if not pairs:
            return
        verb = "Moving" if move else "Copying"
        self.summary.setText(f"{verb} {len(pairs)} file(s) to {dest_dir} ...")

        def work():
            done, errors = [], []
            for m, t in pairs:
                try:
                    if move:
                        shutil.move(m.path, str(t))
                    else:
                        shutil.copy2(m.path, str(t))
                    done.append((m, t))
                except OSError as exc:
                    errors.append(f"{m.name}: {exc}")
            return done, errors

        def finished(res) -> None:
            done, errors = res
            for m, t in done:
                self.ctx.log(f"{'Moved' if move else 'Copied'} {m.path} -> {t}")
                area = self.areas.get(self._key(m))
                if area:                                    # keep the area chosen by the user
                    if move:
                        self.areas.pop(self._key(m), None)
                    self.areas[self.ctx.cfg.tokenize(str(t))] = area
                cat = self.cats.get(self._key(m))
                if cat:                                     # keep the category chosen by the user
                    if move:
                        self.cats.pop(self._key(m), None)
                    self.cats[self.ctx.cfg.tokenize(str(t))] = cat
            self._save_cfg()
            for e in errors:
                self.ctx.log(f"Transfer failed: {e}")
            if errors:
                QMessageBox.warning(self, "Transfer errors", "\n".join(errors[:15]))
            self.scan()

        run_async(work, on_result=finished, on_error=lambda e: self.summary.setText(str(e)))


# =============================================================== Hugging Face
HF_TASKS = [("Any task", ""), ("Text to image", "text-to-image"), ("Image to image", "image-to-image"),
            ("Text to video", "text-to-video"), ("Image to video", "image-to-video"),
            ("Text generation (LLM)", "text-generation"), ("Image + text to text (VLM)", "image-text-to-text"),
            ("Embeddings", "feature-extraction"), ("Speech recognition", "automatic-speech-recognition"),
            ("Text to speech", "text-to-speech"), ("Depth estimation", "depth-estimation")]
HF_FORMATS = [("Any format", ""), ("GGUF", "gguf"), ("Safetensors", "safetensors"), ("Diffusers", "diffusers"),
              ("ONNX", "onnx")]
HF_SORT = [("Most downloads", "downloads"), ("Most likes", "likes"), ("Recently updated", "lastModified"),
           ("Trending", "trendingScore")]


class HFTab(QWidget):
    def __init__(self, ctx: Ctx):
        super().__init__()
        self.ctx = ctx
        self.results: list[dict] = []
        self.files: list[dict] = []
        self.repo = ""
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setPlaceholderText("Search Hugging Face models  (e.g. flux, wan 2.2, qwen3, sdxl lora ...)")
        self.q.returnPressed.connect(self.search)
        self.task = QComboBox()
        for n, v in HF_TASKS:
            self.task.addItem(n, v)
        self.fmt = QComboBox()
        for n, v in HF_FORMATS:
            self.fmt.addItem(n, v)
        self.sort = QComboBox()
        for n, v in HF_SORT:
            self.sort.addItem(n, v)
        top.addWidget(self.q, 6)                       # search field ~40 % narrower, the controls follow directly
        for w in (self.task, self.fmt, self.sort):
            top.addWidget(w)
        top.addWidget(button("Search", "Primary", self.search))
        top.addWidget(button("Open on huggingface.co", "", self.open_web))
        top.addStretch(4)
        lay.addLayout(top)

        split = QSplitter(Qt.Vertical)
        self.table = make_table(["Model", "Task", "Downloads", "Likes", "Updated", "Access"], 0)
        self.table.itemSelectionChanged.connect(self.pick)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._model_menu)
        split.addWidget(self.table)
        lower = QWidget()
        ll = QVBoxLayout(lower)
        ll.setContentsMargins(0, 4, 0, 0)
        self.files_label = QLabel("Select a model to list its files")
        self.files_label.setObjectName("Muted")
        ll.addWidget(self.files_label)
        self.ftable = make_table(["File", "Size", "VRAM", "Target folder"], 0)
        self.ftable.setSelectionMode(self.ftable.SelectionMode.MultiSelection)     # click marks / unmarks a file
        self.ftable.itemSelectionChanged.connect(self._update_dl_button)
        ll.addWidget(self.ftable, 1)
        row = QHBoxLayout()
        self.dl_btn = button("Download selected", "Primary", self.download)      # left: batch download of the marked files
        self.dl_btn.setEnabled(False)
        row.addWidget(self.dl_btn)
        row.addWidget(button("Select all", "", self.ftable.selectAll))
        row.addWidget(button("Clear", "", self.ftable.clearSelection))
        self.only_models = QCheckBox("Model files only")
        self.only_models.setChecked(True)
        self.only_models.toggled.connect(self.fill_files)
        row.addWidget(self.only_models)
        row.addStretch(1)
        row.addWidget(QLabel("Save to"))
        self.target = QComboBox()
        self.target.addItem("auto (by file name)", "")
        for c in CATEGORIES:
            self.target.addItem(c, c)
        row.addWidget(self.target)
        ll.addLayout(row)
        split.addWidget(lower)
        split.setSizes([260, 300])
        lay.addWidget(split, 1)
        self.status = QLabel("")
        self.status.setObjectName("Muted")
        lay.addWidget(self.status)

    def token(self) -> str:
        return get_secret("hf_token")

    def search(self) -> None:
        self.status.setText("Searching...")
        tags = [self.fmt.currentData()] if self.fmt.currentData() else []
        run_async(net.hf_search, self.q.text().strip(), self.task.currentData(), tags, self.sort.currentData(), 50,
                  self.token(), on_result=self._results, on_error=lambda e: self.status.setText(f"Error: {e}"))

    def _results(self, res: list[dict]) -> None:
        self.results = res
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(len(res))
        for i, m in enumerate(res):
            it = _item(m["id"])
            it.setData(Qt.UserRole, i)
            t.setItem(i, 0, it)
            t.setItem(i, 1, _item(m["pipeline"]))
            t.setItem(i, 2, NumItem(fmt_num(m["downloads"]), m["downloads"] or 0))
            t.setItem(i, 3, NumItem(fmt_num(m["likes"]), m["likes"] or 0))
            t.setItem(i, 4, _item(m["updated"]))
            t.setItem(i, 5, _item("gated - token + licence" if m["gated"] else "open", BAD if m["gated"] else None))
            for c in range(6):                                        # mouse hover shows the source URL
                t.item(i, c).setToolTip(f"{net.HF}/{m['id']}")
        t.setSortingEnabled(True)
        self.status.setText(f"{len(res)} models")

    def pick(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        idx = self.table.item(rows[0].row(), 0).data(Qt.UserRole)
        self.repo = self.results[idx]["id"]
        self.files_label.setText(f"Loading files of {self.repo} ...")
        run_async(net.hf_files, self.repo, self.token(), on_result=self._files,
                  on_error=lambda e: self.files_label.setText(f"Error: {e}"))

    def _files(self, files: list[dict]) -> None:
        self.files = sorted(files, key=lambda f: -(f["size"] or 0))
        self.fill_files()

    def fill_files(self) -> None:
        meta = next((m for m in self.results if m["id"] == self.repo), {})
        rows = [f for f in self.files if not self.only_models.isChecked()
                or Path(f["path"]).suffix.lower() in MODEL_EXT]
        t = self.ftable
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        vram = self.ctx.vram_gb()
        _set_vram_header(t, 2, vram)
        for i, f in enumerate(rows):
            it = _item(f["path"])
            it.setData(Qt.UserRole, f)
            t.setItem(i, 0, it)
            t.setItem(i, 1, NumItem(fmt_size(f["size"]), f["size"] or 0))
            label, col = vram_verdict(f["size"], vram)
            t.setItem(i, 2, _item(label, col))
            t.setItem(i, 3, _item(suggest_category(f["path"], meta.get("tags"), meta.get("pipeline", ""))))
            _set_bold_row(t, i, label == "fits")
        t.setSortingEnabled(True)
        self.files_label.setText(f"{self.repo}  -  {len(rows)} files shown ({len(self.files)} in repo)")

    def open_web(self) -> None:
        if self.repo:
            import webbrowser
            webbrowser.open(f"{net.HF}/{self.repo}")

    def _model_menu(self, pos) -> None:
        """Right click on a model: mark it and offer 'Open source URL' (default browser)."""
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        self.table.selectRow(row)
        idx = self.table.item(row, 0).data(Qt.UserRole)
        url = f"{net.HF}/{self.results[idx]['id']}"
        menu = QMenu(self)
        a_open = menu.addAction("Open source URL")
        a_copy = menu.addAction("Copy source URL")
        act = menu.exec(self.table.viewport().mapToGlobal(pos))
        if act is a_open:
            import webbrowser
            webbrowser.open(url)
        elif act is a_copy:
            QGuiApplication.clipboard().setText(url)

    def _update_dl_button(self) -> None:
        n = len(self.ftable.selectionModel().selectedRows())
        self.dl_btn.setText(f"Download selected ({n})" if n else "Download selected")
        self.dl_btn.setEnabled(n > 0)

    def download(self) -> None:
        rows = self.ftable.selectionModel().selectedRows()
        if not rows:
            self.status.setText("Select one or more files first")
            return
        root = self.ctx.cfg.path("downloads")          # new downloads land in X_DOWNLOADS
        lib = self.ctx.cfg.path("models")
        hdr = net.hf_headers(self.token())
        queued = 0
        for r in rows:
            f = self.ftable.item(r.row(), 0).data(Qt.UserRole)
            cat = self.target.currentData() or self.ftable.item(r.row(), 3).text()
            dest = root / cat / Path(f["path"]).name
            if dest.exists() or (lib / cat / dest.name).exists():
                self.ctx.log(f"Skipped (already exists): {dest}")
                continue
            self.ctx.downloads.add_file(f"{self.repo}/{f['path']}", net.hf_download_url(self.repo, f["path"]),
                                        str(dest), hdr, f["size"] or 0)
            self.ctx.log(f"Queued: {self.repo}/{f['path']} -> {cat}")
            queued += 1
        self.status.setText(f"{queued} of {len(rows)} files queued")
        self.ftable.clearSelection()
        self.ctx.goto("downloads")


# =============================================================== Civitai
CIVITAI_TARGET = {"Checkpoint": "checkpoints", "LORA": "loras", "LoCon": "loras", "DoRA": "loras",
                  "TextualInversion": "embeddings", "Controlnet": "controlnet", "VAE": "vae", "Upscaler": "upscale_models"}


class CivitaiTab(QWidget):
    def __init__(self, ctx: Ctx):
        super().__init__()
        self.ctx = ctx
        self.results: list[dict] = []
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setPlaceholderText("Search Civitai (checkpoints, LoRAs, embeddings ...)")
        self.q.returnPressed.connect(self.search)
        self.type = QComboBox()
        self.type.addItem("All types", "")
        for t in net.CIVITAI_TYPES:
            self.type.addItem(t, t)
        self.sort = QComboBox()
        for s in ("Most Downloaded", "Highest Rated", "Newest"):
            self.sort.addItem(s, s)
        self.nsfw = QCheckBox("Include NSFW")
        top.addWidget(self.q, 1)
        for w in (self.type, self.sort, self.nsfw):
            top.addWidget(w)
        top.addWidget(button("Search", "Primary", self.search))
        lay.addLayout(top)
        split = QSplitter(Qt.Vertical)
        self.table = make_table(["Model", "Type", "Downloads", "Likes", "Versions"], 0)
        self.table.itemSelectionChanged.connect(self.pick)
        split.addWidget(self.table)
        lower = QWidget()
        ll = QVBoxLayout(lower)
        ll.setContentsMargins(0, 4, 0, 0)
        vrow = QHBoxLayout()
        vrow.addWidget(QLabel("Version"))
        self.version = QComboBox()
        self.version.currentIndexChanged.connect(self.fill_files)
        vrow.addWidget(self.version, 1)
        ll.addLayout(vrow)
        self.ftable = make_table(["File", "Size", "VRAM", "Kind"], 0)
        self.ftable.setSelectionMode(self.ftable.SelectionMode.ExtendedSelection)
        ll.addWidget(self.ftable, 1)
        row = QHBoxLayout()
        self.note = QLabel("Many Civitai downloads need an API key (Settings > Access tokens).")
        self.note.setObjectName("Muted")
        row.addWidget(self.note, 1)
        row.addWidget(button("Open on civitai.com", "", self.open_web))
        row.addWidget(button("Download selected", "Primary", self.download))
        ll.addLayout(row)
        split.addWidget(lower)
        split.setSizes([260, 260])
        lay.addWidget(split, 1)
        self.status = QLabel("")
        self.status.setObjectName("Muted")
        lay.addWidget(self.status)

    def search(self) -> None:
        self.status.setText("Searching...")
        run_async(net.civitai_search, self.q.text().strip(), self.type.currentData(), self.sort.currentData(), 30,
                  self.nsfw.isChecked(), get_secret("civitai_key"), on_result=self._results,
                  on_error=lambda e: self.status.setText(f"Error: {e}"))

    def _results(self, res: list[dict]) -> None:
        self.results = res
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(len(res))
        for i, m in enumerate(res):
            it = _item(m["name"])
            it.setData(Qt.UserRole, i)
            t.setItem(i, 0, it)
            t.setItem(i, 1, _item(m["type"]))
            t.setItem(i, 2, NumItem(fmt_num(m["downloads"]), m["downloads"] or 0))
            t.setItem(i, 3, NumItem(fmt_num(m["likes"]), m["likes"] or 0))
            t.setItem(i, 4, NumItem(str(len(m["versions"])), len(m["versions"])))
        t.setSortingEnabled(True)
        self.status.setText(f"{len(res)} models")

    def _model(self) -> dict | None:
        rows = self.table.selectionModel().selectedRows()
        return self.results[self.table.item(rows[0].row(), 0).data(Qt.UserRole)] if rows else None

    def pick(self) -> None:
        m = self._model()
        self.version.blockSignals(True)
        self.version.clear()
        for i, v in enumerate(m["versions"] if m else []):
            self.version.addItem(f"{v['name']}  ({v['base']})" if v["base"] else v["name"], i)
        self.version.blockSignals(False)
        self.fill_files()

    def fill_files(self) -> None:
        m = self._model()
        t = self.ftable
        t.setSortingEnabled(False)
        t.setRowCount(0)
        if m and self.version.currentData() is not None:
            files = m["versions"][self.version.currentData()]["files"]
            t.setRowCount(len(files))
            for i, f in enumerate(files):
                it = _item(f["name"])
                it.setData(Qt.UserRole, f)
                t.setItem(i, 0, it)
                t.setItem(i, 1, NumItem(fmt_size(f["size"]), f["size"]))
                label, col = vram_verdict(f["size"], self.ctx.vram_gb())
                _set_vram_header(t, 2, self.ctx.vram_gb())
                t.setItem(i, 2, _item(label, col))
                t.setItem(i, 3, _item(f"{f['type']} {f['format']}".strip()))
                _set_bold_row(t, i, label == "fits")
        t.setSortingEnabled(True)

    def open_web(self) -> None:
        m = self._model()
        if m:
            import webbrowser
            webbrowser.open(f"{net.CIVITAI}/models/{m['id']}")

    def download(self) -> None:
        m = self._model()
        rows = self.ftable.selectionModel().selectedRows()
        if not m or not rows:
            self.status.setText("Select a model and file first")
            return
        cat = CIVITAI_TARGET.get(m["type"], "checkpoints")
        key = get_secret("civitai_key")
        for r in rows:
            f = self.ftable.item(r.row(), 0).data(Qt.UserRole)
            dest = self.ctx.cfg.path("downloads") / cat / f["name"]          # new downloads land in X_DOWNLOADS
            if dest.exists() or (self.ctx.cfg.path("models") / cat / f["name"]).exists():
                self.ctx.log(f"Skipped (already exists): {dest}")
                continue
            self.ctx.downloads.add_file(f"civitai: {m['name']} / {f['name']}", net.civitai_url(f["url"], key),
                                        str(dest), {}, f["size"])
            self.ctx.log(f"Queued: {f['name']} -> {cat}")
        self.ctx.goto("downloads")


# =============================================================== Ollama
class OllamaTab(QWidget):
    def __init__(self, ctx: Ctx):
        super().__init__()
        self.ctx = ctx
        lay = QVBoxLayout(self)
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        lay.addWidget(self.banner)
        split = QSplitter(Qt.Vertical)
        up = QWidget()
        ul = QVBoxLayout(up)
        ul.setContentsMargins(0, 0, 0, 0)
        self.inst = make_table(["Installed model", "Size", "Parameters", "Quantization", "Modified", "In VRAM"], 0)
        ul.addWidget(self.inst, 1)
        r = QHBoxLayout()
        r.addWidget(button("Refresh", "", self.refresh))
        r.addWidget(button("Unload from VRAM", "", self.unload))
        r.addWidget(button("Delete model", "Danger", self.delete))
        r.addStretch(1)
        ul.addLayout(r)
        split.addWidget(up)

        low = QWidget()
        ll = QVBoxLayout(low)
        ll.setContentsMargins(0, 4, 0, 0)
        row = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setPlaceholderText("Search the Ollama library, or type an exact tag to pull (e.g. qwen3:14b, hf.co/user/repo:Q4_K_M)")
        self.q.returnPressed.connect(self.search)
        row.addWidget(self.q, 1)
        row.addWidget(button("Search library", "", self.search))
        row.addWidget(button("Pull", "Primary", self.pull_typed))
        ll.addLayout(row)
        self.lib = make_table(["Model", "Description", "Sizes", "Pulls"], 1)
        ll.addWidget(self.lib, 1)
        r2 = QHBoxLayout()
        self._suggested = True
        self.lib_note = QLabel("")
        self.lib_note.setObjectName("Muted")
        r2.addWidget(self.lib_note, 1)
        r2.addWidget(button("Pull selected", "Primary", self.pull_selected))
        ll.addLayout(r2)
        split.addWidget(low)
        split.setSizes([200, 300])
        lay.addWidget(split, 1)
        self.lib.doubleClicked.connect(lambda: self.pull_selected())
        self.show_suggested()

    def _suggest_note(self) -> str:
        return (f"Suggested for a {self.ctx.vram_gb():.0f} GB GPU (Q4 quantization). Bold = fits in VRAM. "
                "Double-click a row to pull it.")

    def show_suggested(self) -> None:
        rows = [{"name": n, "desc": d, "sizes": [s], "pulls": "suggested"} for n, d, s in net.OLLAMA_SUGGESTED]
        self._suggested = True
        self.lib_note.setText(self._suggest_note())
        self._fill_lib(rows)

    def rebold(self) -> None:
        """VRAM setting changed: refresh the bold marking without reloading anything."""
        v = self.ctx.vram_gb()
        for i in range(self.inst.rowCount()):
            it = self.inst.item(i, 1)
            _set_bold_row(self.inst, i, vram_verdict(getattr(it, "key", 0), v)[0] == "fits")
        for i in range(self.lib.rowCount()):
            it = self.lib.item(i, 2)
            _set_bold_row(self.lib, i, bool(it) and fits_est([x for x in it.text().split(",")], v))
        if self._suggested:
            self.lib_note.setText(self._suggest_note())

    def refresh(self) -> None:
        cl = self.ctx.ollama()
        self.ctx.downloads.set_ollama(cl)
        run_async(lambda: (cl.tags(), cl.ps()), on_result=self._installed, on_error=self._offline)

    def _offline(self, err: str) -> None:
        """Ollama does not answer. If the llama.cpp server (CPP) is up on its own port, show that instead of an error."""
        self.inst.setRowCount(0)
        cfg = self.ctx.cfg
        lport = cfg.port("llamacpp")
        if port_open(lport, "127.0.0.1", 0.4):
            base = f"http://127.0.0.1:{lport}"
            run_async(lambda: net.http_json(f"{base}/v1/models", timeout=4),
                      on_result=lambda d: self._llama_only(lport, d), on_error=lambda _e: self._llama_only(lport, {}))
            return
        self.banner.setText(f"Neither Ollama (port {cfg.port('ollama')}) nor the llama.cpp server (port {lport}) is running. "
                            "Start one of them on the Dashboard, then press Refresh.")
        self.banner.setStyleSheet(f"color:{BAD}; font-weight:600;")

    def _llama_only(self, lport: int, data: dict) -> None:
        models = data.get("data", []) if isinstance(data, dict) else []
        names = ", ".join(str(m.get("id", "?")) for m in models) or "model is still loading"
        self.banner.setText(f"llama.cpp server is active on port {lport} ({names}). Ollama itself is not running "
                            f"(port {self.ctx.cfg.port('ollama')}) - start it on the Dashboard to manage Ollama models.")
        self.banner.setStyleSheet(f"color:{WARN}; font-weight:600;")
        t = self.inst
        t.setSortingEnabled(False)
        t.setRowCount(len(models))
        for i, m in enumerate(models):
            meta = m.get("meta") or {}
            t.setItem(i, 0, _item(str(m.get("id", ""))))
            t.setItem(i, 1, NumItem(fmt_size(meta.get("size")), meta.get("size", 0) or 0))
            t.setItem(i, 2, _item(fmt_num(meta.get("n_params")) if meta.get("n_params") else ""))
            t.setItem(i, 3, _item("llama.cpp server"))
            t.setItem(i, 4, _item(""))
            t.setItem(i, 5, _item("yes", OK))
        t.setSortingEnabled(True)

    def _installed(self, data) -> None:
        tags, ps = data
        loaded = {m.get("name") for m in ps}
        self.banner.setText(f"Ollama online - {len(tags)} models installed, {len(loaded)} loaded in VRAM")
        self.banner.setStyleSheet(f"color:{OK}; font-weight:600;")
        t = self.inst
        t.setSortingEnabled(False)
        t.setRowCount(len(tags))
        for i, m in enumerate(tags):
            d = m.get("details", {})
            t.setItem(i, 0, _item(m["name"]))
            t.setItem(i, 1, NumItem(fmt_size(m.get("size")), m.get("size", 0)))
            t.setItem(i, 2, _item(d.get("parameter_size", "")))
            t.setItem(i, 3, _item(d.get("quantization_level", "")))
            t.setItem(i, 4, _item((m.get("modified_at") or "")[:10]))
            t.setItem(i, 5, _item("yes" if m["name"] in loaded else "", OK))
            _set_bold_row(t, i, vram_verdict(m.get("size"), self.ctx.vram_gb())[0] == "fits")
        t.setSortingEnabled(True)

    def _sel_installed(self) -> str | None:
        rows = self.inst.selectionModel().selectedRows()
        if not rows:
            return None
        if (self.inst.item(rows[0].row(), 3) or _item("")).text() == "llama.cpp server":
            return None                      # llama.cpp entries cannot be unloaded / deleted through Ollama
        return self.inst.item(rows[0].row(), 0).text()

    def unload(self) -> None:
        n = self._sel_installed()
        if n:
            cl = self.ctx.ollama()
            run_async(cl.unload, n, on_result=lambda _: self.refresh(), on_error=lambda e: self.ctx.log(e))

    def delete(self) -> None:
        n = self._sel_installed()
        if n and QMessageBox.question(self, "Delete", f"Delete model {n}?") == QMessageBox.Yes:
            cl = self.ctx.ollama()
            run_async(cl.delete, n, on_result=lambda _: (self.ctx.log(f"Deleted ollama model {n}"), self.refresh()),
                      on_error=lambda e: self.ctx.log(e))

    def search(self) -> None:
        q = self.q.text().strip()
        if not q:
            self.show_suggested()
            return
        self.lib_note.setText("Searching ollama.com ...")
        run_async(net.ollama_library_search, q, on_result=self._lib_result, on_error=self._lib_error)

    def _lib_error(self, e: str) -> None:
        self.lib_note.setText(f"Library search failed ({e}). You can still type an exact tag and press Pull.")

    def _lib_result(self, rows: list[dict]) -> None:
        self._suggested = False
        self.lib_note.setText(f"{len(rows)} results from ollama.com" if rows else
                              "No results (site layout may have changed) - type an exact tag and press Pull.")
        self._fill_lib(rows)

    def _fill_lib(self, rows: list[dict]) -> None:
        t = self.lib
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for i, m in enumerate(rows):
            t.setItem(i, 0, _item(m["name"]))
            t.setItem(i, 1, _item(m["desc"]))
            t.setItem(i, 2, _item(", ".join(m["sizes"])))
            t.setItem(i, 3, _item(m["pulls"]))
            _set_bold_row(t, i, fits_est(m["sizes"], self.ctx.vram_gb()))
        t.setSortingEnabled(True)

    def _pull(self, name: str) -> None:
        self.ctx.downloads.set_ollama(self.ctx.ollama())
        self.ctx.downloads.add_ollama(name)
        self.ctx.log(f"Queued: ollama pull {name}")
        self.ctx.goto("downloads")

    def pull_typed(self) -> None:
        if self.q.text().strip():
            self._pull(self.q.text().strip())

    def pull_selected(self) -> None:
        rows = self.lib.selectionModel().selectedRows()
        if rows:
            self._pull(self.lib.item(rows[0].row(), 0).text())


# =============================================================== downloads
class DownloadsTab(QWidget):
    def __init__(self, ctx: Ctx):
        super().__init__()
        self.ctx = ctx
        self.row_of: dict[int, int] = {}
        lay = QVBoxLayout(self)
        self.table = make_table(["Download", "State", "Progress", "Speed", "Size", "Target / status"], 0)
        self.table.setSortingEnabled(False)
        lay.addWidget(self.table, 1)
        r = QHBoxLayout()
        r.addWidget(button("Refresh", "", self.refresh,
                            "Re-read the Downloads folder from disk - picks up files that finished, or were added "
                            "there some other way (browser, manually copied, from a previous launcher run)."))
        r.addWidget(button("Cancel", "Danger", self.cancel))
        r.addWidget(button("Retry / resume", "", self.retry))
        r.addWidget(button("Clear finished", "", self.clear))
        r.addWidget(button("Open target folder", "", self.open_target,
                            "Selected download's folder, or the configured Downloads folder if none is selected."))
        r.addStretch(1)
        self.info = QLabel("")
        self.info.setObjectName("Muted")
        r.addWidget(self.info)
        lay.addLayout(r)
        ctx.downloads.changed.connect(self.update_job)
        ctx.downloads.finished.connect(self._finished)
        self.refresh()          # show what's already on disk immediately, not only after a manual click

    def _finished(self, job) -> None:
        self.ctx.log(f"Download {job.state}: {job.label}" + (f" - {job.error}" if job.error else ""))
        if job.kind == "ollama" and job.state == "done":
            self.ctx.refresh_services()

    def update_job(self, job) -> None:
        t = self.table
        row = self.row_of.get(job.id)
        if row is None or row >= t.rowCount() or t.item(row, 0) is None or t.item(row, 0).data(Qt.UserRole) != job.id:
            row = t.rowCount()
            t.insertRow(row)
            self.row_of[job.id] = row
            it = QTableWidgetItem(job.label)
            it.setData(Qt.UserRole, job.id)
            t.setItem(row, 0, it)
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setTextVisible(True)
            t.setCellWidget(row, 2, bar)
        pct = int(1000 * job.done / job.size) if job.size else 0
        bar = t.cellWidget(row, 2)
        bar.setValue(pct)
        bar.setFormat(f"{pct / 10:.1f} %" if job.size else "...")
        colour = {"done": OK, "error": BAD, "cancelled": MUTED}.get(job.state, "#172033")
        st = QTableWidgetItem(job.state)
        from PySide6.QtGui import QColor
        st.setForeground(QColor(colour))
        t.setItem(row, 1, st)
        t.setItem(row, 3, QTableWidgetItem(f"{job.speed / 1024 ** 2:.1f} MB/s" if job.state == "running" and job.speed else ""))
        t.setItem(row, 4, QTableWidgetItem(f"{fmt_size(job.done)} / {fmt_size(job.size)}" if job.size else fmt_size(job.done)))
        t.setItem(row, 5, QTableWidgetItem(job.error or job.status or job.dest))
        self.info.setText(f"{self.ctx.downloads.active()} active")

    def _job(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        jid = self.table.item(rows[0].row(), 0).data(Qt.UserRole)
        return next((j for j in self.ctx.downloads.jobs if j.id == jid), None)

    def cancel(self) -> None:
        j = self._job()
        if j:
            self.ctx.downloads.cancel(j)

    def retry(self) -> None:
        j = self._job()
        if j:
            self.ctx.downloads.retry(j)

    def open_target(self) -> None:
        j = self._job()
        path = Path(j.dest).parent if j and j.dest else self.ctx.cfg.path("downloads")
        open_folder(path)

    def refresh(self) -> None:
        """Re-scan the configured Downloads folder for files not already tracked (see
        DownloadManager.scan_existing) - this is what makes files show up that were downloaded
        outside this launcher, or from a previous run."""
        self.ctx.downloads.scan_existing(self.ctx.cfg.path("downloads"))

    def clear(self) -> None:
        self.ctx.downloads.clear_finished()
        self.table.setRowCount(0)
        self.row_of.clear()
        for j in self.ctx.downloads.jobs:
            self.update_job(j)


# =============================================================== page
VRAM_TIERS = (8, 12, 16, 24, 32)


def _detect_vram_tier() -> int | None:
    """VRAM tier (GB) of the active dedicated GPU, or None."""
    gs = [g for g in gpu_info.list_gpus() if g.get("vram_mb") and g.get("vendor") in ("NVIDIA", "AMD")]
    if not gs:
        return None
    g = next((x for x in gs if x.get("active")), None) or max(gs, key=lambda x: x["vram_mb"])
    gb = g["vram_mb"] / 1024
    return min(VRAM_TIERS, key=lambda t: abs(t - gb))


class ModelsPage(QWidget):
    def __init__(self, ctx: Ctx):
        super().__init__()
        self.setObjectName("Page")
        self.ctx = ctx
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 10)
        head = QHBoxLayout()
        head.addWidget(page_header("AI models", "Local library, model search (Hugging Face, Civitai, Ollama) and downloads"), 1)
        vl = QLabel("GPU VRAM")
        vl.setToolTip("Models that fit into this much video memory are shown in bold")
        head.addWidget(vl)
        self._detected: int | None = None                # VRAM tier of the active GPU (green)
        self.vram_group = QButtonGroup(self)
        self.vram_group.setExclusive(True)
        self.vram_btns: dict[int, QPushButton] = {}
        cur = int(round(ctx.vram_gb()))
        for gb in VRAM_TIERS:
            b = QPushButton(f"{gb} GB")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(f"Bold = model file fits into {gb} GB VRAM (weights <= 80 %, room left for context)")
            b.clicked.connect(lambda _c=False, g=gb: self._pick_vram(g))
            self.vram_group.addButton(b)
            self.vram_btns[gb] = b
            head.addWidget(b)
        self.vram_btns[min(VRAM_TIERS, key=lambda g: abs(g - cur))].setChecked(True)
        self._style_vram()
        lay.addLayout(head)
        run_async(_detect_vram_tier, on_result=self._on_detected)
        self.tabs = QTabWidget()
        self.local = LocalTab(ctx)
        self.mgr = ManagerTab(ctx)
        self.hf = HFTab(ctx)
        self.civ = CivitaiTab(ctx)
        self.oll = OllamaTab(ctx)
        self.dl = DownloadsTab(ctx)
        for w, n in ((self.local, "Local library"), (self.mgr, "Manager"), (self.hf, "Hugging Face"),
                     (self.civ, "Civitai"), (self.oll, "Ollama"), (self.dl, "Downloads")):
            self.tabs.addTab(w, n)
        self.tabs.currentChanged.connect(self._tab)
        for tab in (self.hf, self.civ):
            _set_vram_header(tab.ftable, 2, self.ctx.vram_gb())
        lay.addWidget(self.tabs, 1)
        ctx.downloads.changed.connect(lambda _j: self.tabs.setTabText(self.tabs.indexOf(self.dl), f"Downloads ({ctx.downloads.active()})"
                                                                     if ctx.downloads.active() else "Downloads"))
        self._scanned = False

    def _style_vram(self) -> None:
        """Selected = accent filled; detected (active) GPU = green."""
        for gb, b in self.vram_btns.items():
            det = gb == self._detected
            col = OK if det else ACCENT
            b.setStyleSheet(
                f"QPushButton {{ padding: 4px 10px; border: 1px solid {col if det else '#C9D4E2'}; border-radius: 6px;"
                f" background: #FFFFFF; color: {OK if det else '#172033'}; font-weight: {700 if det else 400}; }}"
                f"QPushButton:checked {{ background: {col}; color: #FFFFFF; border-color: {col}; font-weight: 700; }}")

    def _on_detected(self, gb) -> None:
        if not gb:
            return
        self._detected = int(gb)
        self.vram_btns[self._detected].setToolTip(f"Active GPU: {self._detected} GB VRAM detected")
        if not self.ctx.cfg.data.get("vram_user_set"):        # default = active GPU until the user picks another size
            self.vram_btns[self._detected].setChecked(True)
            self._vram_changed(self._detected)
        self._style_vram()

    def _pick_vram(self, gb: int) -> None:
        self.ctx.cfg.data["vram_user_set"] = True
        self._vram_changed(gb)

    def _vram_changed(self, v: int) -> None:
        self.ctx.cfg.data["vram_gb"] = v
        try:
            self.ctx.cfg.save()
        except OSError as exc:
            self.ctx.log(f"Could not save config: {exc}")
        self.local.apply_filter()
        self.mgr.refresh_view()
        for tab in (self.hf, self.civ):
            _set_vram_header(tab.ftable, 2, float(v))    # header always follows the selector
            if not getattr(tab, "files", True):          # HF tab without a selected repo
                continue
            try:
                tab.fill_files()
            except Exception:        # noqa: BLE001 - nothing selected yet
                pass
        self.oll.rebold()

    def show_downloads(self) -> None:
        self.tabs.setCurrentWidget(self.dl)

    def on_show(self) -> None:
        if not self._scanned:
            self._scanned = True
            self.local.scan()

    def _tab(self, i: int) -> None:
        if self.tabs.widget(i) is self.oll:
            self.oll.refresh()
        elif self.tabs.widget(i) is self.mgr:
            self.mgr.on_show()
        elif self.tabs.widget(i) is self.dl:
            self.dl.refresh()
