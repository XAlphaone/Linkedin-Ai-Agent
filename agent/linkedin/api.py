"""LinkedIn OAuth + REST API wrapper (Phase 1: authenticate + publish text).

Scopes requested: openid profile email w_member_social
  - openid/profile/email → fetch the member URN via /v2/userinfo
  - w_member_social      → create posts (and, in Phase 3, likes and comments)

Uses the newer versioned /rest/* API (LinkedIn-Version + X-Restli-Protocol-Version).
"""
from __future__ import annotations

import logging
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
POSTS_URL = "https://api.linkedin.com/rest/posts"
IMAGES_INIT_URL = "https://api.linkedin.com/rest/images?action=initializeUpload"

SCOPES = "openid profile email w_member_social"
# LinkedIn rotates API versions monthly and supports each for at least 12 months.
# Check https://learn.microsoft.com/en-us/linkedin/marketing/versioning and bump
# this once a year. Using a retired version returns HTTP 426 NONEXISTENT_VERSION.
LINKEDIN_API_VERSION = "202604"
RESTLI_VERSION = "2.0.0"

# Short-lived CSRF state store (single-user localhost; in-process is fine).
# { state_value: expiry_epoch_seconds }
_STATE_STORE: dict[str, float] = {}
_STATE_TTL_SECONDS = 600


def _prune_states() -> None:
    now = time.time()
    for k in list(_STATE_STORE.keys()):
        if _STATE_STORE[k] < now:
            del _STATE_STORE[k]


def new_state() -> str:
    _prune_states()
    state = secrets.token_urlsafe(24)
    _STATE_STORE[state] = time.time() + _STATE_TTL_SECONDS
    return state


def consume_state(state: str) -> bool:
    _prune_states()
    if state in _STATE_STORE:
        del _STATE_STORE[state]
        return True
    return False


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    qs = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": SCOPES,
        }
    )
    return f"{AUTHORIZE_URL}?{qs}"


def exchange_code(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict:
    """Exchange an authorization code for an access token.

    Returns the raw token payload: {access_token, expires_in, scope, ...}.
    Raises requests.HTTPError on failure.
    """
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_userinfo(access_token: str) -> dict:
    """GET /v2/userinfo — returns {sub, name, given_name, family_name, email, picture, ...}."""
    resp = requests.get(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def urn_from_sub(sub: str) -> str:
    """LinkedIn userinfo 'sub' is the member id; the URN prepends urn:li:person:."""
    return f"urn:li:person:{sub}"


def expires_at_from_expires_in(expires_in: int) -> str:
    """Compute an absolute expiry as ISO-8601 UTC from a token's lifetime (seconds)."""
    return (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()


def _auth_headers(access_token: str, json_body: bool = True) -> dict:
    h = {
        "Authorization": f"Bearer {access_token}",
        "LinkedIn-Version": LINKEDIN_API_VERSION,
        "X-Restli-Protocol-Version": RESTLI_VERSION,
    }
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _extract_post_urn(resp: requests.Response) -> Optional[str]:
    """LinkedIn's /rest/posts returns the new URN in x-restli-id; be forgiving
    about header casing and fall back to the Location header."""
    urn = resp.headers.get("x-restli-id") or resp.headers.get("X-RestLi-Id")
    if not urn:
        location = resp.headers.get("Location", "")
        if location.startswith("urn:"):
            urn = location
    if not urn:
        log.warning("could not find post URN in response headers: %s", dict(resp.headers))
    return urn


def create_text_post(
    access_token: str,
    member_urn: str,
    text: str,
) -> Optional[str]:
    """Create a member feed post with no media. Returns the post URN on success."""
    body = {
        "author": member_urn,
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    resp = requests.post(POSTS_URL, headers=_auth_headers(access_token), json=body, timeout=30)
    if resp.status_code >= 400:
        log.error(
            "create_text_post failed: HTTP %s body=%s",
            resp.status_code,
            resp.text[:500],
        )
        resp.raise_for_status()
    return _extract_post_urn(resp)


def initialize_image_upload(access_token: str, member_urn: str) -> tuple[str, str]:
    """Step 1 of LinkedIn image flow: register an upload.

    Returns (upload_url, image_urn). Raises on failure.
    """
    body = {"initializeUploadRequest": {"owner": member_urn}}
    resp = requests.post(IMAGES_INIT_URL, headers=_auth_headers(access_token), json=body, timeout=30)
    if resp.status_code >= 400:
        log.error(
            "initialize_image_upload failed: HTTP %s body=%s",
            resp.status_code,
            resp.text[:500],
        )
        resp.raise_for_status()
    data = resp.json().get("value") or {}
    upload_url = data.get("uploadUrl")
    image_urn = data.get("image")
    if not upload_url or not image_urn:
        raise RuntimeError(f"unexpected initializeUpload response: {resp.text[:500]}")
    return upload_url, image_urn


def upload_image_bytes(access_token: str, upload_url: str, image_bytes: bytes) -> None:
    """Step 2 of LinkedIn image flow: PUT the raw bytes to the registered URL."""
    resp = requests.put(
        upload_url,
        data=image_bytes,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=60,
    )
    if resp.status_code >= 400:
        log.error(
            "upload_image_bytes failed: HTTP %s body=%s",
            resp.status_code,
            resp.text[:500],
        )
        resp.raise_for_status()


def create_image_post(
    access_token: str,
    member_urn: str,
    text: str,
    image_urn: str,
    alt_text: Optional[str] = None,
) -> Optional[str]:
    """Step 3 of LinkedIn image flow: create a member post that references the
    already-uploaded image URN. Returns the post URN on success."""
    media: dict = {"id": image_urn}
    if alt_text:
        media["altText"] = alt_text[:4086]

    body = {
        "author": member_urn,
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "content": {"media": media},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    resp = requests.post(POSTS_URL, headers=_auth_headers(access_token), json=body, timeout=30)
    if resp.status_code >= 400:
        log.error(
            "create_image_post failed: HTTP %s body=%s",
            resp.status_code,
            resp.text[:500],
        )
        resp.raise_for_status()
    return _extract_post_urn(resp)


def publish_post_with_optional_image(
    access_token: str,
    member_urn: str,
    text: str,
    image_path: Optional[str],
    alt_text: Optional[str] = None,
) -> Optional[str]:
    """If image_path is an existing file, run the 3-step image flow. Otherwise
    fall back to a plain text post. Returns the post URN."""
    if image_path:
        from pathlib import Path
        p = Path(image_path)
        if p.exists() and p.is_file():
            log.info("uploading image for post (size=%d): %s", p.stat().st_size, p)
            upload_url, image_urn = initialize_image_upload(access_token, member_urn)
            upload_image_bytes(access_token, upload_url, p.read_bytes())
            return create_image_post(access_token, member_urn, text, image_urn, alt_text)
        log.warning("image_path %s not found; falling back to text-only post", image_path)
    return create_text_post(access_token, member_urn, text)
