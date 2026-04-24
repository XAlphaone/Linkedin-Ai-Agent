"""Reddit opportunity scanner.

Pulls recent posts from a configured list of subreddits and full-text-search
queries, looking for pain-point language ("I wish there was an app that...")
and product-idea patterns. Inserts hits into reddit_opportunities; dedup is
handled by UNIQUE(permalink).

Uses Reddit's application-only OAuth (installed_client grant), so no user
login is needed. The caller still needs REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET
+ a real REDDIT_USER_AGENT (Reddit 429s generic UAs).

Docs: https://github.com/reddit-archive/reddit/wiki/OAuth2
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

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH_BASE = "https://oauth.reddit.com"

# Simple module-level token cache so a single scan doesn't re-auth per call.
_TOKEN_CACHE: dict = {"token": None, "expires_at": 0.0}


def _get_app_token(client_id: str, client_secret: str, user_agent: str) -> Optional[str]:
    now = time.time()
    if _TOKEN_CACHE["token"] and _TOKEN_CACHE["expires_at"] - 60 > now:
        return _TOKEN_CACHE["token"]

    try:
        resp = requests.post(
            TOKEN_URL,
            auth=(client_id, client_secret),
            data={
                "grant_type": "https://oauth.reddit.com/grants/installed_client",
                "device_id": "DO_NOT_TRACK_THIS_DEVICE",
            },
            headers={"User-Agent": user_agent},
            timeout=20,
        )
        resp.raise_for_status()
    except Exception:
        log.exception("reddit token fetch failed")
        return None

    data = resp.json()
    token = data.get("access_token")
    expires_in = int(data.get("expires_in") or 3600)
    if not token:
        log.error("reddit returned no access_token: %s", data)
        return None

    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["expires_at"] = now + expires_in
    return token


def _posted_at(entry: dict) -> Optional[str]:
    created = entry.get("created_utc")
    if created is None:
        return None
    try:
        return datetime.fromtimestamp(float(created), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _ingest_listing(listing: dict, matched_by: str) -> int:
    """Walk a reddit listing response and insert each child. Returns count of new rows."""
    inserted = 0
    children = (listing.get("data") or {}).get("children") or []
    for c in children:
        kind = c.get("kind")
        if kind != "t3":  # t3 == submission; skip comments etc
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
            reddit_id=d.get("name"),  # t3_xxxx
            url=d.get("url") or full_permalink,
            posted_at=_posted_at(d),
            matched_by=matched_by,
        )
        if row_id is not None:
            inserted += 1
    return inserted


def _fetch(
    token: str,
    user_agent: str,
    path: str,
    params: Optional[dict] = None,
) -> Optional[dict]:
    try:
        resp = requests.get(
            f"{OAUTH_BASE}{path}",
            headers={"Authorization": f"Bearer {token}", "User-Agent": user_agent},
            params=params or {},
            timeout=20,
        )
        if resp.status_code == 429:
            log.warning("reddit rate-limited on %s — sleeping a beat", path)
            time.sleep(2)
            resp = requests.get(
                f"{OAUTH_BASE}{path}",
                headers={"Authorization": f"Bearer {token}", "User-Agent": user_agent},
                params=params or {},
                timeout=20,
            )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        log.exception("reddit fetch failed for %s", path)
        return None


def scan_subreddit(token: str, user_agent: str, subreddit: str, limit: int = 25) -> int:
    """Pull recent hot+new posts from one subreddit."""
    total = 0
    for sort in ("hot", "new"):
        data = _fetch(
            token, user_agent,
            f"/r/{subreddit}/{sort}",
            {"limit": limit, "t": "week"},
        )
        if data:
            total += _ingest_listing(data, matched_by=f"subreddit:{subreddit}:{sort}")
    return total


def search_reddit(token: str, user_agent: str, query: str, limit: int = 25) -> int:
    """Full-text search Reddit for a query (no subreddit restriction)."""
    data = _fetch(
        token, user_agent,
        "/search",
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
    """Run one full scan pass using cfg.reddit_scan. Returns a summary dict."""
    scan_cfg = cfg.reddit_scan
    summary = {"subreddit_hits": 0, "query_hits": 0, "errors": []}

    if not scan_cfg.enabled:
        log.info("reddit_scan: disabled in config")
        return summary
    if not (cfg.reddit_client_id and cfg.reddit_client_secret and cfg.reddit_user_agent):
        log.warning("reddit_scan: REDDIT_CLIENT_ID / SECRET / USER_AGENT not set — skipping")
        summary["errors"].append("credentials not configured")
        return summary

    token = _get_app_token(
        cfg.reddit_client_id, cfg.reddit_client_secret, cfg.reddit_user_agent,
    )
    if not token:
        summary["errors"].append("oauth token failed")
        return summary

    for sub in scan_cfg.subreddits:
        try:
            n = scan_subreddit(token, cfg.reddit_user_agent, sub, scan_cfg.per_source_limit)
            summary["subreddit_hits"] += n
        except Exception:
            log.exception("subreddit scan failed for %s", sub)
            summary["errors"].append(f"subreddit:{sub}")

    for q in scan_cfg.queries:
        try:
            n = search_reddit(token, cfg.reddit_user_agent, q, scan_cfg.per_source_limit)
            summary["query_hits"] += n
        except Exception:
            log.exception("reddit query scan failed for %s", q)
            summary["errors"].append(f"query:{q}")

    log.info(
        "reddit_scan: +%d from subreddits, +%d from queries, errors=%s",
        summary["subreddit_hits"], summary["query_hits"], summary["errors"],
    )
    return summary
