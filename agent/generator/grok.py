"""xAI Grok client — generates 3 variants, one per angle.

Uses the OpenAI Python SDK pointed at xAI's OpenAI-compatible endpoint at
https://api.x.ai/v1. The only provider-specific thing here is the base URL
and the env var name.
"""
from __future__ import annotations

import logging
from typing import Optional

from agent.config import Config
from agent.generator import prompts

log = logging.getLogger(__name__)

XAI_BASE_URL = "https://api.x.ai/v1"
# Reasoning models can take many minutes: default OpenAI-SDK timeout isn't
# long enough. Set a generous ceiling per call. xAI's own async docs use 3600s.
REQUEST_TIMEOUT_SECONDS = 900.0
MAX_REGEN_ATTEMPTS = 2


def _extract_text(response) -> str:
    if not response.choices:
        return ""
    msg = response.choices[0].message
    return (msg.content or "").strip()


def _call_once(
    client,
    model: str,
    system_text: str,
    user_text: str,
) -> str:
    # max_completion_tokens (not the deprecated max_tokens) caps only the
    # visible output, not the reasoning trace. Exactly what we want for a
    # 150-220 word post.
    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=800,
        messages=[
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ],
    )
    return _extract_text(response)


def _generate_for_angle(
    client,
    model: str,
    system_text: str,
    angle: str,
    events: list[dict],
) -> Optional[dict]:
    user_text = prompts.user_prompt_for_angle(angle, events)
    text = ""
    hits: list[str] = []
    for attempt in range(MAX_REGEN_ATTEMPTS + 1):
        prompt_text = user_text
        if attempt > 0 and hits:
            prompt_text += (
                "\n\nIMPORTANT: your previous draft violated the voice guide. "
                "Rewrite and do NOT include any of these: "
                + "; ".join(hits)
                + ". Obey every rule in the system prompt."
            )
        try:
            text = _call_once(client, model, system_text, prompt_text)
        except Exception:
            log.exception("grok call failed for angle=%s attempt=%d", angle, attempt)
            return None
        if not text:
            continue
        hits = prompts.contains_banned(text)
        if not hits:
            break
        log.info("angle=%s attempt=%d banned-phrase hits=%s", angle, attempt, hits)

    if not text:
        return None
    if hits:
        log.warning("angle=%s: exhausted regen; keeping draft with hits=%s", angle, hits)

    hook = text.splitlines()[0].strip() if text else ""
    return {"angle": angle, "hook": hook, "content": text}


def generate_variants(
    cfg: Config,
    events: list[dict],
    target: str = "personal",
) -> list[dict]:
    """Generate one post per angle (three total). Returns [{angle, hook, content}, ...].

    `target` picks the voice: 'personal' uses the author section in config.yaml;
    any other slug looks up cfg.brand_voices[target] for the company voice.
    Falls back to personal voice silently if the target isn't configured.
    """
    if not cfg.xai_api_key:
        log.error("XAI_API_KEY not set; skipping generation")
        return []

    try:
        from openai import OpenAI
    except ImportError:
        log.error("openai SDK not installed; run: pip install -r requirements.txt")
        return []

    client = OpenAI(
        api_key=cfg.xai_api_key,
        base_url=XAI_BASE_URL,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    system_text = prompts.system_prompt_for_target(cfg, target)
    log.info("generating %d angles for target=%r", len(prompts.ANGLES), target)

    variants: list[dict] = []
    for angle in prompts.ANGLES:
        v = _generate_for_angle(client, cfg.generation.model, system_text, angle, events)
        if v is not None:
            variants.append(v)
    return variants
