"""SQLite index of model files (old stock, downloads, portable library) - data base of the two-pane Model Manager.

Only metadata lives here (path, size, hash, area, Ollama marker). The files themselves are never touched by this module.
Small data set (hundreds to a few thousand rows), so all calls are cheap and safe to make from the GUI thread.
"""
from __future__ import annotations

import csv
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .config import Config
from .models import MODEL_EXT, LocalModel, base_area

DB_FILE = Path(__file__).resolve().parent.parent / "models.db"
ROOT_ALT, ROOT_DOWNLOADS, ROOT_PORTABLE = "alt", "downloads", "portable"
ROOTS = (ROOT_ALT, ROOT_DOWNLOADS, ROOT_PORTABLE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    root        TEXT    NOT NULL,            -- alt | downloads | portable | user:<tokenized folder>
    base        TEXT    NOT NULL,            -- folder relpath is relative to ({ROOT}-token for portable/downloads, absolute for alt)
    relpath     TEXT    NOT NULL,            -- forward slashes, first folder = category
    name        TEXT    NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL    NOT NULL,
    hash        TEXT,                        -- SHA256 (hex, upper case) or NULL = not computed yet
    category    TEXT    NOT NULL,            -- taken from the folder
    area        TEXT,                        -- Einsatzbereich chosen by the user, NULL = automatic suggestion
    ollama      INTEGER NOT NULL DEFAULT 0,  -- 1 = model is used by Ollama
    ollama_name TEXT,                        -- name inside Ollama (e.g. qwen3-8b-q8)
    present     INTEGER NOT NULL DEFAULT 1,  -- 0 = file not found on disk at the last check
    UNIQUE(root, base, relpath)
);
CREATE INDEX IF NOT EXISTS ix_files_root ON files(root);
CREATE INDEX IF NOT EXISTS ix_files_hash ON files(hash);

-- audit trail: every action that changes a location or the Ollama state (rows stay when the file is deleted)
CREATE TABLE IF NOT EXISTS history (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT    NOT NULL,           -- local time, YYYY-MM-DD HH:MM:SS
    action   TEXT    NOT NULL,           -- import | scan_new | scan_gone | copy | move | delete | ollama_create | ollama_rm | area | marker
    file_id  INTEGER,                    -- files.id (NULL/dangling after a delete)
    name     TEXT,
    size     INTEGER,
    hash     TEXT,
    from_loc TEXT,
    to_loc   TEXT,
    status   TEXT,                       -- ok | skipped | error
    note     TEXT
);
CREATE INDEX IF NOT EXISTS ix_history_file ON history(file_id);
"""


def _cat(relpath: str) -> str:
    parts = relpath.split("/")
    return parts[0] if len(parts) > 1 else "(root)"


def _is_ollama_area(area: str | None) -> bool:
    return bool(area) and area.lower().startswith("ollama")


class ModelDB:
    def __init__(self, path: Path = DB_FILE):
        self.path = path
        self._lock = threading.RLock()
        self.con = sqlite3.connect(str(path), check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        with self._lock:
            self.con.executescript(SCHEMA)
            self.con.commit()
            self._migrate()

    def _migrate(self) -> None:
        """Schema / data upgrades of older index files. Idempotent, runs on every start."""
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(files)")}
        if "status" not in cols:            # test gate: new | tested | approved (NULL = derived from the root)
            self.con.execute("ALTER TABLE files ADD COLUMN status TEXT")
        # areas used to mix use case, runtime and quant ('LLM - QT5', 'Ollama - Tool-Calling') - keep the use case only
        rows = self.con.execute("SELECT id, name, area, ollama FROM files WHERE area IS NOT NULL").fetchall()
        for r in rows:
            new_area = base_area(r["area"])
            if new_area == r["area"]:
                continue
            ollama = 1 if (r["area"] or "").lower().startswith("ollama") else r["ollama"]
            self.con.execute("UPDATE files SET area=?, ollama=? WHERE id=?", (new_area, ollama, r["id"]))
            self.con.execute("INSERT INTO history(ts, action, file_id, name, from_loc, to_loc, status, note) "
                             "VALUES (?,?,?,?,?,?,?,?)",
                             (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "area", r["id"], r["name"], "", "", "ok",
                              f"migrated {r['area']} -> {new_area}"))
        self.con.commit()

    def close(self) -> None:
        with self._lock:
            self.con.close()

    # ---------------------------------------------------------------- reading
    def rows(self, root: str) -> list[dict]:
        with self._lock:
            cur = self.con.execute("SELECT * FROM files WHERE root=? ORDER BY name COLLATE NOCASE", (root,))
            return [dict(r) for r in cur.fetchall()]

    def get(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        with self._lock:
            q = ",".join("?" * len(ids))
            return [dict(r) for r in self.con.execute(f"SELECT * FROM files WHERE id IN ({q})", ids)]

    def count(self, root: str) -> int:
        with self._lock:
            return self.con.execute("SELECT COUNT(*) FROM files WHERE root=?", (root,)).fetchone()[0]

    # ---------------------------------------------------------------- audit trail
    def log(self, action: str, file_id: int | None = None, name: str = "", size: int | None = None, hash_: str | None = None,
            from_loc: str = "", to_loc: str = "", status: str = "ok", note: str = "") -> None:
        with self._lock:
            self.con.execute("INSERT INTO history(ts, action, file_id, name, size, hash, from_loc, to_loc, status, note) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, file_id, name, size, hash_,
                              from_loc, to_loc, status, note))
            self.con.commit()

    def history(self, limit: int = 2000, file_id: int | None = None) -> list[dict]:
        with self._lock:
            if file_id is None:
                cur = self.con.execute("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,))
            else:
                cur = self.con.execute("SELECT * FROM history WHERE file_id=? ORDER BY id DESC LIMIT ?", (file_id, limit))
            return [dict(r) for r in cur.fetchall()]

    def match_keys(self, root: str) -> tuple[set[str], set[tuple[str, int]]]:
        """(hashes, (lower-case name, size) pairs) of a root - used to flag files that exist on the other side."""
        hashes: set[str] = set()
        pairs: set[tuple[str, int]] = set()
        for r in self.rows(root):
            if r["hash"]:
                hashes.add(r["hash"])
            pairs.add((r["name"].lower(), r["size"]))
        return hashes, pairs

    # ---------------------------------------------------------------- writing
    def _find(self, root: str, base: str, relpath: str) -> dict | None:
        r = self.con.execute("SELECT * FROM files WHERE root=? AND base=? AND relpath=?", (root, base, relpath)).fetchone()
        return dict(r) if r else None

    def upsert(self, root: str, base: str, relpath: str, size: int, mtime: float, hash_: str | None = None,
               area: str | None = None, ollama: int = 0, ollama_name: str | None = None, status: str | None = None) -> int:
        """Insert or refresh a file. User data (area, Ollama marker) of an existing row is kept."""
        relpath = relpath.replace("\\", "/")
        name = relpath.rsplit("/", 1)[-1]
        with self._lock:
            old = self._find(root, base, relpath)
            if old:
                changed = old["size"] != size or abs(old["mtime"] - mtime) > 2
                new_hash = hash_ or (None if changed else old["hash"])
                self.con.execute("UPDATE files SET size=?, mtime=?, hash=?, present=1 WHERE id=?",
                                 (size, mtime, new_hash, old["id"]))
                self.con.commit()
                return old["id"]
            cur = self.con.execute(
                "INSERT INTO files(root, base, relpath, name, size, mtime, hash, category, area, ollama, ollama_name, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (root, base, relpath, name, size, mtime, hash_, _cat(relpath), area, ollama, ollama_name, status))
            self.con.commit()
            return int(cur.lastrowid)

    def sync_scan(self, root: str, base: str, models: list[LocalModel], scan_root: str,
                  legacy_areas: dict[str, str], tokenize) -> tuple[int, int]:
        """Bring the rows of (root, base) in line with a fresh disk scan. Returns (added, removed).

        legacy_areas = the old config "model_areas" (tokenized full path -> area); used once for new rows,
        an area starting with "Ollama" also sets the Ollama marker."""
        seen: set[str] = set()
        added = 0
        with self._lock:
            for m in models:
                rel = os.path.relpath(m.path, scan_root).replace("\\", "/")
                seen.add(rel)
                if self._find(root, base, rel) is None:
                    added += 1
                    raw = legacy_areas.get(tokenize(m.path))
                    fid = self.upsert(root, base, rel, m.size, m.mtime, None, base_area(raw), 1 if _is_ollama_area(raw) else 0,
                                      None, "new" if root == ROOT_DOWNLOADS else None)
                    self.log("scan_new", fid, m.name, m.size, None, "", f"{root}:{rel}", "ok", "found on disk")
                else:
                    self.upsert(root, base, rel, m.size, m.mtime)
            gone = [dict(r) for r in self.con.execute("SELECT * FROM files WHERE root=? AND base=?", (root, base))
                    if r["relpath"] not in seen]
            for g in gone:
                self.log("scan_gone", g["id"], g["name"], g["size"], g["hash"], f"{root}:{g['relpath']}", "", "ok",
                         "no longer on disk (removed outside the manager)")
                self.con.execute("DELETE FROM files WHERE id=?", (g["id"],))
            self.con.commit()
        return added, len(gone)

    def import_csv(self, csv_path: Path) -> int:
        """Import the audit CSV (columns SourcePath, FileName, RelativePath, FullPath, SizeBytes, Hash, Modified) as old stock."""
        n = 0
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                rel = (r.get("RelativePath") or r.get("FileName") or "").replace("\\", "/")
                if not rel or Path(rel).suffix.lower() not in MODEL_EXT:
                    continue                                   # .lnk, .bat, put_*_here, ... are not models
                try:
                    size = int(r.get("SizeBytes") or 0)
                except ValueError:
                    continue
                try:
                    mtime = datetime.strptime(r.get("Modified", ""), "%d.%m.%Y %H:%M:%S").timestamp()
                except ValueError:
                    mtime = 0.0
                base = os.path.normpath(r.get("SourcePath") or "")
                self.upsert(ROOT_ALT, base, rel, size, mtime, (r.get("Hash") or "").upper() or None)
                n += 1
        self.log("import", None, csv_path.name, None, None, str(csv_path), ROOT_ALT, "ok", f"{n} model files imported as old stock")
        return n

    def set_area(self, ids: list[int], area: str | None) -> None:
        with self._lock:
            self.con.executemany("UPDATE files SET area=? WHERE id=?", [(area, i) for i in ids])
            self.con.commit()

    def set_status(self, ids: list[int], status: str | None) -> None:
        with self._lock:
            self.con.executemany("UPDATE files SET status=? WHERE id=?", [(status, i) for i in ids])
            self.con.commit()

    def area_by_path(self, roots: tuple[str, ...], expand) -> dict[str, dict]:
        """normcase(full path) -> row of the given roots. `expand` turns a tokenized base into a real folder."""
        out: dict[str, dict] = {}
        with self._lock:
            q = ",".join("?" * len(roots))
            for r in self.con.execute(f"SELECT * FROM files WHERE root IN ({q})", roots):
                full = os.path.join(expand(r["base"]), *r["relpath"].split("/"))
                out[os.path.normcase(os.path.normpath(full))] = dict(r)
        return out

    def set_ollama(self, ids: list[int], flag: bool, name: str | None = None) -> None:
        with self._lock:
            self.con.executemany("UPDATE files SET ollama=?, ollama_name=? WHERE id=?",
                                 [(1 if flag else 0, name if flag else None, i) for i in ids])
            self.con.commit()

    def set_hash(self, id_: int, hash_: str) -> None:
        with self._lock:
            self.con.execute("UPDATE files SET hash=? WHERE id=?", (hash_.upper(), id_))
            self.con.commit()

    def set_present(self, present: dict[int, bool]) -> None:
        with self._lock:
            self.con.executemany("UPDATE files SET present=? WHERE id=?", [(1 if v else 0, i) for i, v in present.items()])
            self.con.commit()

    def drop_root(self, root: str) -> None:
        """Forget all index rows of one source (e.g. a removed user folder). Files are not touched; history stays."""
        with self._lock:
            self.con.execute("DELETE FROM files WHERE root=?", (root,))
            self.con.commit()

    def delete(self, ids: list[int]) -> None:
        with self._lock:
            self.con.executemany("DELETE FROM files WHERE id=?", [(i,) for i in ids])
            self.con.commit()

    def move_row(self, id_: int, root: str, base: str, relpath: str, hash_: str | None = None) -> None:
        """F6: the same file now lives at another place - keeps area and Ollama marker."""
        relpath = relpath.replace("\\", "/")
        with self._lock:
            self.con.execute("UPDATE files SET root=?, base=?, relpath=?, name=?, category=?, present=1, "
                             "hash=COALESCE(?, hash) WHERE id=?",
                             (root, base, relpath, relpath.rsplit("/", 1)[-1], _cat(relpath), hash_, id_))
            self.con.commit()

    def copy_row(self, src: dict, root: str, base: str, relpath: str, hash_: str | None = None) -> int:
        """F5: new row for the copy; area and Ollama marker are carried over."""
        return self.upsert(root, base, relpath, src["size"], src["mtime"], hash_ or src["hash"],
                           src["area"], src["ollama"], src["ollama_name"], src.get("status"))
