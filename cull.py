#!/usr/bin/env python3
"""Photo Culler v0 - pick which photos from a shoot get printed.

Usage:
    python cull.py /path/to/photos

Originals are opened read-only and never modified, moved, renamed or re-encoded.
The manifest and the proxy cache live in an OS app-data directory, never in the
photo folder. Files are copied only at export time.

Dependencies: Python 3.9+ and Pillow. Nothing else.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import threading
import time
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageOps

APP_NAME = "photo-culler"
PROJECT_DIR = Path(__file__).resolve().parent
STATIC_DIR = PROJECT_DIR / "static"

DISPLAY_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2"}

DECISIONS = ("undecided", "keep", "maybe", "reject")

PROXY_LONG_EDGE = 1600
PROXY_QUALITY = 85
PREFETCH_AHEAD = 8

# Pillow moved the resampling constants; both spellings work across versions.
try:
    LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover - older Pillow
    LANCZOS = Image.LANCZOS


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------

def app_data_dir() -> Path:
    """OS-appropriate app-data root. Never inside the photo folder."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME


def library_dir(photo_root: Path, data_root: Path = None) -> Path:
    """Per-photo-folder library: manifest + proxy cache."""
    root = data_root or app_data_dir()
    digest = hashlib.sha256(str(photo_root).encode("utf-8")).hexdigest()[:12]
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in photo_root.name) or "photos"
    return root / ("%s-%s" % (safe[:40], digest))


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos(
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE,
  stem TEXT,
  raw_path TEXT,
  width INTEGER,
  height INTEGER,
  bytes INTEGER,
  mtime REAL,
  seq INTEGER,
  decision TEXT DEFAULT 'undecided',
  decided_at REAL,
  capture_ts REAL
);
CREATE INDEX IF NOT EXISTS photos_seq ON photos(seq);

CREATE TABLE IF NOT EXISTS unpaired_raws(
  path TEXT PRIMARY KEY,
  bytes INTEGER,
  mtime REAL
);

CREATE TABLE IF NOT EXISTS state(
  k TEXT PRIMARY KEY,
  v TEXT
);
"""


class Manifest:
    """SQLite manifest. One connection guarded by a lock - single user, low traffic."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # -- generic helpers ---------------------------------------------------

    def query(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def run(self, sql: str, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    # -- state -------------------------------------------------------------

    def get_state(self, key: str, default=None):
        row = self.one("SELECT v FROM state WHERE k=?", (key,))
        return row["v"] if row else default

    def set_state(self, key: str, value) -> None:
        self.run(
            "INSERT INTO state(k, v) VALUES(?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, str(value)),
        )

    # -- decisions ---------------------------------------------------------

    def set_decision(self, photo_id: int, decision: str) -> bool:
        if decision not in DECISIONS:
            return False
        decided_at = None if decision == "undecided" else time.time()
        cur = self.run(
            "UPDATE photos SET decision=?, decided_at=? WHERE id=?",
            (decision, decided_at, photo_id),
        )
        return cur.rowcount > 0

    def counts(self):
        rows = self.query("SELECT decision, COUNT(*) AS n FROM photos GROUP BY decision")
        out = {d: 0 for d in DECISIONS}
        for r in rows:
            out[r["decision"]] = r["n"]
        return out


# --------------------------------------------------------------------------
# Scan and pairing
# --------------------------------------------------------------------------

EXIF_DATETIME_TAGS = (36867, 36868, 306)  # DateTimeOriginal, DateTimeDigitized, DateTime
EXIF_ORIENTATION_TAG = 274
EXIF_IFD_POINTER = 0x8769


def _parse_exif_datetime(value) -> float:
    """'2024:06:15 14:03:21' -> POSIX timestamp, or None."""
    if not value:
        return None
    text = str(value).strip().rstrip("\x00")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except (ValueError, OverflowError, OSError):
            continue
    return None


def read_metadata(path: str):
    """Header-only read: (width, height, capture_ts). Originals opened read-only.

    Dimensions are reported post-orientation so the print-size figure is honest.
    """
    width = height = None
    capture_ts = None
    try:
        with open(path, "rb") as fh:
            with Image.open(fh) as im:
                width, height = im.size
                orientation = None
                try:
                    exif = im.getexif()
                except Exception:
                    exif = None
                if exif:
                    orientation = exif.get(EXIF_ORIENTATION_TAG)
                    merged = dict(exif)
                    try:
                        merged.update(dict(exif.get_ifd(EXIF_IFD_POINTER) or {}))
                    except Exception:
                        pass
                    for tag in EXIF_DATETIME_TAGS:
                        capture_ts = _parse_exif_datetime(merged.get(tag))
                        if capture_ts is not None:
                            break
                if orientation in (5, 6, 7, 8):
                    width, height = height, width
    except Exception:
        pass
    return width, height, capture_ts


def walk_folder(root: Path):
    """Return (displayables, raws) as {abs_path: os.stat_result}."""
    displayables = {}
    raws = {}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in DISPLAY_EXTS and ext not in RAW_EXTS:
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if not os.path.isfile(full):
                continue
            (displayables if ext in DISPLAY_EXTS else raws)[full] = st
    return displayables, raws


def pair_raws(displayables, raws):
    """A RAW attaches to a displayable when stems match in the same directory."""
    raw_index = {}
    for raw_path in raws:
        d, name = os.path.split(raw_path)
        stem = os.path.splitext(name)[0]
        raw_index.setdefault((d, stem.lower()), raw_path)

    pairs = {}
    used = set()
    for disp_path in displayables:
        d, name = os.path.split(disp_path)
        stem = os.path.splitext(name)[0]
        raw_path = raw_index.get((d, stem.lower()))
        if raw_path is not None:
            pairs[disp_path] = raw_path
            used.add(raw_path)
    unpaired = sorted(p for p in raws if p not in used)
    return pairs, unpaired


def scan(manifest: Manifest, root: Path, verbose: bool = True):
    """Scan the folder into the manifest. Existing decisions are never lost."""
    displayables, raws = walk_folder(root)
    pairs, unpaired = pair_raws(displayables, raws)

    existing = {r["path"]: r for r in manifest.query("SELECT * FROM photos")}

    # Only touch the filesystem for files that are new or changed on disk.
    needs_meta = []
    for path, st in displayables.items():
        row = existing.get(path)
        if row is None or row["width"] is None or abs((row["mtime"] or 0) - st.st_mtime) > 1e-6:
            needs_meta.append(path)

    meta = {}
    if needs_meta:
        workers = min(16, max(4, (os.cpu_count() or 4) * 2))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for path, result in zip(needs_meta, pool.map(read_metadata, needs_meta)):
                meta[path] = result

    with manifest.lock:
        conn = manifest.conn
        for path, st in displayables.items():
            row = existing.get(path)
            raw_path = pairs.get(path)
            if path in meta:
                width, height, capture_ts = meta[path]
            elif row is not None:
                width, height, capture_ts = row["width"], row["height"], row["capture_ts"]
            else:
                width = height = capture_ts = None

            if row is None:
                conn.execute(
                    "INSERT INTO photos(path, stem, raw_path, width, height, bytes, mtime,"
                    " seq, decision, decided_at, capture_ts)"
                    " VALUES(?,?,?,?,?,?,?,?,'undecided',NULL,?)",
                    (
                        path,
                        os.path.splitext(os.path.basename(path))[0],
                        raw_path,
                        width,
                        height,
                        st.st_size,
                        st.st_mtime,
                        0,
                        capture_ts,
                    ),
                )
            else:
                # Decision and decided_at are deliberately untouched.
                conn.execute(
                    "UPDATE photos SET raw_path=?, width=?, height=?, bytes=?, mtime=?,"
                    " capture_ts=? WHERE id=?",
                    (raw_path, width, height, st.st_size, st.st_mtime, capture_ts, row["id"]),
                )

        # Files that vanished from disk keep their rows but drop out of review order.
        gone = set(existing) - set(displayables)

        conn.execute("DELETE FROM unpaired_raws")
        conn.executemany(
            "INSERT OR REPLACE INTO unpaired_raws(path, bytes, mtime) VALUES(?,?,?)",
            [(p, raws[p].st_size, raws[p].st_mtime) for p in unpaired],
        )
        conn.commit()

        # Review order: EXIF capture time, else mtime, then filename.
        rows = conn.execute("SELECT id, path, mtime, capture_ts FROM photos").fetchall()
        live = [r for r in rows if r["path"] not in gone]
        live.sort(key=lambda r: ((r["capture_ts"] if r["capture_ts"] is not None
                                  else (r["mtime"] or 0.0)), r["path"]))
        conn.executemany(
            "UPDATE photos SET seq=? WHERE id=?",
            [(i, r["id"]) for i, r in enumerate(live)],
        )
        for r in rows:
            if r["path"] in gone:
                conn.execute("UPDATE photos SET seq=NULL WHERE id=?", (r["id"],))
        conn.commit()

    if verbose:
        print("  %d displayable photo(s)" % len(displayables))
        print("  %d with a RAW attached" % len(pairs))
        print("  %d unpaired RAW(s) recorded but not shown" % len(unpaired))
        if gone:
            print("  %d previously seen file(s) no longer on disk (rows kept)" % len(gone))
    return {
        "photos": len(displayables),
        "paired": len(pairs),
        "unpaired_raws": len(unpaired),
        "missing": len(gone),
    }


# --------------------------------------------------------------------------
# Proxy cache
# --------------------------------------------------------------------------

class ProxyCache:
    """Display-only JPEG proxies. No bearing on what gets exported."""

    def __init__(self, cache_dir: Path):
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._locks = {}
        self._locks_guard = threading.Lock()

    def key(self, path: str, mtime: float, size: int) -> str:
        raw = "%s|%r|%d" % (path, round(float(mtime or 0), 6), int(size or 0))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _key_lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = threading.Lock()
            return lock

    def get(self, path: str, mtime: float, size: int) -> Path:
        key = self.key(path, mtime, size)
        out = self.dir / (key + ".jpg")
        if out.exists():
            return out
        with self._key_lock(key):
            if out.exists():
                return out
            tmp = self.dir / (".%s.%d.tmp" % (key, threading.get_ident()))
            with open(path, "rb") as fh:  # originals: read-only, always
                with Image.open(fh) as im:
                    im = ImageOps.exif_transpose(im)
                    if im.mode not in ("RGB", "L"):
                        im = im.convert("RGB")
                    im.thumbnail((PROXY_LONG_EDGE, PROXY_LONG_EDGE), LANCZOS)
                    im.save(str(tmp), "JPEG", quality=PROXY_QUALITY)
            os.replace(str(tmp), str(out))
        return out


class Prefetcher:
    """Builds the next few proxies ahead of the reviewer in the background."""

    def __init__(self, manifest: Manifest, cache: ProxyCache, workers: int = 2):
        self.manifest = manifest
        self.cache = cache
        self.pending = deque()
        self.queued = set()
        self.cv = threading.Condition()
        self.stop = False
        self.threads = [
            threading.Thread(target=self._worker, name="prefetch-%d" % i, daemon=True)
            for i in range(workers)
        ]
        for t in self.threads:
            t.start()

    def request_from(self, seq: int, ahead: int = PREFETCH_AHEAD) -> None:
        rows = self.manifest.query(
            "SELECT id, path, mtime, bytes FROM photos WHERE seq >= ? AND seq IS NOT NULL"
            " ORDER BY seq LIMIT ?",
            (seq, ahead),
        )
        with self.cv:
            for r in rows:
                if r["id"] in self.queued:
                    continue
                self.queued.add(r["id"])
                self.pending.append((r["id"], r["path"], r["mtime"], r["bytes"]))
            self.cv.notify_all()

    def _worker(self) -> None:
        while True:
            with self.cv:
                while not self.pending and not self.stop:
                    self.cv.wait(0.5)
                if self.stop:
                    return
                item = self.pending.popleft()
            photo_id, path, mtime, size = item
            try:
                self.cache.get(path, mtime, size)
            except Exception:
                pass
            finally:
                with self.cv:
                    self.queued.discard(photo_id)

    def shutdown(self) -> None:
        with self.cv:
            self.stop = True
            self.cv.notify_all()


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def _unique_destination(dest_dir: Path, filename: str) -> Path:
    """Suffix on collision. Never overwrite, never delete."""
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem, ext = os.path.splitext(filename)
    n = 1
    while True:
        candidate = dest_dir / ("%s_%d%s" % (stem, n, ext))
        if not candidate.exists():
            return candidate
        n += 1


def export(manifest: Manifest, photo_root: Path, dest: Path):
    """Copy keeps and maybes to dest. Rejects are not exported. Nothing is deleted."""
    dest = Path(dest).expanduser()
    try:
        resolved_dest = dest.resolve()
    except OSError:
        resolved_dest = dest
    resolved_root = photo_root.resolve()
    if resolved_dest == resolved_root or resolved_root in resolved_dest.parents:
        raise ValueError("Destination must be outside the photo folder - nothing is "
                         "ever written into the source.")

    dest.mkdir(parents=True, exist_ok=True)
    dirs = {
        "keep": dest / "keep",
        "maybe": dest / "maybe",
        "keep_raw": dest / "keep" / "raw",
        "maybe_raw": dest / "maybe" / "raw",
    }

    rows = manifest.query(
        "SELECT * FROM photos WHERE decision IN ('keep','maybe') ORDER BY seq"
    )
    decisions = []
    failures = []
    files_copied = 0
    raws_copied = 0

    for row in rows:
        bucket = row["decision"]
        record = {
            "path": row["path"],
            "decision": bucket,
            "decided_at": row["decided_at"],
            "exported_to": None,
            "raw_path": row["raw_path"],
            "raw_exported_to": None,
        }
        try:
            dirs[bucket].mkdir(parents=True, exist_ok=True)
            target = _unique_destination(dirs[bucket], os.path.basename(row["path"]))
            shutil.copy2(row["path"], str(target))
            record["exported_to"] = str(target)
            files_copied += 1
        except Exception as exc:
            failures.append({"path": row["path"], "stage": "photo", "error": str(exc)})
            decisions.append(record)
            continue

        if row["raw_path"]:
            raw_dir = dirs["keep_raw"] if bucket == "keep" else dirs["maybe_raw"]
            try:
                raw_dir.mkdir(parents=True, exist_ok=True)
                raw_target = _unique_destination(raw_dir, os.path.basename(row["raw_path"]))
                shutil.copy2(row["raw_path"], str(raw_target))
                record["raw_exported_to"] = str(raw_target)
                raws_copied += 1
            except Exception as exc:
                failures.append({"path": row["raw_path"], "stage": "raw", "error": str(exc)})
        decisions.append(record)

    # Rejected and undecided shots are listed in the report but never copied.
    for row in manifest.query(
        "SELECT path, decision, decided_at FROM photos WHERE decision IN ('reject','undecided')"
        " ORDER BY seq"
    ):
        decisions.append({
            "path": row["path"],
            "decision": row["decision"],
            "decided_at": row["decided_at"],
            "exported_to": None,
            "raw_path": None,
            "raw_exported_to": None,
        })

    counts = manifest.counts()
    unpaired = [dict(r) for r in manifest.query(
        "SELECT path, bytes, mtime FROM unpaired_raws ORDER BY path")]

    report = {
        "tool": "photo-culler v0",
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_root": str(photo_root),
        "destination": str(dest),
        "counts": {
            "keep": counts["keep"],
            "maybe": counts["maybe"],
            "reject": counts["reject"],
            "undecided": counts["undecided"],
            "files_copied": files_copied,
            "raws_copied": raws_copied,
            "failures": len(failures),
            "unpaired_raws": len(unpaired),
        },
        "decisions": decisions,
        "unpaired_raws": unpaired,
        "failures": failures,
    }
    with open(str(dest / "export-report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    return report


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class App:
    """Everything the request handler needs."""

    def __init__(self, photo_root: Path, manifest: Manifest, cache: ProxyCache,
                 prefetcher: Prefetcher):
        self.photo_root = photo_root
        self.manifest = manifest
        self.cache = cache
        self.prefetcher = prefetcher


def photo_dict(row) -> dict:
    return {
        "id": row["id"],
        "seq": row["seq"],
        "name": os.path.basename(row["path"]),
        "stem": row["stem"],
        "width": row["width"],
        "height": row["height"],
        "bytes": row["bytes"],
        "has_raw": bool(row["raw_path"]),
        "raw_name": os.path.basename(row["raw_path"]) if row["raw_path"] else None,
        "decision": row["decision"],
    }


class Handler(BaseHTTPRequestHandler):
    app: App = None
    server_version = "PhotoCuller/0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet by default
        pass

    # -- plumbing ----------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, code: int = 200):
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def _error(self, code: int, message: str):
        self._json({"error": message}, code)

    def _file(self, path: Path, content_type: str, cache: str = None):
        try:
            with open(str(path), "rb") as fh:  # read-only
                data = fh.read()
        except OSError:
            return self._error(404, "not found")
        extra = {"Cache-Control": cache} if cache else None
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache or "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        app = self.app

        if path in ("/", "/index.html"):
            return self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")

        if path.startswith("/static/"):
            name = os.path.basename(path)
            target = STATIC_DIR / name
            if not target.is_file():
                return self._error(404, "not found")
            ctype = {
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".html": "text/html; charset=utf-8",
            }.get(target.suffix.lower(), "application/octet-stream")
            return self._file(target, ctype)

        if path == "/api/session":
            counts = app.manifest.counts()
            total = app.manifest.one(
                "SELECT COUNT(*) AS n FROM photos WHERE seq IS NOT NULL")["n"]
            unpaired = app.manifest.one("SELECT COUNT(*) AS n FROM unpaired_raws")["n"]
            position = max(0, min(int(app.manifest.get_state("position", "0") or 0),
                                  max(0, total - 1)))
            # A finished set has no "where I left off" - the saved position is the
            # last frame, which is a dead end. Reopen at the start instead.
            complete = bool(total) and counts["undecided"] == 0
            if complete:
                position = 0
            return self._json({
                "root": str(app.photo_root),
                "total": total,
                "counts": counts,
                "unpaired_raws": unpaired,
                "position": position,
                "complete": complete,
            })

        if path == "/api/photos":
            rows = app.manifest.query(
                "SELECT * FROM photos WHERE seq IS NOT NULL ORDER BY seq")
            return self._json({"photos": [photo_dict(r) for r in rows]})

        if path == "/api/unpaired":
            rows = app.manifest.query(
                "SELECT path, bytes, mtime FROM unpaired_raws ORDER BY path")
            return self._json({"unpaired_raws": [dict(r) for r in rows]})

        if path.startswith("/proxy/"):
            row = self._photo_row(path.rsplit("/", 1)[-1])
            if row is None:
                return self._error(404, "no such photo")
            try:
                proxy = app.cache.get(row["path"], row["mtime"], row["bytes"])
            except Exception as exc:
                return self._error(500, "proxy failed: %s" % exc)
            return self._file(proxy, "image/jpeg", cache="private, max-age=86400")

        if path.startswith("/full/"):
            row = self._photo_row(path.rsplit("/", 1)[-1])
            if row is None:
                return self._error(404, "no such photo")
            ctype = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".tif": "image/tiff", ".tiff": "image/tiff", ".webp": "image/webp",
            }.get(os.path.splitext(row["path"])[1].lower(), "application/octet-stream")
            return self._file(Path(row["path"]), ctype, cache="private, max-age=600")

        return self._error(404, "not found")

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        app = self.app
        payload = self._body()

        if path == "/api/decide":
            photo_id = payload.get("id")
            decision = payload.get("decision")
            if not isinstance(photo_id, int) or decision not in DECISIONS:
                return self._error(400, "id and a valid decision are required")
            if not app.manifest.set_decision(photo_id, decision):
                return self._error(404, "no such photo")
            return self._json({"ok": True, "counts": app.manifest.counts()})

        if path == "/api/position":
            seq = payload.get("seq")
            if not isinstance(seq, int):
                return self._error(400, "seq required")
            app.manifest.set_state("position", seq)
            app.prefetcher.request_from(seq)
            return self._json({"ok": True})

        if path == "/api/export":
            dest = (payload.get("dest") or "").strip()
            if not dest:
                return self._error(400, "destination folder required")
            try:
                report = export(app.manifest, app.photo_root, Path(dest))
            except ValueError as exc:
                return self._error(400, str(exc))
            except Exception as exc:
                return self._error(500, str(exc))
            return self._json({"ok": True, "report": report})

        return self._error(404, "not found")

    def _photo_row(self, raw_id: str):
        try:
            photo_id = int(raw_id)
        except (TypeError, ValueError):
            return None
        return self.app.manifest.one("SELECT * FROM photos WHERE id=?", (photo_id,))


def pick_port(preferred: int = 0) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
        except OSError:
            s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="cull.py",
        description="Pick which photos from a shoot get printed. Originals are never "
                    "modified, moved, renamed or re-encoded.",
    )
    parser.add_argument("folder", help="folder of photos to review (never written to)")
    parser.add_argument("--port", type=int, default=0, help="preferred port (default: any free)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    parser.add_argument("--scan-only", action="store_true", help="scan and exit")
    parser.add_argument("--export", metavar="DEST",
                        help="export current decisions to DEST and exit")
    args = parser.parse_args(argv)

    photo_root = Path(args.folder).expanduser()
    if not photo_root.is_dir():
        print("error: not a folder: %s" % photo_root, file=sys.stderr)
        return 2
    photo_root = photo_root.resolve()

    if PROJECT_DIR == photo_root or PROJECT_DIR in photo_root.parents:
        print("error: the photo folder must not live inside the project folder.",
              file=sys.stderr)
        return 2

    lib = library_dir(photo_root)
    lib.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(lib / "manifest.sqlite3")

    print("photo-culler v0")
    print("  photos:   %s" % photo_root)
    print("  manifest: %s" % (lib / "manifest.sqlite3"))
    t0 = time.time()
    summary = scan(manifest, photo_root)
    print("  scanned in %.2fs" % (time.time() - t0))

    if summary["photos"] == 0:
        print("\nNothing displayable found. Supported: %s"
              % " ".join(sorted(DISPLAY_EXTS)))
        manifest.close()
        return 1

    if args.export:
        report = export(manifest, photo_root, Path(args.export))
        print("\nExported %d file(s) and %d RAW(s) to %s"
              % (report["counts"]["files_copied"], report["counts"]["raws_copied"],
                 args.export))
        manifest.close()
        return 0

    if args.scan_only:
        manifest.close()
        return 0

    cache = ProxyCache(lib / "proxies")
    prefetcher = Prefetcher(manifest, cache)
    position = int(manifest.get_state("position", "0") or 0)
    prefetcher.request_from(position)

    Handler.app = App(photo_root, manifest, cache, prefetcher)
    port = pick_port(args.port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    url = "http://127.0.0.1:%d/" % port

    counts = manifest.counts()
    print("\n  %d photo(s), resuming at #%d" % (summary["photos"], position + 1))
    print("  keep %d / maybe %d / reject %d / undecided %d"
          % (counts["keep"], counts["maybe"], counts["reject"], counts["undecided"]))
    print("\n  %s   (Ctrl-C to quit)\n" % url)

    if not args.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye - decisions are saved.")
    finally:
        prefetcher.shutdown()
        httpd.server_close()
        manifest.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
