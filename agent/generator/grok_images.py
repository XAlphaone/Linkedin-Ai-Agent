"""xAI Grok image generator — one image per post variant.

TWO-PASS DESIGN:
  Pass 1 — a Grok reasoning model reads the full post and drafts a concrete,
           photographable image brief (lens, lighting, subject, action).
  Pass 2 — grok-imagine-image(-pro) renders that brief.

Uses the OpenAI Python SDK pointed at xAI. Images come back as b64_json and are
written to data/images/post_<id>.png. URLs from the API are temporary, so we
always download and the dashboard serves from disk.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Optional

from agent.config import Config

log = logging.getLogger(__name__)

XAI_BASE_URL = "https://api.x.ai/v1"
IMAGE_DIR = Path("data/images")
IMAGE_TIMEOUT_SECONDS = 300.0
BRIEF_TIMEOUT_SECONDS = 600.0


BRIEF_SYSTEM_PROMPT = """\
You write image briefs for LinkedIn post thumbnails. Given a post, output ONE \
image brief that, when handed to an image model, produces a photographic or \
cinematic image that (a) visually pertains to what the post is actually about, \
(b) looks professional and modern, and (c) earns the scroll on a LinkedIn feed.

RULES
- Output ONE paragraph of 80-160 words. No preamble, no lists, no meta commentary.
- Describe a real, concrete scene — a specific subject, place, action, and moment. \
Not a metaphor, not a mood, not an abstract concept.
- Include a camera/style spec: lens (e.g. "35mm"), depth of field, lighting \
(e.g. "warm afternoon key light, soft fill"), time of day, texture.
- Photorealistic by default. If the post is explicitly about art, animation, \
or visualization, choose ONE named visual style ("cinematic VFX concept render", \
"Pixar-style 3D frame", "pencil-and-watercolor board panel") and commit to it.
- For posts about code, software, or abstract systems: choose ONE grounded physical \
scene the post could credibly illustrate. Examples: a dev workstation at 2am with \
one glowing monitor showing a waveform; a trading-floor terminal with order books; \
a datacenter aisle with an amber service light; a whiteboard covered in architecture \
diagrams and coffee rings; a mechanical watch face with exposed gears. Pick specifics.
- Avoid stock-photo cliches: handshakes, lightbulbs, cartoon brains, rockets, \
arrows going up, people in suits pointing at charts, floating holograms.
- No text, logos, UI chrome, brand marks, or watermarks in the scene. Instruct \
explicitly: "no text or logos in the image".
- No people unless the post genuinely centers on one. If a person appears, they are \
specific (role, clothing, action, not "a professional").
- Target 2:1 aspect ratio suitable for LinkedIn feed.

Your entire response is the brief itself, in plain prose. Start with the subject.\
"""


def _draft_image_brief(
    cfg: Config,
    hook: str,
    content: str,
) -> Optional[str]:
    """Use the text reasoning model to write a concrete image brief."""
    try:
        from openai import OpenAI
    except ImportError:
        log.error("openai SDK not installed")
        return None

    client = OpenAI(
        api_key=cfg.xai_api_key,
        base_url=XAI_BASE_URL,
        timeout=BRIEF_TIMEOUT_SECONDS,
    )
    user_msg = (
        f"Post hook: {hook.strip()}\n\n"
        f"Post body:\n{content.strip()}\n\n"
        "Write the image brief."
    )
    try:
        resp = client.chat.completions.create(
            model=cfg.generation.model,
            max_completion_tokens=400,
            messages=[
                {"role": "system", "content": BRIEF_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        )
    except Exception:
        log.exception("image brief draft failed")
        return None

    if not resp.choices:
        return None
    brief = (resp.choices[0].message.content or "").strip()
    if not brief:
        return None
    return brief


def _call_xai_image(
    client,
    model: str,
    prompt: str,
    aspect_ratio: str,
    resolution: str,
) -> Optional[bytes]:
    """Make a single image call. Returns raw image bytes, or None on failure."""
    try:
        response = client.images.generate(
            model=model,
            prompt=prompt,
            response_format="b64_json",
            extra_body={
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
            },
        )
    except Exception:
        log.exception("xAI images.generate failed")
        return None

    if not response.data:
        log.warning("xAI images.generate returned no data")
        return None

    b64 = response.data[0].b64_json
    if not b64:
        log.warning("xAI images.generate returned empty b64_json")
        return None
    try:
        return base64.b64decode(b64)
    except Exception:
        log.exception("failed to decode b64_json image")
        return None


def generate_image_for_post(
    cfg: Config,
    post_id: int,
    hook: str,
    content: str,
) -> Optional[str]:
    """Generate and save one image for the given post. Returns relative image path
    (e.g. 'data/images/post_42.png') on success, or None on any failure.
    """
    if not cfg.generation.generate_images:
        return None
    if not cfg.xai_api_key:
        log.error("XAI_API_KEY not set; skipping image for post %d", post_id)
        return None

    try:
        from openai import OpenAI
    except ImportError:
        log.error("openai SDK not installed; run: pip install -r requirements.txt")
        return None

    # Pass 1: reasoning model drafts a concrete, on-topic image brief.
    brief = _draft_image_brief(cfg, hook, content)
    if brief:
        log.info("image brief for post %d (%d chars): %s", post_id, len(brief), brief[:200])
    else:
        log.warning("image brief draft failed for post %d; skipping image", post_id)
        return None

    # Pass 2: render the brief.
    client = OpenAI(
        api_key=cfg.xai_api_key,
        base_url=XAI_BASE_URL,
        timeout=IMAGE_TIMEOUT_SECONDS,
    )
    image_bytes = _call_xai_image(
        client=client,
        model=cfg.generation.image_model,
        prompt=brief,
        aspect_ratio=cfg.generation.image_aspect_ratio,
        resolution=cfg.generation.image_resolution,
    )
    if image_bytes is None:
        return None

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    rel_path = IMAGE_DIR / f"post_{post_id}.png"
    try:
        rel_path.write_bytes(image_bytes)
    except Exception:
        log.exception("failed to write image for post %d", post_id)
        return None

    log.info("image saved for post %d: %s (%d bytes)", post_id, rel_path, len(image_bytes))
    return str(rel_path).replace("\\", "/")
