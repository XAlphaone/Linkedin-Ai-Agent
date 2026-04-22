"""SQLite schema + dict-returning helpers."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

DB_PATH = Path("data/agent.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    path_or_url TEXT NOT NULL,
    branch TEXT DEFAULT 'main',
    enabled INTEGER DEFAULT 1,
    last_checked_at TEXT,
    last_sha TEXT
);

CREATE TABLE IF NOT EXISTS repo_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    sha TEXT,
    title TEXT,
    body TEXT,
    files_changed TEXT,
    author TEXT,
    event_timestamp TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    processed INTEGER DEFAULT 0,
    UNIQUE(repo_id, sha, event_type)
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    source_event_ids TEXT,
    angle TEXT NOT NULL,
    variant_group TEXT NOT NULL,
    hook TEXT,
    content TEXT NOT NULL,
    edited_content TEXT,
    status TEXT DEFAULT 'pending',
    posted_at TEXT,
    image_path TEXT
);

CREATE TABLE IF NOT EXISTS engagement (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    measured_at TEXT DEFAULT CURRENT_TIMESTAMP,
    impressions INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    comments INTEGER DEFAULT 0,
    reshares INTEGER DEFAULT 0,
    profile_visits INTEGER DEFAULT 0,
    followers_delta INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_events_processed ON repo_events(processed);
"""


def _row_to_dict(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


@contextmanager
def connect(db_path: Path | str = DB_PATH) -> Iterator[sqlite3.Connection]:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = _row_to_dict
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path | str = DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Lightweight migrations for existing DBs.
        existing_post_cols = {row["name"] for row in conn.execute("PRAGMA table_info(posts)").fetchall()}
        if "image_path" not in existing_post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN image_path TEXT")


# -------------------- repos --------------------

def upsert_repo(
    name: str,
    type_: str,
    path_or_url: str,
    branch: str = "main",
    enabled: bool = True,
) -> dict:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO repos (name, type, path_or_url, branch, enabled)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                type = excluded.type,
                path_or_url = excluded.path_or_url,
                branch = excluded.branch,
                enabled = excluded.enabled
            """,
            (name, type_, path_or_url, branch, 1 if enabled else 0),
        )
        return get_repo_by_name(name)


def list_repos(enabled_only: bool = False) -> list[dict]:
    with connect() as conn:
        sql = "SELECT * FROM repos"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name"
        return list(conn.execute(sql).fetchall())


def get_repo_by_name(name: str) -> Optional[dict]:
    with connect() as conn:
        return conn.execute("SELECT * FROM repos WHERE name = ?", (name,)).fetchone()


def set_repo_enabled(repo_id: int, enabled: bool) -> None:
    with connect() as conn:
        conn.execute("UPDATE repos SET enabled = ? WHERE id = ?", (1 if enabled else 0, repo_id))


def update_repo_checkpoint(repo_id: int, last_sha: str, last_checked_at: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE repos SET last_sha = ?, last_checked_at = ? WHERE id = ?",
            (last_sha, last_checked_at, repo_id),
        )


# -------------------- repo_events --------------------

def insert_event(
    repo_id: int,
    event_type: str,
    sha: Optional[str],
    title: Optional[str],
    body: Optional[str],
    files_changed: Optional[list[str]],
    author: Optional[str],
    event_timestamp: Optional[str],
) -> Optional[int]:
    """Insert a repo event; returns row id or None on conflict (duplicate)."""
    with connect() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO repo_events
                    (repo_id, event_type, sha, title, body, files_changed, author, event_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo_id,
                    event_type,
                    sha,
                    title,
                    body,
                    json.dumps(files_changed or []),
                    author,
                    event_timestamp,
                ),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def unprocessed_events(limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT e.*, r.name AS repo_name
            FROM repo_events e
            JOIN repos r ON r.id = e.repo_id
            WHERE e.processed = 0
            ORDER BY e.event_timestamp DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for r in rows:
            try:
                r["files_changed"] = json.loads(r.get("files_changed") or "[]")
            except Exception:
                r["files_changed"] = []
        return list(rows)


def mark_events_processed(event_ids: Iterable[int]) -> None:
    ids = list(event_ids)
    if not ids:
        return
    with connect() as conn:
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE repo_events SET processed = 1 WHERE id IN ({placeholders})",
            ids,
        )


# -------------------- posts --------------------

def new_variant_group() -> str:
    return uuid.uuid4().hex


def insert_post(
    source_event_ids: list[int],
    angle: str,
    variant_group: str,
    hook: str,
    content: str,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO posts (source_event_ids, angle, variant_group, hook, content)
            VALUES (?, ?, ?, ?, ?)
            """,
            (json.dumps(source_event_ids), angle, variant_group, hook, content),
        )
        return cur.lastrowid


def set_post_image_path(post_id: int, image_path: Optional[str]) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE posts SET image_path = ? WHERE id = ?",
            (image_path, post_id),
        )


def pending_groups() -> list[list[dict]]:
    """Return pending posts grouped by variant_group, newest group first."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM posts
            WHERE status = 'pending'
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for row in rows:
        vg = row["variant_group"]
        if vg not in groups:
            groups[vg] = []
            order.append(vg)
        groups[vg].append(row)
    return [groups[vg] for vg in order]


def get_post(post_id: int) -> Optional[dict]:
    with connect() as conn:
        return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()


def mark_post_posted(post_id: int, edited_content: Optional[str]) -> None:
    post = get_post(post_id)
    if post is None:
        return
    with connect() as conn:
        conn.execute(
            """
            UPDATE posts
            SET status = 'posted',
                edited_content = ?,
                posted_at = datetime('now')
            WHERE id = ?
            """,
            (edited_content, post_id),
        )
        # Auto-reject siblings in the same variant_group
        conn.execute(
            """
            UPDATE posts
            SET status = 'rejected'
            WHERE variant_group = ? AND id != ? AND status = 'pending'
            """,
            (post["variant_group"], post_id),
        )


def mark_post_rejected(post_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE posts SET status = 'rejected' WHERE id = ?",
            (post_id,),
        )


def recent_history(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                   e.impressions, e.likes, e.comments, e.reshares,
                   e.profile_visits, e.followers_delta,
                   e.id AS engagement_id
            FROM posts p
            LEFT JOIN engagement e ON e.post_id = p.id
            WHERE p.status IN ('posted', 'rejected')
            ORDER BY COALESCE(p.posted_at, p.created_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return list(rows)


# -------------------- engagement --------------------

def upsert_engagement(
    post_id: int,
    impressions: int,
    likes: int,
    comments: int,
    reshares: int,
    profile_visits: int,
    followers_delta: int,
) -> None:
    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM engagement WHERE post_id = ?", (post_id,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE engagement
                SET impressions = ?, likes = ?, comments = ?, reshares = ?,
                    profile_visits = ?, followers_delta = ?, measured_at = datetime('now')
                WHERE post_id = ?
                """,
                (impressions, likes, comments, reshares, profile_visits, followers_delta, post_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO engagement
                    (post_id, impressions, likes, comments, reshares,
                     profile_visits, followers_delta)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (post_id, impressions, likes, comments, reshares, profile_visits, followers_delta),
            )


def engagement_by_angle() -> list[dict]:
    """Return rows of (angle, score) for every posted post with engagement."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT p.angle,
                   e.impressions, e.likes, e.comments, e.reshares
            FROM posts p
            JOIN engagement e ON e.post_id = p.id
            WHERE p.status = 'posted'
            """
        ).fetchall()
        return list(rows)
