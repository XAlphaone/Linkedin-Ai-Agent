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
    """Poll a single source (git repo or RSS feed). Inserts new events,
    updates checkpoint on success. Returns count of new events. Never raises."""
    from agent.watchers import git_local, github_api, rss
    try:
        t = repo["type"]
        if t == "local":
            inserted, last_sha = git_local.walk_commits(repo)
        elif t == "github":
            inserted, last_sha = github_api.fetch_events(repo, cfg.github_token)
        elif t == "rss":
            inserted, last_sha = rss.fetch_items(repo)
        else:
            log.warning("unknown repo type %s for %s", t, repo["name"])
            return 0
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


def generate_variants_job(
    cfg: Config,
    events: list[dict] | None = None,
    target: str = "personal",
    user_image_path: str | None = None,
) -> int:
    """Generate 3 variants + images.

    If `events` is None (cron or 'Generate Now' default), pull up to 20
    unprocessed events. If a list is passed in (from the /events UI), use it
    verbatim — caller is responsible for filtering to unprocessed.

    `target` picks the voice ('personal' or a brand_voices key).

    `user_image_path`: when provided (e.g. from /compose with an upload), all
    variants share that image and the per-variant Grok image generation is
    skipped. When None, the normal two-pass image pipeline runs per variant.
    """
    from agent.generator import grok as gen_client
    from agent.generator import grok_images
    from agent.db import set_post_image_path

    if events is None:
        events = balanced_unprocessed_events(per_repo=EVENTS_PER_REPO_DEFAULT)
        log.info(
            "generate_variants: balanced draw — %d events across %d repos, target=%s",
            len(events),
            len({e["repo_id"] for e in events}),
            target,
        )
    variants = gen_client.generate_variants(cfg, events, target=target)
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
            draft_target=target,
        )
        inserted.append((post_id, v))
    mark_events_processed(source_ids)
    log.info("generate_variants: group=%s variants=%d", vg, len(variants))

    # Image handling. User-supplied image short-circuits the AI gen path.
    if user_image_path:
        log.info("user supplied image %s — skipping AI image gen for %d variants",
                 user_image_path, len(inserted))
        for post_id, _v in inserted:
            set_post_image_path(post_id, user_image_path)
    elif cfg.generation.generate_images:
        # Best-effort image per variant. Failures don't affect the posts.
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


def reddit_scan_job(cfg: Config) -> dict:
    """Scheduled reddit scan. Summary is logged; errors are non-fatal."""
    from agent.scanners import reddit as reddit_scanner
    try:
        return reddit_scanner.run_scan(cfg)
    except Exception:
        log.exception("reddit_scan_job crashed")
        return {"errors": ["exception"]}


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
    if cfg.reddit_scan.enabled:
        sched.add_job(
            reddit_scan_job,
            trigger=IntervalTrigger(hours=cfg.reddit_scan.scan_interval_hours),
            args=[cfg],
            id="reddit_scan",
            replace_existing=True,
        )
    sched.start()
    log.info(
        "scheduler started: poll every %dh; generate cron=%r; reddit_scan=%s",
        cfg.generation.poll_interval_hours,
        cfg.generation.daily_generate_cron,
        f"every {cfg.reddit_scan.scan_interval_hours}h" if cfg.reddit_scan.enabled else "off",
    )
    return sched
