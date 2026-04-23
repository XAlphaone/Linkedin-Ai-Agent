"""Send an uploaded image to xAI's vision-capable reasoning model and get
a factual description back. That description becomes part of the synthetic
event the generator sees when drafting posts.

We deliberately ask for a description — NOT a draft post. The post-drafting
happens downstream in the normal generate_variants pipeline, so the voice
guide still applies. This step is only about turning pixels into text the
text generator can reason about.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Optional

from agent.config import Config

log = logging.getLogger(__name__)

XAI_BASE_URL = "https://api.x.ai/v1"
TIMEOUT_SECONDS = 300.0

DESCRIBE_SYSTEM = """\
You describe images for a software engineer who will use your description as \
the factual basis of a LinkedIn post. You are a reporter, not an author.

RULES
- Output ONE paragraph of 80-180 words.
- Be specific about what the image shows: subject, scene, any visible text, \
data points on charts/graphs, UI elements, architecture diagram components, \
code content, faces or logos if prominent.
- If there's a chart or graph: identify the axes, what's being measured, the \
rough shape of the trend, any notable values or outliers.
- If there's code: name the language if identifiable, what the code appears \
to do, any notable patterns.
- If there's an architecture diagram: name the components and the data/flow \
relationships between them.
- Don't draft a post, don't speculate about meaning, don't use promotional \
language. Be a camera.
- No preamble. Start with the subject directly.\
"""

DESCRIBE_USER = "Describe this image following the rules above."


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "image/png")


def describe_image(cfg: Config, image_path: Path) -> Optional[str]:
    """Return the model's factual description, or None on any failure."""
    if not image_path or not image_path.exists():
        return None
    if not cfg.xai_api_key:
        log.error("XAI_API_KEY not set; can't describe image")
        return None

    try:
        from openai import OpenAI
    except ImportError:
        log.error("openai SDK not installed")
        return None

    try:
        raw = image_path.read_bytes()
    except Exception:
        log.exception("couldn't read image at %s", image_path)
        return None

    b64 = base64.b64encode(raw).decode("ascii")
    mime = _guess_mime(image_path)
    data_uri = f"data:{mime};base64,{b64}"

    client = OpenAI(
        api_key=cfg.xai_api_key,
        base_url=XAI_BASE_URL,
        timeout=TIMEOUT_SECONDS,
    )
    try:
        resp = client.chat.completions.create(
            model=cfg.generation.model,
            max_completion_tokens=500,
            messages=[
                {"role": "system", "content": DESCRIBE_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": DESCRIBE_USER},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                },
            ],
        )
    except Exception:
        log.exception("image description call failed for %s", image_path)
        return None

    if not resp.choices:
        return None
    out = (resp.choices[0].message.content or "").strip()
    log.info("image described (%d chars) for %s", len(out), image_path.name)
    return out or None
