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


def fetch_events(repo: dict, github_token: str) -> tuple[int, Optional[str]]:
    parsed = _parse_owner_repo(repo["path_or_url"])
    if not parsed:
        log.warning("unparseable github url: %s", repo["path_or_url"])
        return 0, None
    owner, name = parsed
    branch = repo.get("branch") or "main"
    last_sha = repo.get("last_sha")
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    inserted = 0
    newest_sha: Optional[str] = last_sha

    # ---- commits on branch ----
    try:
        r = requests.get(
            f"{API}/repos/{owner}/{name}/commits",
            params={"sha": branch, "per_page": 50, "since": since},
            headers=_headers(github_token),
            timeout=20,
        )
        r.raise_for_status()
        commits = r.json()
    except Exception as e:
        log.warning("github commits fetch failed for %s/%s: %s", owner, name, e)
        commits = []

    new_commits = []
    for c in commits:
        if last_sha and c.get("sha") == last_sha:
            break
        new_commits.append(c)

    if new_commits:
        newest_sha = new_commits[0].get("sha") or newest_sha

    for c in reversed(new_commits):
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

    # ---- merged PRs (last 7 days) ----
    try:
        r = requests.get(
            f"{API}/repos/{owner}/{name}/pulls",
            params={"state": "closed", "per_page": 50, "sort": "updated", "direction": "desc"},
            headers=_headers(github_token),
            timeout=20,
        )
        r.raise_for_status()
        pulls = r.json()
    except Exception as e:
        log.warning("github pulls fetch failed for %s/%s: %s", owner, name, e)
        pulls = []

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
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
