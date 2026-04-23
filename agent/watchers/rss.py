"""Fetch recent items from an RSS/Atom feed and insert them as repo_events.

Each feed is tracked as a repo row (type='rss'). Items dedupe by entry link
via the UNIQUE(repo_id, sha, event_type) constraint — the link is stored in
the sha column. Nothing exotic; fits the existing schema without changes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from agent.db import insert_event

log = logging.getLogger(__name__)

# Only consider articles from the last year — matches the 365d window used for
# git activity so the /events page has a consistent horizon.
ARTICLE_WINDOW_DAYS = 365
MAX_ITEMS_PER_POLL = 50


def _parse_published(entry) -> Optional[str]:
    """feedparser normalizes to .published_parsed / .updated_parsed (time.struct_time).
    Return an ISO 8601 UTC string, or None if we can't figure it out."""
    import time
    struct = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not struct:
        return None
    try:
        ts = time.mktime(struct)  # local time approximation; good enough for ordering
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return None


def fetch_items(repo: dict, _unused_token: str = "") -> tuple[int, Optional[str]]:
    """Pull recent items from the feed. Returns (inserted_count, newest_link).

    Signature mirrors github_api.fetch_events so scheduler.poll_one_repo can
    dispatch uniformly.
    """
    try:
        import feedparser
    except ImportError:
        log.error("feedparser not installed; run: pip install -r requirements.txt")
        return 0, None

    url = repo.get("path_or_url") or ""
    if not url:
        return 0, None

    try:
        parsed = feedparser.parse(url)
    except Exception as e:
        log.warning("feed parse failed for %s: %s", url, e)
        return 0, None

    if parsed.get("bozo") and parsed.get("bozo_exception"):
        # Not necessarily fatal — feedparser is lenient — but log it.
        log.info("feed %s had a bozo_exception: %s", url, parsed.get("bozo_exception"))

    entries = parsed.get("entries") or []
    if not entries:
        log.warning("feed %s returned zero entries", url)
        return 0, None

    cutoff = datetime.now(timezone.utc).timestamp() - (ARTICLE_WINDOW_DAYS * 86400)

    inserted = 0
    newest_link: Optional[str] = None
    for entry in entries[:MAX_ITEMS_PER_POLL]:
        link = entry.get("link") or entry.get("id")
        if not link:
            continue
        if newest_link is None:
            newest_link = link

        published_iso = _parse_published(entry)
        if published_iso:
            try:
                ts = datetime.fromisoformat(published_iso).timestamp()
                if ts < cutoff:
                    continue
            except Exception:
                pass  # accept the item if timestamp is funny

        title = (entry.get("title") or "").strip() or link
        body = (entry.get("summary") or entry.get("description") or "").strip()
        # Strip basic HTML tags — summaries often have <p> / <a> / etc that
        # bloat the prompt. Nothing fancy; feedparser's bleached output is fine.
        import re
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body).strip()[:1500]

        author = (entry.get("author") or "").strip() or None

        row_id = insert_event(
            repo_id=repo["id"],
            event_type="article",
            sha=link,  # unique per feed
            title=title[:500],
            body=body or None,
            files_changed=None,
            author=author,
            event_timestamp=published_iso,
        )
        if row_id is not None:
            inserted += 1

    log.info("rss %s: %d new items (total seen=%d)", repo["name"], inserted, len(entries))
    return inserted, newest_link
