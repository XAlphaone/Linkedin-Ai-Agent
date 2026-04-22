"""Walk a local git repo; insert new commits as repo_events."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from agent.db import insert_event

log = logging.getLogger(__name__)


def walk_commits(repo: dict) -> tuple[int, Optional[str]]:
    """Walk commits newer than repo['last_sha']; return (inserted_count, newest_sha)."""
    try:
        from git import Repo, InvalidGitRepositoryError, NoSuchPathError
    except ImportError:
        log.warning("GitPython not installed; skipping local repo %s", repo["name"])
        return 0, None

    path = repo["path_or_url"]
    try:
        git_repo = Repo(path)
    except (InvalidGitRepositoryError, NoSuchPathError, Exception) as e:
        log.warning("cannot open local repo %s at %s: %s", repo["name"], path, e)
        return 0, None

    branch = repo.get("branch") or "main"
    last_sha = repo.get("last_sha")

    try:
        rev = branch
        commits = list(git_repo.iter_commits(rev, max_count=200))
    except Exception as e:
        log.warning("iter_commits failed for %s (%s): %s", repo["name"], branch, e)
        return 0, None

    new_commits: list = []
    for c in commits:
        if last_sha and c.hexsha == last_sha:
            break
        new_commits.append(c)

    inserted = 0
    for c in reversed(new_commits):  # oldest first for stable insertion
        title, _, body = c.message.partition("\n")
        files_changed = list(c.stats.files.keys()) if c.stats else []
        ts = datetime.fromtimestamp(c.committed_date, tz=timezone.utc).isoformat()
        row_id = insert_event(
            repo_id=repo["id"],
            event_type="commit",
            sha=c.hexsha,
            title=title.strip() or c.hexsha[:8],
            body=body.strip() or None,
            files_changed=files_changed,
            author=str(c.author),
            event_timestamp=ts,
        )
        if row_id is not None:
            inserted += 1

    newest_sha = commits[0].hexsha if commits else last_sha
    return inserted, newest_sha
