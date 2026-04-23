"""APScheduler jobs — repo polling + daily generation."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from agent.config import Config
from agent.db import (
    list_repos,
    update_repo_checkpoint,
    balanced_unprocessed_events,
    mark_events_processed,
    new_variant_group,
    insert_post,
)

# How many newest unprocessed events to pull from each repo when Generate Now
# runs without an explicit selection. Ensures every active repo contributes
# to each batch rather than one chatty repo starving the rest.
EVENTS_PER_REPO_DEFAULT = 4

log = logging.getLogger(__name__)


def poll_one_repo(cfg: Config, repo: dict) -> int:
    """Poll a single repo. Inserts new events, updates checkpoint on success.
    Returns count of new events inserted. Never raises."""
    from agent.watchers import git_local, github_api
    try:
        if repo["type"] == "local":
            inserted, last_sha = git_local.walk_commits(repo)
        else:
            inserted, last_sha = github_api.fetch_events(repo, cfg.github_token)
        if last_sha:
            update_repo_checkpoint(
                repo["id"],
                last_sha,
                datetime.now(timezone.utc).isoformat(),
            )
        log.info("polled %s: +%d events", repo["name"], inserted)
        return inserted
    except Exception:
        log.exception("polling repo %s failed", repo["name"])
        return 0


def poll_repos_job(cfg: Config) -> int:
    """Walk every enabled repo. Returns total count of new events inserted."""
    total = 0
    for repo in list_repos(enabled_only=True):
        total += poll_one_repo(cfg, repo)
    log.info("poll_repos: inserted %d events", total)
    return total


def generate_variants_job(cfg: Config, events: list[dict] | None = None) -> int:
    """Generate 3 variants + images.

    If `events` is None (cron or 'Generate Now' default), pull up to 20
    unprocessed events. If a list is passed in (from the /events UI), use it
    verbatim — caller is responsible for filtering to unprocessed.
    """
    from agent.generator import grok as gen_client
    from agent.generator import grok_images
    from agent.db import set_post_image_path

    if events is None:
        events = balanced_unprocessed_events(per_repo=EVENTS_PER_REPO_DEFAULT)
        log.info(
            "generate_variants: balanced draw — %d events across %d repos",
            len(events),
            len({e["repo_id"] for e in events}),
        )
    variants = gen_client.generate_variants(cfg, events)
    if not variants:
        log.info("generate_variants: nothing produced")
        return 0

    vg = new_variant_group()
    source_ids = [e["id"] for e in events]
    inserted: list[tuple[int, dict]] = []
    for v in variants:
        post_id = insert_post(
            source_event_ids=source_ids,
            angle=v["angle"],
            variant_group=vg,
            hook=v["hook"],
            content=v["content"],
        )
        inserted.append((post_id, v))
    mark_events_processed(source_ids)
    log.info("generate_variants: group=%s variants=%d", vg, len(variants))

    # Best-effort image per variant. Failures don't affect the posts.
    if cfg.generation.generate_images:
        for post_id, v in inserted:
            path = grok_images.generate_image_for_post(
                cfg=cfg,
                post_id=post_id,
                hook=v["hook"],
                content=v["content"],
            )
            if path:
                set_post_image_path(post_id, path)
    return len(variants)


def start_scheduler(cfg: Config) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(
        poll_repos_job,
        trigger=IntervalTrigger(hours=cfg.generation.poll_interval_hours),
        args=[cfg],
        id="poll_repos",
        replace_existing=True,
    )
    sched.add_job(
        generate_variants_job,
        trigger=CronTrigger.from_crontab(cfg.generation.daily_generate_cron),
        args=[cfg],
        id="generate_variants",
        replace_existing=True,
    )
    sched.start()
    log.info(
        "scheduler started: poll every %dh; generate cron=%r",
        cfg.generation.poll_interval_hours,
        cfg.generation.daily_generate_cron,
    )
    return sched
