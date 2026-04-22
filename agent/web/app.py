"""FastAPI routes for the dashboard."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agent.config import Config
from agent import db
from agent.learner import angle_weights

log = logging.getLogger(__name__)

_HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))


ANGLE_LABELS: dict[str, dict[str, str]] = {
    "technical_peer": {
        "label": "Technical Peer",
        "audience": "senior engineers / technical leaders",
    },
    "decision_maker": {
        "label": "Decision Maker",
        "audience": "founders, CTOs, hiring managers",
    },
    "mixed_story": {
        "label": "Mixed Story",
        "audience": "both — a narrative with one technical detail",
    },
}


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="linkedin-agent", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    def queue_page(request: Request):
        groups = db.pending_groups()
        return TEMPLATES.TemplateResponse(
            "queue.html",
            {
                "request": request,
                "groups": groups,
                "angle_labels": ANGLE_LABELS,
                "active": "queue",
            },
        )

    @app.post("/posts/{post_id}/mark_posted")
    def mark_posted(post_id: int, edited_content: str = Form("")):
        db.mark_post_posted(post_id, edited_content.strip() or None)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/posts/{post_id}/reject")
    def reject_post(post_id: int):
        db.mark_post_rejected(post_id)
        return RedirectResponse(url="/", status_code=303)

    @app.get("/images/{post_id}")
    def serve_image(post_id: int):
        post = db.get_post(post_id)
        if not post or not post.get("image_path"):
            raise HTTPException(status_code=404, detail="no image for post")
        path = Path(post["image_path"])
        if not path.exists():
            raise HTTPException(status_code=404, detail="image file missing")
        return FileResponse(path, media_type="image/png")

    @app.post("/posts/{post_id}/regenerate_image")
    def regenerate_image(post_id: int):
        from agent.generator import grok_images
        post = db.get_post(post_id)
        if not post:
            raise HTTPException(status_code=404, detail="post not found")
        content = post.get("edited_content") or post.get("content") or ""
        path = grok_images.generate_image_for_post(
            cfg=cfg,
            post_id=post_id,
            hook=post.get("hook") or "",
            content=content,
        )
        if path:
            db.set_post_image_path(post_id, path)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/actions/poll_now")
    def poll_now():
        from agent.scheduler import poll_repos_job
        try:
            poll_repos_job(cfg)
        except Exception:
            log.exception("poll_now failed")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/actions/generate_now")
    def generate_now():
        from agent.scheduler import generate_variants_job
        try:
            generate_variants_job(cfg)
        except Exception:
            log.exception("generate_now failed")
        return RedirectResponse(url="/", status_code=303)

    @app.get("/history", response_class=HTMLResponse)
    def history_page(request: Request):
        rows = db.recent_history(limit=100)
        for r in rows:
            try:
                r["source_event_ids_list"] = json.loads(r.get("source_event_ids") or "[]")
            except Exception:
                r["source_event_ids_list"] = []
        return TEMPLATES.TemplateResponse(
            "history.html",
            {
                "request": request,
                "rows": rows,
                "angle_labels": ANGLE_LABELS,
                "active": "history",
            },
        )

    @app.post("/engagement/{post_id}")
    def save_engagement(
        post_id: int,
        impressions: int = Form(0),
        likes: int = Form(0),
        comments: int = Form(0),
        reshares: int = Form(0),
        profile_visits: int = Form(0),
        followers_delta: int = Form(0),
    ):
        db.upsert_engagement(
            post_id=post_id,
            impressions=impressions,
            likes=likes,
            comments=comments,
            reshares=reshares,
            profile_visits=profile_visits,
            followers_delta=followers_delta,
        )
        return RedirectResponse(url="/history", status_code=303)

    @app.get("/repos", response_class=HTMLResponse)
    def repos_page(request: Request):
        repos = db.list_repos(enabled_only=False)
        return TEMPLATES.TemplateResponse(
            "repos.html",
            {
                "request": request,
                "repos": repos,
                "active": "repos",
            },
        )

    @app.post("/repos")
    def repos_upsert(
        name: str = Form(...),
        type: str = Form(...),
        path_or_url: str = Form(...),
        branch: str = Form("main"),
        enabled: Optional[str] = Form(None),
    ):
        db.upsert_repo(
            name=name.strip(),
            type_=type.strip(),
            path_or_url=path_or_url.strip(),
            branch=branch.strip() or "main",
            enabled=bool(enabled),
        )
        return RedirectResponse(url="/repos", status_code=303)

    @app.post("/repos/{repo_id}/toggle")
    def repos_toggle(repo_id: int, enabled: Optional[str] = Form(None)):
        db.set_repo_enabled(repo_id, bool(enabled))
        return RedirectResponse(url="/repos", status_code=303)

    @app.get("/stats", response_class=HTMLResponse)
    def stats_page(request: Request):
        rows = db.engagement_by_angle()
        stats = angle_weights(rows)
        return TEMPLATES.TemplateResponse(
            "stats.html",
            {
                "request": request,
                "stats": stats,
                "angle_labels": ANGLE_LABELS,
                "active": "stats",
            },
        )

    return app
