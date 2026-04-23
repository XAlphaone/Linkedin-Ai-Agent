"""Bulk-upsert RSS feeds into the agent's watch list.

Idempotent: re-running is safe. Edit FEEDS below to add/remove, then run:

    python scripts/seed_feeds.py

By default it also triggers an immediate poll of each feed so articles
show up on /events right away. Pass --no-poll to skip that.

Feeds below are curated for a software engineer building across AI,
trading, sports analytics, insurance AI, and content tooling. Reorganize
freely for your own domains.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as `python scripts/seed_feeds.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import load_config
from agent.db import init_db, upsert_repo
from agent.scheduler import poll_one_repo


FEEDS: list[tuple[str, str]] = [
    # ---- General tech & software development ----
    ("Hacker News",          "https://hnrss.org/frontpage"),
    ("DEV Community",        "https://dev.to/feed"),
    ("freeCodeCamp",         "https://www.freecodecamp.org/news/rss/"),
    ("Stack Overflow Blog",  "https://stackoverflow.blog/feed/"),
    ("GitHub Blog",          "https://github.blog/feed/"),
    ("Smashing Magazine",    "https://www.smashingmagazine.com/feed/"),
    ("CSS-Tricks",           "https://css-tricks.com/feed/"),
    ("A List Apart",         "https://alistapart.com/main/feed/"),
    ("SitePoint",            "https://www.sitepoint.com/feed/"),
    ("r/programming",        "https://www.reddit.com/r/programming.rss"),
    ("r/webdev",             "https://www.reddit.com/r/webdev.rss"),
    ("r/coding",             "https://www.reddit.com/r/coding.rss"),

    # ---- AI & ML ----
    ("MIT Technology Review", "https://www.technologyreview.com/feed/"),
    ("WIRED AI",              "https://www.wired.com/feed/tag/ai/latest/rss"),
    ("arXiv cs.AI",           "http://export.arxiv.org/rss/cs.AI"),
    ("arXiv cs.LG",           "http://export.arxiv.org/rss/cs.LG"),
    ("DeepMind Blog",         "https://deepmind.google/blog/rss.xml"),
    ("NVIDIA Developer Blog", "https://developer.nvidia.com/blog/feed/"),
    ("Hugging Face Blog",     "https://huggingface.co/blog/feed.xml"),
    ("MarkTechPost",          "https://www.marktechpost.com/feed/"),

    # ---- React & frontend / creative coding ----
    ("This Week in React",   "https://thisweekinreact.com/newsletter/rss.xml"),
    ("LogRocket Blog",       "https://blog.logrocket.com/feed/"),
    ("Codrops",              "https://tympanus.net/codrops/feed/"),
    ("r/reactjs",            "https://www.reddit.com/r/reactjs.rss"),
    ("r/threejs",            "https://www.reddit.com/r/threejs.rss"),

    # ---- Broader engineering & tech news ----
    ("TechCrunch",           "https://techcrunch.com/feed/"),
    ("The Verge",            "https://www.theverge.com/rss/index.xml"),
    ("Ars Technica",         "https://feeds.arstechnica.com/arstechnica/index"),
    ("Meta Engineering",     "https://engineering.fb.com/feed/"),
    ("Netflix Tech Blog",    "https://netflixtechblog.com/feed"),
    ("Spotify Engineering",  "https://engineering.atspotify.com/feed/"),
]


def main(poll: bool = True) -> int:
    cfg = load_config()
    init_db()

    print(f"Seeding {len(FEEDS)} feeds...\n")
    total_new_events = 0
    failed: list[str] = []
    for name, url in FEEDS:
        try:
            row = upsert_repo(
                name=name,
                type_="rss",
                path_or_url=url,
                branch="",
                enabled=True,
            )
        except Exception as e:
            print(f"  X  {name:30s} upsert failed: {e}")
            failed.append(name)
            continue

        if not poll:
            print(f"  +  {name:30s} saved (skip-poll)")
            continue

        try:
            inserted = poll_one_repo(cfg, dict(row))
        except Exception as e:
            print(f"  !  {name:30s} poll failed: {e}")
            failed.append(name)
            continue

        marker = "ok" if inserted >= 0 else "--"
        print(f"  {marker}  {name:30s} +{inserted} article(s)")
        total_new_events += inserted

    print()
    print(f"Done. {total_new_events} new articles ingested across {len(FEEDS) - len(failed)}/{len(FEEDS)} feeds.")
    if failed:
        print("Failed:", ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-poll", action="store_true", help="upsert only, don't poll each feed")
    args = ap.parse_args()
    raise SystemExit(main(poll=not args.no_poll))
