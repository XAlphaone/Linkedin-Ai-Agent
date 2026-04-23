"""Pull commits + merged PRs from GitHub REST API."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from agent.db import insert_event

log = logging.getLogger(__name__)

API = "https://api.github.com"
GH_URL_RE = re.compile(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?/?$")

# How far back to look on each poll. Wide window lets infrequently-updated
# repos still contribute to posts. The UNIQUE(repo_id, sha, event_type)
# constraint on repo_events means re-seeing the same commit is a no-op.
COMMIT_WINDOW_DAYS = 365


def _parse_owner_repo(url: str) -> Optional[tuple[str, str]]:
    m = GH_URL_RE.search(url.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


def _headers(token: str) -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "linkedin-agent"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _paginated_get(
    url: str,
    params: dict,
    headers: dict,
    max_pages: int,
    timeout: int = 20,
) -> list:
    """Follow GitHub's rel=\"next\" Link header until exhausted or max_pages.

    Each response's Link header contains a fully-qualified next URL with
    params already baked in. We just follow it; params only get passed on
    the first request.
    """
    results: list = []
    page_url: Optional[str] = url
    page_params: Optional[dict] = dict(params)
    for page_idx in range(max_pages):
        if not page_url:
            break
        r = requests.get(page_url, params=page_params, headers=headers, timeout=timeout)
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        results.extend(batch)
        nxt = r.links.get("next")
        if not nxt:
            break
        page_url = nxt["url"]
        page_params = None
    else:
        log.warning("paginated_get hit max_pages=%d on %s", max_pages, url)
    return results


def fetch_events(repo: dict, github_token: str) -> tuple[int, Optional[str]]:
    parsed = _parse_owner_repo(repo["path_or_url"])
    if not parsed:
        log.warning("unparseable github url: %s", repo["path_or_url"])
        return 0, None
    owner, name = parsed
    branch = repo.get("branch") or "main"
    last_sha = repo.get("last_sha")
    since = (datetime.now(timezone.utc) - timedelta(days=COMMIT_WINDOW_DAYS)).isoformat()

    inserted = 0
    newest_sha: Optional[str] = last_sha

    # ---- commits on branch ----
    # max_pages=50 at per_page=100 caps at 5,000 commits per poll per repo.
    # That covers 99% of real repos over a 365d window; anything bigger can
    # bump the cap.
    try:
        commits = _paginated_get(
            url=f"{API}/repos/{owner}/{name}/commits",
            params={"sha": branch, "per_page": 100, "since": since},
            headers=_headers(github_token),
            max_pages=50,
        )
    except Exception as e:
        log.warning("github commits fetch failed for %s/%s: %s", owner, name, e)
        commits = []

    # Intentionally do NOT early-exit at last_sha here. The old logic broke
    # when the commit window was widened retroactively (repos that had only
    # 1 qualifying commit under a 7d window had last_sha set to that commit,
    # so after moving to a 365d window the loop exited immediately and the
    # 99 newly-in-window older commits were never inserted). The
    # UNIQUE(repo_id, sha, event_type) constraint dedupes for us cheaply.
    if commits:
        newest_sha = commits[0].get("sha") or newest_sha

    for c in reversed(commits):
        msg = (c.get("commit") or {}).get("message") or ""
        title, _, body = msg.partition("\n")
        author_info = (c.get("commit") or {}).get("author") or {}
        row_id = insert_event(
            repo_id=repo["id"],
            event_type="commit",
            sha=c.get("sha"),
            title=title.strip() or (c.get("sha") or "")[:8],
            body=body.strip() or None,
            files_changed=None,
            author=author_info.get("name") or (c.get("author") or {}).get("login"),
            event_timestamp=author_info.get("date"),
        )
        if row_id is not None:
            inserted += 1

    # ---- merged PRs (within commit window) ----
    # GitHub's /pulls endpoint sorts by updated, not merged_at, so we can't
    # short-circuit when we see an old PR — we have to scan. Cap at 10 pages
    # (1,000 closed PRs) which is plenty for any reasonable repo.
    try:
        pulls = _paginated_get(
            url=f"{API}/repos/{owner}/{name}/pulls",
            params={"state": "closed", "per_page": 100, "sort": "updated", "direction": "desc"},
            headers=_headers(github_token),
            max_pages=10,
        )
    except Exception as e:
        log.warning("github pulls fetch failed for %s/%s: %s", owner, name, e)
        pulls = []

    cutoff = datetime.now(timezone.utc) - timedelta(days=COMMIT_WINDOW_DAYS)
    for p in pulls:
        merged_at = p.get("merged_at")
        if not merged_at:
            continue
        try:
            when = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when < cutoff:
            continue
        merge_sha = p.get("merge_commit_sha") or f"pr-{p.get('number')}"
        row_id = insert_event(
            repo_id=repo["id"],
            event_type="pr_merged",
            sha=merge_sha,
            title=p.get("title") or f"PR #{p.get('number')}",
            body=p.get("body") or None,
            files_changed=None,
            author=(p.get("user") or {}).get("login"),
            event_timestamp=merged_at,
        )
        if row_id is not None:
            inserted += 1

    return inserted, newest_sha
