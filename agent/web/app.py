"""FastAPI routes for the dashboard."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
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

    # Single-user localhost app. One generation at a time is fine; enforce it
    # so parallel clicks don't hammer the xAI API and DB writes don't race.
    _gen_lock = threading.Lock()
    _job_state: dict = {"running": False, "kind": None, "started_at": None, "detail": None}

    def _set_job(kind: Optional[str], detail: Optional[str] = None) -> None:
        _job_state["running"] = kind is not None
        _job_state["kind"] = kind
        _job_state["detail"] = detail
        _job_state["started_at"] = time.time() if kind else None

    def _run_generate(events: Optional[list], detail: str) -> None:
        """Background wrapper around generate_variants_job with locking + job tracking."""
        from agent.scheduler import generate_variants_job
        if not _gen_lock.acquire(blocking=False):
            log.info("generation already running; ignoring concurrent trigger")
            return
        try:
            _set_job("generating", detail)
            generate_variants_job(cfg, events=events)
        except Exception:
            log.exception("background generate failed")
        finally:
            _set_job(None)
            _gen_lock.release()

    def _run_regen_image(post_id: int) -> None:
        from agent.generator import grok_images
        if not _gen_lock.acquire(blocking=False):
            log.info("generation already running; ignoring regen image request")
            return
        try:
            _set_job("regen_image", f"post {post_id}")
            post = db.get_post(post_id)
            if not post:
                return
            content = post.get("edited_content") or post.get("content") or ""
            path = grok_images.generate_image_for_post(
                cfg=cfg,
                post_id=post_id,
                hook=post.get("hook") or "",
                content=content,
            )
            if path:
                db.set_post_image_path(post_id, path)
        except Exception:
            log.exception("background regen_image failed for post %d", post_id)
        finally:
            _set_job(None)
            _gen_lock.release()

    @app.get("/", response_class=HTMLResponse)
    def queue_page(request: Request):
        groups = db.pending_groups()
        linkedin_connected = db.get_linkedin_auth() is not None
        job = dict(_job_state)
        if job.get("started_at"):
            job["elapsed_s"] = int(time.time() - job["started_at"])
        return TEMPLATES.TemplateResponse(
            "queue.html",
            {
                "request": request,
                "groups": groups,
                "angle_labels": ANGLE_LABELS,
                "linkedin_connected": linkedin_connected,
                "job": job,
                "active": "queue",
            },
        )

    @app.get("/api/job_status")
    def job_status():
        """Polled from the queue banner's JS to auto-refresh when done."""
        snap = dict(_job_state)
        if snap.get("started_at"):
            snap["elapsed_s"] = int(time.time() - snap["started_at"])
        return snap

    @app.post("/posts/{post_id}/mark_posted")
    def mark_posted(post_id: int, edited_content: str = Form("")):
        db.mark_post_posted(post_id, edited_content.strip() or None)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/posts/{post_id}/reject")
    def reject_post(post_id: int):
        db.mark_post_rejected(post_id)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/posts/{post_id}/publish")
    def publish_post(post_id: int, edited_content: str = Form("")):
        from agent.linkedin import api as li_api

        post = db.get_post(post_id)
        if not post:
            raise HTTPException(status_code=404, detail="post not found")
        auth = db.get_linkedin_auth()
        if not auth:
            raise HTTPException(status_code=400, detail="LinkedIn not connected")

        text = (edited_content.strip() or post.get("content") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="post has no content")
        try:
            urn = li_api.publish_post_with_optional_image(
                access_token=auth["access_token"],
                member_urn=auth["member_urn"],
                text=text,
                image_path=post.get("image_path"),
                alt_text=post.get("hook") or None,
            )
        except Exception as e:
            log.exception("LinkedIn publish failed for post %d", post_id)
            # Surface the API error body if we have it — much more useful than
            # redirecting the user to server.log.
            detail = f"LinkedIn publish failed: {e}"
            resp = getattr(e, "response", None)
            if resp is not None:
                detail = f"LinkedIn publish failed (HTTP {resp.status_code}): {resp.text[:500]}"
            raise HTTPException(status_code=502, detail=detail)

        if urn:
            db.set_post_linkedin_urn(post_id, urn)
        db.mark_post_posted(post_id, edited_content.strip() or None)
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
    def regenerate_image(post_id: int, background_tasks: BackgroundTasks):
        post = db.get_post(post_id)
        if not post:
            raise HTTPException(status_code=404, detail="post not found")
        background_tasks.add_task(_run_regen_image, post_id)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/actions/poll_now")
    def poll_now():
        # Poll is fast (~5s), leave synchronous.
        from agent.scheduler import poll_repos_job
        try:
            poll_repos_job(cfg)
        except Exception:
            log.exception("poll_now failed")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/actions/generate_now")
    def generate_now(background_tasks: BackgroundTasks):
        # Runs in background so the browser isn't blocked for 2-3 minutes
        # while Grok drafts 3 posts + 3 images.
        background_tasks.add_task(_run_generate, None, "all unprocessed")
        return RedirectResponse(url="/", status_code=303)

    @app.get("/events", response_class=HTMLResponse)
    def events_page(request: Request):
        groups = db.unprocessed_events_by_repo()
        total = sum(len(g["events"]) for g in groups)
        return TEMPLATES.TemplateResponse(
            "events.html",
            {
                "request": request,
                "groups": groups,
                "total": total,
                "active": "events",
            },
        )

    @app.post("/actions/generate_from_selected")
    async def generate_from_selected(request: Request, background_tasks: BackgroundTasks):
        """Generate 3 variants from only the explicitly selected events.
        Runs in background — the response redirects immediately to /."""
        from agent.db import events_by_ids

        form = await request.form()
        raw_ids = form.getlist("event_ids")
        ids: list[int] = []
        for v in raw_ids:
            try:
                ids.append(int(v))
            except (TypeError, ValueError):
                continue
        if not ids:
            return RedirectResponse(url="/events", status_code=303)

        events = events_by_ids(ids)
        if not events:
            return RedirectResponse(url="/events", status_code=303)

        detail = f"{len(events)} selected event(s)"
        background_tasks.add_task(_run_generate, events, detail)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/actions/skip_events")
    async def skip_events(request: Request):
        """Mark selected events as processed without generating — for events
        you'd rather not post about."""
        form = await request.form()
        raw_ids = form.getlist("event_ids")
        ids: list[int] = []
        for v in raw_ids:
            try:
                ids.append(int(v))
            except (TypeError, ValueError):
                continue
        if ids:
            db.mark_events_processed(ids)
        return RedirectResponse(url="/events", status_code=303)

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
        background_tasks: BackgroundTasks,
        name: str = Form(...),
        type: str = Form(...),
        path_or_url: str = Form(...),
        branch: str = Form("main"),
        enabled: Optional[str] = Form(None),
    ):
        row = db.upsert_repo(
            name=name.strip(),
            type_=type.strip(),
            path_or_url=path_or_url.strip(),
            branch=branch.strip() or "main",
            enabled=bool(enabled),
        )
        if row and row.get("enabled"):
            from agent.scheduler import poll_one_repo
            background_tasks.add_task(poll_one_repo, cfg, row)
        return RedirectResponse(url="/repos", status_code=303)

    @app.post("/repos/{repo_id}/toggle")
    def repos_toggle(
        repo_id: int,
        background_tasks: BackgroundTasks,
        enabled: Optional[str] = Form(None),
    ):
        new_enabled = bool(enabled)
        db.set_repo_enabled(repo_id, new_enabled)
        # Only poll on the enable transition — disabling is a no-op for data.
        if new_enabled:
            with db.connect() as conn:
                row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
            if row:
                from agent.scheduler import poll_one_repo
                background_tasks.add_task(poll_one_repo, cfg, row)
        return RedirectResponse(url="/repos", status_code=303)

    @app.get("/auth/linkedin/start")
    def linkedin_start():
        from agent.linkedin import api as li_api

        if not cfg.linkedin_client_id or not cfg.linkedin_client_secret:
            raise HTTPException(
                status_code=400,
                detail="LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET not set in .env",
            )
        state = li_api.new_state()
        url = li_api.build_authorize_url(
            client_id=cfg.linkedin_client_id,
            redirect_uri=cfg.linkedin_redirect_uri,
            state=state,
        )
        return RedirectResponse(url=url, status_code=302)

    @app.get("/auth/linkedin/callback")
    def linkedin_callback(
        code: Optional[str] = None,
        state: Optional[str] = None,
        error: Optional[str] = None,
        error_description: Optional[str] = None,
    ):
        from agent.linkedin import api as li_api

        if error:
            log.error("LinkedIn OAuth error: %s — %s", error, error_description)
            raise HTTPException(status_code=400, detail=f"LinkedIn error: {error} — {error_description}")
        if not code or not state:
            raise HTTPException(status_code=400, detail="missing code or state")
        if not li_api.consume_state(state):
            raise HTTPException(status_code=400, detail="invalid or expired state (possible CSRF)")

        try:
            token_payload = li_api.exchange_code(
                code=code,
                client_id=cfg.linkedin_client_id,
                client_secret=cfg.linkedin_client_secret,
                redirect_uri=cfg.linkedin_redirect_uri,
            )
        except Exception:
            log.exception("token exchange failed")
            raise HTTPException(status_code=502, detail="token exchange failed — see server.log")

        access_token = token_payload.get("access_token")
        expires_in = int(token_payload.get("expires_in", 0))
        scopes = token_payload.get("scope", "")
        if not access_token:
            raise HTTPException(status_code=502, detail="no access_token in LinkedIn response")

        try:
            info = li_api.get_userinfo(access_token)
        except Exception:
            log.exception("userinfo fetch failed")
            raise HTTPException(status_code=502, detail="userinfo fetch failed — see server.log")

        sub = info.get("sub")
        if not sub:
            raise HTTPException(status_code=502, detail="LinkedIn userinfo missing 'sub'")

        db.save_linkedin_auth(
            member_urn=li_api.urn_from_sub(sub),
            member_name=info.get("name") or "",
            access_token=access_token,
            expires_at=li_api.expires_at_from_expires_in(expires_in),
            scopes=scopes,
        )
        log.info("LinkedIn connected: %s (expires in %ds)", info.get("name"), expires_in)
        return RedirectResponse(url="/settings", status_code=303)

    @app.post("/auth/linkedin/disconnect")
    def linkedin_disconnect():
        db.clear_linkedin_auth()
        return RedirectResponse(url="/settings", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        auth = db.get_linkedin_auth()
        configured = bool(cfg.linkedin_client_id and cfg.linkedin_client_secret)
        return TEMPLATES.TemplateResponse(
            "settings.html",
            {
                "request": request,
                "auth": auth,
                "configured": configured,
                "redirect_uri": cfg.linkedin_redirect_uri,
                "active": "settings",
            },
        )

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
