"""Reddit opportunity scanner — public JSON endpoints only, no OAuth.

Reddit removed self-service API app creation in November 2025 (Responsible
Builder Policy). The OAuth path is no longer practically available for
personal / small-commercial use. But every public Reddit listing is still
exposed as JSON — append .json to any subreddit or search URL — rate-limited
to ~10 requests/minute when you send a real User-Agent.

So this scanner drops the OAuth dance entirely. It:
  - hits www.reddit.com/r/<sub>/{hot,new}.json for each configured subreddit
  - hits www.reddit.com/search.json?q=... for each configured query
  - throttles itself to one request every ~6.5s (comfortably under 10/min)
  - requires REDDIT_USER_AGENT in .env (Reddit 429s generic UAs)

That's it. No client_id, no client_secret, no Developer Support application.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from agent.config import Config
from agent.db import insert_reddit_opportunity

log = logging.getLogger(__name__)

PUBLIC_BASE = "https://www.reddit.com"
# Reddit: "approximately 10 requests per minute" for public JSON. 6.5s gives headroom.
MIN_SECONDS_BETWEEN_REQUESTS = 6.5


class _Throttle:
    """Simple spacing throttle — ensures requests don't fire faster than the
    public-JSON limit. A single instance is reused across one scan."""

    def __init__(self) -> None:
        self._last = 0.0

    def wait(self) -> None:
        now = time.time()
        elapsed = now - self._last
        if elapsed < MIN_SECONDS_BETWEEN_REQUESTS:
            time.sleep(MIN_SECONDS_BETWEEN_REQUESTS - elapsed)
        self._last = time.time()


def _posted_at(entry: dict) -> Optional[str]:
    created = entry.get("created_utc")
    if created is None:
        return None
    try:
        return datetime.fromtimestamp(float(created), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _ingest_listing(listing: dict, matched_by: str) -> int:
    """Walk a reddit listing and insert each t3 (submission) child. Returns new rows."""
    inserted = 0
    children = (listing.get("data") or {}).get("children") or []
    for c in children:
        if c.get("kind") != "t3":  # t3 == submission; skip comments
            continue
        d = c.get("data") or {}
        permalink = d.get("permalink")
        if not permalink:
            continue
        full_permalink = f"https://www.reddit.com{permalink}"

        row_id = insert_reddit_opportunity(
            permalink=full_permalink,
            subreddit=d.get("subreddit") or "",
            title=(d.get("title") or "").strip()[:500],
            body=(d.get("selftext") or "").strip()[:4000] or None,
            author=d.get("author"),
            score=int(d.get("score") or 0),
            num_comments=int(d.get("num_comments") or 0),
            reddit_id=d.get("name"),
            url=d.get("url") or full_permalink,
            posted_at=_posted_at(d),
            matched_by=matched_by,
        )
        if row_id is not None:
            inserted += 1
    return inserted


def _fetch(
    throttle: _Throttle,
    user_agent: str,
    path: str,
    params: Optional[dict] = None,
) -> Optional[dict]:
    """GET one public JSON page with throttling + one retry on 429."""
    throttle.wait()
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    url = f"{PUBLIC_BASE}{path}"
    try:
        resp = requests.get(url, headers=headers, params=params or {}, timeout=20)
        if resp.status_code == 429:
            log.warning("reddit 429 on %s — sleeping 30s before retry", path)
            time.sleep(30)
            throttle.wait()
            resp = requests.get(url, headers=headers, params=params or {}, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        log.exception("reddit fetch failed for %s", path)
        return None


def scan_subreddit(
    throttle: _Throttle,
    user_agent: str,
    subreddit: str,
    limit: int = 25,
) -> int:
    """Pull recent hot + new from one subreddit via the public JSON endpoint."""
    total = 0
    for sort in ("hot", "new"):
        data = _fetch(
            throttle, user_agent,
            f"/r/{subreddit}/{sort}.json",
            {"limit": limit, "t": "week", "raw_json": 1},
        )
        if data:
            total += _ingest_listing(data, matched_by=f"subreddit:{subreddit}:{sort}")
    return total


def search_reddit(
    throttle: _Throttle,
    user_agent: str,
    query: str,
    limit: int = 25,
) -> int:
    """Full-text search all of Reddit via /search.json."""
    data = _fetch(
        throttle, user_agent,
        "/search.json",
        {
            "q": query,
            "limit": limit,
            "sort": "new",
            "type": "link",
            "t": "month",
            "raw_json": 1,
        },
    )
    if not data:
        return 0
    return _ingest_listing(data, matched_by=f"query:{query}")


def run_scan(cfg: Config) -> dict:
    """Run one full scan pass. Summary dict is returned and logged."""
    scan_cfg = cfg.reddit_scan
    summary = {"subreddit_hits": 0, "query_hits": 0, "errors": []}

    if not scan_cfg.enabled:
        log.info("reddit_scan: disabled in config")
        return summary
    if not cfg.reddit_user_agent:
        log.warning(
            "reddit_scan: REDDIT_USER_AGENT not set in .env — skipping "
            "(public JSON works without OAuth, but Reddit 429s generic UAs)"
        )
        summary["errors"].append("user-agent not configured")
        return summary

    throttle = _Throttle()

    for sub in scan_cfg.subreddits:
        try:
            n = scan_subreddit(throttle, cfg.reddit_user_agent, sub, scan_cfg.per_source_limit)
            summary["subreddit_hits"] += n
        except Exception:
            log.exception("subreddit scan failed for %s", sub)
            summary["errors"].append(f"subreddit:{sub}")

    for q in scan_cfg.queries:
        try:
            n = search_reddit(throttle, cfg.reddit_user_agent, q, scan_cfg.per_source_limit)
            summary["query_hits"] += n
        except Exception:
            log.exception("reddit query scan failed for %s", q)
            summary["errors"].append(f"query:{q}")

    log.info(
        "reddit_scan: +%d from subreddits, +%d from queries, errors=%s",
        summary["subreddit_hits"], summary["query_hits"], summary["errors"],
    )
    return summary
