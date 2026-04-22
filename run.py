"""Entry point — starts scheduler + uvicorn."""
from __future__ import annotations

import logging
import sys

import uvicorn

from agent.config import load_config
from agent.db import init_db
from agent.scheduler import start_scheduler
from agent.web.app import create_app


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("linkedin-agent")

    cfg = load_config()
    init_db()

    for r in cfg.repos:
        from agent.db import upsert_repo
        upsert_repo(
            name=r.name,
            type_=r.type,
            path_or_url=r.path_or_url(),
            branch=r.branch,
            enabled=r.enabled,
        )

    scheduler = start_scheduler(cfg)
    app = create_app(cfg)
    log.info("dashboard at http://%s:%d", cfg.server.host, cfg.server.port)

    try:
        uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="info")
    finally:
        scheduler.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
