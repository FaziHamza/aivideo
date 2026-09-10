"""Persistent reaction library and job history (req 12).

The library survives restarts and can be added to, relabelled, deactivated or
replaced at any time. Each reaction also carries a pointer to its pre-rendered
720x1280 copy: normalising a reaction once and reusing it across the day's 50-60
videos is the single biggest speed win in the pipeline, so the cache pointer
lives right next to the asset.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .config import CACHE_DIR, DB_PATH, Settings, ensure_dirs
from .models import ReactionAsset

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg",
                  ".mpeg", ".wmv", ".flv", ".ts", ".m2ts"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT NOT NULL UNIQUE,
    label           TEXT NOT NULL DEFAULT '',
    duration        REAL NOT NULL,
    width           INTEGER NOT NULL DEFAULT 0,
    height          INTEGER NOT NULL DEFAULT 0,
    has_audio       INTEGER NOT NULL DEFAULT 1,
    active          INTEGER NOT NULL DEFAULT 1,
    cached_path     TEXT,
    cache_signature TEXT NOT NULL DEFAULT '',
    sort_order      INTEGER NOT NULL DEFAULT 0,
    added_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    output       TEXT,
    ok           INTEGER NOT NULL DEFAULT 0,
    duration     REAL NOT NULL DEFAULT 0,
    clips_used   INTEGER NOT NULL DEFAULT 0,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    elapsed      REAL NOT NULL DEFAULT 0,
    error        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReactionLibrary:
    """SQLite-backed store. Safe to construct per use; connections are short."""

    def __init__(self, db_path: Path | str | None = None):
        ensure_dirs()
        self.db_path = Path(db_path) if db_path else DB_PATH
        self._init_schema()

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # Older databases predate the notes column. ALTER is idempotent
            # enough via the exception: sqlite has no IF NOT EXISTS for
            # columns.
            try:
                conn.execute("ALTER TABLE jobs ADD COLUMN notes TEXT "
                             "DEFAULT ''")
            except sqlite3.OperationalError:
                pass

    @staticmethod
    def _row_to_asset(row: sqlite3.Row) -> ReactionAsset:
        cached = row["cached_path"]
        return ReactionAsset(
            id=int(row["id"]),
            path=Path(row["path"]),
            label=row["label"] or Path(row["path"]).stem,
            duration=float(row["duration"]),
            width=int(row["width"]),
            height=int(row["height"]),
            has_audio=bool(row["has_audio"]),
            active=bool(row["active"]),
            cached_path=Path(cached) if cached else None,
            cache_signature=row["cache_signature"] or "",
            added_at=row["added_at"] or "",
        )

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def all_reactions(self, only_active: bool = False) -> list[ReactionAsset]:
        query = "SELECT * FROM reactions"
        if only_active:
            query += " WHERE active = 1"
        query += " ORDER BY sort_order, id"
        with self._connect() as conn:
            return [self._row_to_asset(r) for r in conn.execute(query)]

    def active_reactions(self) -> list[ReactionAsset]:
        """Reactions usable for a render: active, on disk, non-zero length."""
        return [r for r in self.all_reactions(only_active=True)
                if r.duration > 0.05 and r.path.exists()]

    def count(self, only_active: bool = False) -> int:
        query = "SELECT COUNT(*) FROM reactions"
        if only_active:
            query += " WHERE active = 1"
        with self._connect() as conn:
            return int(conn.execute(query).fetchone()[0])

    def missing_files(self) -> list[ReactionAsset]:
        return [r for r in self.all_reactions() if not r.path.exists()]

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    def add(self, path: Path | str, label: str = "",
            probe_fn=None) -> ReactionAsset:
        """Add (or refresh) one reaction. Re-adding a path updates it in place."""
        from . import ffmpeg  # imported lazily so the DB works without FFmpeg

        path = Path(path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Reaction file not found: {path}")
        if path.suffix.lower() not in VIDEO_SUFFIXES:
            raise ValueError(f"Not a video file: {path.name}")

        # Reactions are short and get reused across every render, so their
        # length is worth counting exactly rather than trusting the container.
        if probe_fn:
            info = probe_fn(path)
        else:
            info = ffmpeg.probe(path, count_frames=True)
        duration = info.usable_duration
        if duration <= 0.05:
            raise ValueError(f"{path.name} has no usable duration.")

        key = str(path)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, cache_signature FROM reactions WHERE path = ?", (key,)
            ).fetchone()
            if existing:
                # Replacing the file behind an existing entry invalidates its
                # cached copy.
                conn.execute(
                    "UPDATE reactions SET label = ?, duration = ?, width = ?, "
                    "height = ?, has_audio = ?, active = 1, cached_path = NULL, "
                    "cache_signature = '' WHERE id = ?",
                    (label or Path(key).stem, duration, info.width,
                     info.height, int(info.has_audio), existing["id"]),
                )
                new_id = int(existing["id"])
            else:
                order = conn.execute(
                    "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM reactions"
                ).fetchone()[0]
                cur = conn.execute(
                    "INSERT INTO reactions (path, label, duration, width, "
                    "height, has_audio, active, sort_order, added_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (key, label or path.stem, duration, info.width,
                     info.height, int(info.has_audio), order, _now()),
                )
                new_id = int(cur.lastrowid)
            row = conn.execute("SELECT * FROM reactions WHERE id = ?",
                               (new_id,)).fetchone()
        return self._row_to_asset(row)

    def add_many(self, paths: Iterable[Path | str]) -> tuple[list[ReactionAsset],
                                                             list[str]]:
        """Bulk add. Returns (added, errors) so one bad file cannot abort a batch."""
        added: list[ReactionAsset] = []
        errors: list[str] = []
        for p in paths:
            try:
                added.append(self.add(p))
            except Exception as exc:
                errors.append(f"{Path(p).name}: {exc}")
        return added, errors

    def remove(self, reaction_id: int, drop_cache: bool = True) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT cached_path FROM reactions WHERE id = ?",
                               (reaction_id,)).fetchone()
            conn.execute("DELETE FROM reactions WHERE id = ?", (reaction_id,))
        if drop_cache and row and row["cached_path"]:
            _unlink_quietly(Path(row["cached_path"]))

    def clear(self, drop_cache: bool = True) -> None:
        """Wipe the library, e.g. before loading a completely new set (req 12)."""
        if drop_cache:
            for r in self.all_reactions():
                if r.cached_path:
                    _unlink_quietly(r.cached_path)
        with self._connect() as conn:
            conn.execute("DELETE FROM reactions")

    def replace_all(self, paths: Iterable[Path | str]) -> tuple[list[ReactionAsset],
                                                                list[str]]:
        """Swap the whole library for a new set in one step."""
        self.clear()
        return self.add_many(paths)

    def set_active(self, reaction_id: int, active: bool) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE reactions SET active = ? WHERE id = ?",
                         (int(active), reaction_id))

    def set_label(self, reaction_id: int, label: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE reactions SET label = ? WHERE id = ?",
                         (label, reaction_id))

    def reorder(self, ordered_ids: list[int]) -> None:
        """Set the rotation order (req 5)."""
        with self._connect() as conn:
            conn.executemany(
                "UPDATE reactions SET sort_order = ? WHERE id = ?",
                [(i, rid) for i, rid in enumerate(ordered_ids)],
            )

    # ------------------------------------------------------------------
    # normalised-segment cache
    # ------------------------------------------------------------------
    def cache_target(self, reaction: ReactionAsset, signature: str) -> Path:
        ensure_dirs()
        return CACHE_DIR / f"reaction_{reaction.id}_{signature}.mp4"

    def set_cache(self, reaction_id: int, cached_path: Optional[Path],
                  signature: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE reactions SET cached_path = ?, cache_signature = ? "
                "WHERE id = ?",
                (str(cached_path) if cached_path else None, signature,
                 reaction_id),
            )

    def valid_cache(self, reaction: ReactionAsset, signature: str) -> Optional[Path]:
        """The reaction's normalised copy, if it matches current settings."""
        if not reaction.cached_path or reaction.cache_signature != signature:
            return None
        if not reaction.cached_path.exists():
            return None
        return reaction.cached_path

    def purge_cache(self, keep_signature: str = "") -> int:
        """Delete stale normalised segments. Returns how many were removed."""
        ensure_dirs()
        removed = 0
        live = {str(r.cached_path) for r in self.all_reactions()
                if r.cached_path and r.cache_signature == keep_signature}
        for f in CACHE_DIR.glob("reaction_*.mp4"):
            if str(f) not in live:
                if _unlink_quietly(f):
                    removed += 1
        with self._connect() as conn:
            conn.execute(
                "UPDATE reactions SET cached_path = NULL, cache_signature = '' "
                "WHERE cache_signature != ?", (keep_signature,))
        return removed

    def import_copy(self, path: Path | str, store_dir: Path) -> Path:
        """Copy a reaction into a managed folder so the library is portable."""
        path = Path(path)
        store_dir = Path(store_dir)
        store_dir.mkdir(parents=True, exist_ok=True)
        target = store_dir / path.name
        counter = 1
        while target.exists() and not target.samefile(path):
            target = store_dir / f"{path.stem}_{counter}{path.suffix}"
            counter += 1
        if not target.exists():
            shutil.copy2(path, target)
        return target

    # ------------------------------------------------------------------
    # rotation position + job history
    # ------------------------------------------------------------------
    def get_state(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key = ?",
                               (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def get_rotation_offset(self) -> int:
        try:
            return int(self.get_state("rotation_offset", "0"))
        except ValueError:
            return 0

    def set_rotation_offset(self, offset: int) -> None:
        self.set_state("rotation_offset", str(int(offset)))

    def record_job(self, source: Path | str, output: Optional[Path], ok: bool,
                   duration: float = 0.0, clips_used: int = 0,
                   size_bytes: int = 0, elapsed: float = 0.0,
                   error: str = "", notes: str = "") -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO jobs (source, output, ok, duration, clips_used, "
                "size_bytes, elapsed, error, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(source), str(output) if output else None, int(ok),
                 duration, clips_used, size_bytes, elapsed, error, notes,
                 _now()),
            )
            return int(cur.lastrowid)

    def recent_jobs(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def jobs_done_today(self) -> int:
        """Progress against the 50-60/day target (req 10)."""
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE ok = 1 AND created_at LIKE ?",
                (f"{today}%",),
            ).fetchone()
        return int(row[0])


def _unlink_quietly(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError:
        return False


def next_output_path(source: Path, settings: Settings) -> Path:
    """First free output name for `source`, per the configured template."""
    out_dir = Path(settings.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = source.stem
    for n in range(1, 10_000):
        try:
            name = settings.output_template.format(stem=stem, n=n)
        except (KeyError, IndexError):
            name = f"{stem}_reaction_{n:03d}.mp4"
        candidate = out_dir / name
        if not candidate.exists():
            return candidate
    return out_dir / f"{stem}_reaction_{_now().replace(':', '')}.mp4"
