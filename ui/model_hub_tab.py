"""Model Hub (test tab): ONE view of every LLM model and where it is used - library file, Ollama store, llama.cpp.

The wiring it makes visible and controllable:
    library GGUF (AI_Modells / X_DOWNLOADS / user folders, indexed in models.db)
        -> "Use in Ollama"  = hard link as Ollama blob + ollama create   (no second copy)
        -> "Use in llama.cpp" = llama-server reads the file directly     (no import at all)
    Ollama pull (from the Ollama library)
        -> "Make available to llama.cpp" = hard link the pulled blob into AI_Modells\\gguf\\<name>.gguf
    existing duplicates -> "Deduplicate" = replace Ollama's copy by a hard link
Everything is reversible: removing a model from Ollama only drops a link, the library file stays.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar,
                               QTableWidget, QVBoxLayout, QWidget)

from core import ollama_ops
from core.modeldb import ROOT_DOWNLOADS, ROOT_PORTABLE, ModelDB
from core.procs import port_open
from core.system import fmt_size, vram_verdict
from .theme import ACCENT, BAD, MUTED, OK, WARN
from .widgets import NumItem, button, card, run_async, section

if TYPE_CHECKING:
    from .context import Ctx

COLS = ["Model", "Size", "Fits VRAM", "Library file", "Ollama", "llama.cpp", "Default"]
STATE_COLOUR = {"linked": OK, "copy": WARN, "pulled": ACCENT, "-": MUTED}


def _it(text: str, colour: str | None = None, tip: str = ""):
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QTableWidgetItem
    it = QTableWidgetItem(text)
    if colour:
        it.setForeground(QColor(colour))
    if tip:
        it.setToolTip(tip)
    return it


class ModelHubTab(QWidget):
    def __init__(self, ctx: "Ctx"):
        super().__init__()
        self.ctx = ctx
        self.db = ModelDB()
        self.rows: list[dict] = []
        self.before_show = None                    # set by the main window (Manager scan fills models.db)
        lay = QVBoxLayout(self)
        from .widgets import page_header
        lay.addWidget(page_header("HUB Modell", "One model path for Ollama and llama.cpp - library, pull and Ollama blobs linked without duplicates"))

        # ---- wiring / paths
        box, bl = card(10)
        bl.addWidget(section("Wiring (one model path, shared by Ollama and llama.cpp)"))
        self.paths_lbl = QLabel("")
        self.paths_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.paths_lbl.setWordWrap(True)
        bl.addWidget(self.paths_lbl)
        row = QHBoxLayout()
        self.link_lbl = QLabel("Hard-link check: not run")
        row.addWidget(self.link_lbl, 1)
        row.addWidget(button("Check hard links", "", self.check_links, "Tests whether library and Ollama store can share files"))
        row.addWidget(button("Refresh", "", self.refresh))
        bl.addLayout(row)
        self.storage_lbl = QLabel("")
        bl.addWidget(self.storage_lbl)
        lay.addWidget(box)

        # ---- unified model table
        t = self.table = QTableWidget(0, len(COLS))
        t.setHorizontalHeaderLabels(COLS)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.ExtendedSelection)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.verticalHeader().setVisible(False)
        t.setAlternatingRowColors(True)
        t.setSortingEnabled(True)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c, w in enumerate((0, 80, 80, 320, 170, 90, 70)):
            if c:
                t.setColumnWidth(c, w)
        lay.addWidget(t, 1)

        # ---- actions
        a = QHBoxLayout()
        a.addWidget(button("Use in Ollama", "Go", self.use_ollama, "Hard link the file into Ollama's store + ollama create (no copy)"))
        a.addWidget(button("Use in llama.cpp", "Go", self.use_llamacpp, "Set as llama.cpp server model (reads the file directly)"))
        a.addWidget(button("Set as Ollama default", "", self.set_default, "Preloaded when Ollama starts (dashboard picker)"))
        a.addWidget(button("Make available to llama.cpp", "", self.export_pulled, "Pulled Ollama model -> hard link as .gguf in the library"))
        a.addWidget(button("Deduplicate", "", self.dedupe, "Replace Ollama's separate copy by a hard link"))
        a.addWidget(button("Remove from Ollama", "Danger", self.remove, "ollama rm - the library file stays"))
        a.addStretch(1)
        lay.addLayout(a)

        # ---- pull
        p = QHBoxLayout()
        self.pull_edit = QLineEdit()
        self.pull_edit.setPlaceholderText("Ollama tag to pull, e.g. qwen3:14b - afterwards 'Make available to llama.cpp' links it into the library")
        p.addWidget(self.pull_edit, 1)
        p.addWidget(button("Pull", "Primary", self.pull))
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        p.addWidget(self.bar, 1)
        lay.addLayout(p)
        self.status = QLabel("")
        self.status.setObjectName("Muted")
        lay.addWidget(self.status)
        self._loaded = False

    # ------------------------------------------------------------------ data
    def on_show(self) -> None:
        if callable(self.before_show):
            self.before_show()
        if not self._loaded:
            self._loaded = True
            self.refresh()

    def say(self, text: str) -> None:
        self.status.setText(text)
        self.ctx.log(f"[HUB Modell] {text}")

    def _paths(self) -> dict:
        c = self.ctx.cfg
        return {"library": c.path("models"), "gguf": c.path("gguf"), "downloads": c.path("downloads"),
                "store": c.path("ollama_models"), "llamacpp": c.path("llamacpp")}

    def refresh(self) -> None:
        cfg = self.ctx.cfg
        p = self._paths()
        oll_up = port_open(cfg.port("ollama"))
        lcp_up = port_open(cfg.port("llamacpp"))
        self.paths_lbl.setText(
            f"Library: {p['library']}   (GGUF: {p['gguf']})\nDownloads: {p['downloads']}\n"
            f"Ollama store (OLLAMA_MODELS): {p['store']}   - server {'running' if oll_up else 'stopped'} :{cfg.port('ollama')}\n"
            f"llama.cpp: {p['llamacpp']}   - server {'running' if lcp_up else 'stopped'} :{cfg.port('llamacpp')}   "
            f"model: {cfg.data.get('llamacpp', {}).get('model', '')}")
        files = []
        for root in (ROOT_PORTABLE, ROOT_DOWNLOADS):
            for r in self.db.rows(root):
                if r["name"].lower().endswith(".gguf") and "mmproj" not in r["name"].lower() and r["present"]:
                    base = cfg.expand(r["base"])
                    r["path"] = os.path.normpath(os.path.join(base, *r["relpath"].split("/")))
                    files.append(r)
        self.say("Reading Ollama store ...")

        def work():
            mf = ollama_ops.manifests(cfg)
            st = ollama_ops.storage(cfg, [{"path": f["path"], "size": f["size"], "hash": f["hash"]} for f in files])
            sizes = {}
            for n, ds in mf.items():
                for d in ds:
                    bp = ollama_ops.blob_path(cfg, d)
                    sizes[d] = bp.stat().st_size if bp.is_file() else 0
            return mf, st, sizes

        run_async(work, on_result=lambda res: self._fill(files, *res), on_error=lambda e: self.say(f"refresh failed: {e}"))

    def _fill(self, files: list[dict], mf: dict, st: dict, sizes: dict) -> None:
        cfg = self.ctx.cfg
        self.storage_lbl.setText(f"Storage:  Library {fmt_size(st['library'])}   |   Ollama store {fmt_size(st['ollama'])}   |   "
                                 f"<b>Duplicate {fmt_size(st['duplicate'])}</b>   |   Shared (hard links) {fmt_size(st['shared'])}")
        digest_names: dict[str, list[str]] = {}
        for n, ds in mf.items():
            for d in ds:
                digest_names.setdefault(d, []).append(n)
        models_dir = cfg.path("models")
        lcp_model = str(cfg.data.get("llamacpp", {}).get("model", "")).replace("\\", "/").lower()
        default = ollama_ops.normalize(str(cfg.data.get("ollama", {}).get("model", "") or "x"))
        rows: list[dict] = []
        used_digests = set()
        for f in files:
            h = (f["hash"] or "").lower()
            names = digest_names.get(h, []) if h else []
            if not names and f["ollama_name"] and ollama_ops.normalize(f["ollama_name"]) in mf:
                names = [ollama_ops.normalize(f["ollama_name"])]
            state = "-"
            if names:
                used_digests.add(h)
                state = "linked" if h and ollama_ops.is_linked(ollama_ops.blob_path(cfg, h), f["path"]) else "copy"
            try:
                rel = os.path.relpath(f["path"], models_dir).replace("\\", "/").lower()
            except ValueError:
                rel = ""
            rows.append({"kind": "file", "name": names[0] if names else f["name"], "size": f["size"], "path": f["path"],
                         "hash": h, "id": f["id"], "ollama": names, "state": state,
                         "lcp": rel == lcp_model or os.path.basename(lcp_model) == f["name"].lower(), "file": f})
        for n, ds in mf.items():                      # pulled models with no library file
            d = ds[0] if ds else ""
            if d and d not in used_digests and not any(n in r["ollama"] for r in rows):
                rows.append({"kind": "pulled", "name": n, "size": sizes.get(d, 0), "path": "", "hash": d, "id": None,
                             "ollama": [n], "state": "pulled", "lcp": False, "file": None})
        self.rows = rows
        vram = self.ctx.vram_gb()
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            nm = _it(r["name"], None, r["path"] or "only in Ollama store")
            nm.setData(Qt.UserRole, i)
            t.setItem(i, 0, nm)
            t.setItem(i, 1, NumItem(fmt_size(r["size"]), r["size"]))
            fits = vram_verdict(r["size"], vram)[0]
            t.setItem(i, 2, _it("yes" if fits == "fits" else "partly", OK if fits == "fits" else WARN))
            t.setItem(i, 3, _it(r["path"] or "(none - pulled)", None if r["path"] else MUTED, r["path"]))
            txt = {"linked": "linked (0 extra)", "copy": f"COPY (+{fmt_size(r['size'])})", "pulled": "pulled (store only)", "-": "-"}[r["state"]]
            t.setItem(i, 4, _it(txt, STATE_COLOUR[r["state"]], ", ".join(r["ollama"])))
            t.setItem(i, 5, _it("active" if r["lcp"] else "", OK))
            isdef = any(ollama_ops.normalize(n) == default for n in r["ollama"])
            t.setItem(i, 6, _it("default" if isdef else "", ACCENT))
        t.setSortingEnabled(True)
        self.say(f"{len(rows)} model(s); {sum(1 for r in rows if r['state'] == 'copy')} duplicate(s)")

    def _sel(self) -> list[dict]:
        idx = {self.table.item(ix.row(), 0).data(Qt.UserRole) for ix in self.table.selectionModel().selectedRows()}
        return [self.rows[i] for i in sorted(idx)]

    def _one(self, kind: str | None = None) -> dict | None:
        s = self._sel()
        if len(s) != 1 or (kind and s[0]["kind"] != kind):
            self.say("Select exactly one " + ("library file" if kind == "file" else "pulled model" if kind else "row") + ".")
            return None
        return s[0]

    # ------------------------------------------------------------------ actions
    def check_links(self) -> None:
        cfg = self.ctx.cfg
        run_async(ollama_ops.link_capability, cfg, cfg.path("models"),
                  on_result=lambda res: self.link_lbl.setText(("OK: " if res[0] else "WARNING: ") + res[1]),
                  on_error=lambda e: self.link_lbl.setText(f"check failed: {e}"))

    def use_ollama(self) -> None:
        r = self._one("file")
        if not r:
            return
        if r["ollama"]:
            self.say(f"already in Ollama as {', '.join(r['ollama'])}")
            return
        if not port_open(self.ctx.cfg.port("ollama")):
            QMessageBox.information(self, "Ollama", "Start the Ollama server first (Dashboard).")
            return
        name = ollama_ops.sanitize_name(Path(r["path"]).stem)
        cfg = self.ctx.cfg
        self.say(f"hashing + linking {r['file']['name']} as '{name}' ...")

        def done(res) -> None:
            sha, state = res
            self.db.set_hash(r["id"], sha)
            self.db.set_ollama([r["id"]], True, name)
            self.say(f"Ollama model '{name}' created - blob {state}")
            self.refresh()

        run_async(ollama_ops.create_linked, cfg, name, r["path"], r["hash"] or None,
                  on_result=done, on_error=lambda e: self.say(f"Use in Ollama failed: {e}"))

    def use_llamacpp(self) -> None:
        r = self._one("file")
        if not r:
            return
        cfg = self.ctx.cfg
        try:
            val = os.path.relpath(r["path"], cfg.path("models")).replace("\\", "/")
        except ValueError:
            val = r["path"]
        cfg.data.setdefault("llamacpp", {})["model"] = val
        cfg.save()
        self.say(f"llama.cpp model set: {val} (restart the llama.cpp server to load it)")
        self.ctx.refresh_services()
        self.refresh()

    def set_default(self) -> None:
        r = self._one()
        if not r:
            return
        cfg = self.ctx.cfg
        sec = cfg.data.setdefault("ollama", {})
        if r["ollama"]:
            sec["model"] = r["ollama"][0]
        elif r["kind"] == "file":
            sec["model"] = ollama_ops.normalize(ollama_ops.sanitize_name(Path(r["path"]).stem))
            sec["default_gguf"] = cfg.tokenize(r["path"])         # imported (hard link) on the next Ollama start
        cfg.save()
        self.say(f"Ollama default: {sec['model']}")
        self.ctx.refresh_services()
        self.refresh()

    def export_pulled(self) -> None:
        r = self._one("pulled")
        if not r:
            return
        cfg = self.ctx.cfg
        dest = cfg.path("gguf") / (ollama_ops.sanitize_name(r["name"].replace(":", "-").replace("/", "-")) + ".gguf")
        res = ollama_ops.export_blob(cfg, r["hash"], dest)
        if res.startswith("copy-needed"):
            QMessageBox.warning(self, "Not linked", f"{res}\n\nNo copy was made (would cost {fmt_size(r['size'])}).")
        else:
            self.say(f"{r['name']} -> {dest} ({res}); rescan the Manager tab to index it")
        self.refresh()

    def dedupe(self) -> None:
        rows = [r for r in self._sel() if r["state"] == "copy"]
        if not rows:
            self.say("Select rows with Ollama state COPY.")
            return
        cfg = self.ctx.cfg

        def work():
            out = []
            for r in rows:
                sha = r["hash"] or ollama_ops.sha256_of(r["path"])
                out.append((r, sha, *ollama_ops.dedupe_blob(cfg, r["path"], sha)))
            return out

        def done(res) -> None:
            for r, sha, ok, msg in res:
                self.db.set_hash(r["id"], sha)
                self.ctx.log(f"[HUB Modell] dedupe {r['name']}: {msg}")
            self.say(f"deduplicated {sum(1 for x in res if x[2])} of {len(res)}")
            self.refresh()

        run_async(work, on_result=done, on_error=lambda e: self.say(f"dedupe failed: {e}"))

    def remove(self) -> None:
        rows = [r for r in self._sel() if r["ollama"]]
        if not rows:
            self.say("Nothing selected that is in Ollama.")
            return
        names = [n for r in rows for n in r["ollama"]]
        pulled = [r["name"] for r in rows if r["kind"] == "pulled"]
        warn = ("\n\nPulled models without a library file are gone after this (use 'Make available to llama.cpp' first "
                "to keep them): " + ", ".join(pulled)) if pulled else ""
        if QMessageBox.question(self, "Remove from Ollama", "ollama rm " + ", ".join(names) +
                                "\n\nLibrary files stay untouched." + warn) != QMessageBox.Yes:
            return
        cfg = self.ctx.cfg

        def work():
            return [(n, (lambda: ollama_ops.remove_model(cfg, n))()) for n in names]

        def done(_res) -> None:
            for r in rows:
                if r["id"]:
                    self.db.set_ollama([r["id"]], False)
            self.say("removed: " + ", ".join(names))
            self.refresh()

        run_async(work, on_result=done, on_error=lambda e: self.say(f"ollama rm failed: {e}"))

    def pull(self) -> None:
        tag = self.pull_edit.text().strip()
        if not tag:
            return
        if not port_open(self.ctx.cfg.port("ollama")):
            QMessageBox.information(self, "Ollama", "Start the Ollama server first (Dashboard).")
            return
        cl = self.ctx.ollama()
        self.bar.setVisible(True)
        self.bar.setRange(0, 1000)
        prog = {"s": "", "c": 0, "t": 0}

        def cb(s, c, t):
            prog.update(s=s, c=c, t=t)

        from PySide6.QtCore import QTimer
        timer = QTimer(self)
        timer.timeout.connect(lambda: (self.bar.setValue(int(prog["c"] * 1000 / prog["t"]) if prog["t"] else 0),
                                       self.bar.setFormat(f"{prog['s']}  {fmt_size(prog['c'])}/{fmt_size(prog['t'])}")))
        timer.start(500)

        def finish(msg: str) -> None:
            timer.stop()
            self.bar.setVisible(False)
            self.say(msg)
            self.refresh()

        run_async(cl.pull, tag, cb, lambda: False,
                  on_result=lambda _r: finish(f"pulled {tag} - select it and use 'Make available to llama.cpp' to share it"),
                  on_error=lambda e: finish(f"pull failed: {e}"))
