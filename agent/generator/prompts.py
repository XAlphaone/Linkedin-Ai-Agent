"""System prompt, angle specs, voice guide."""
from __future__ import annotations

from typing import Optional

from agent.config import Config

BANNED_PHRASES: list[str] = [
    "in today's rapidly evolving landscape",
    "i'm excited to announce",
    "thrilled to share",
    "game changer",
    "game-changer",
    "let's dive in",
    "at the end of the day",
]

BANNED_ENDINGS: list[str] = [
    "thoughts?",
]

BANNED_SENTENCE_STARTS: list[str] = [
    "remember:",
    "the truth is:",
]


ANGLES: dict[str, dict[str, str]] = {
    "technical_peer": {
        "audience": "senior engineers and technical leaders",
        "intent": (
            "Build credibility by exposing the real tradeoff behind a technical decision. "
            "Show the load-bearing constraint, not the marketing version."
        ),
        "length": "150-220 words",
    },
    "decision_maker": {
        "audience": "founders, CTOs, hiring managers",
        "intent": (
            "Frame the work around a business problem and its outcome. "
            "Name the time, cost, risk, or compliance lever the work pulled."
        ),
        "length": "120-180 words",
    },
    "mixed_story": {
        "audience": "both technical and non-technical readers",
        "intent": (
            "A short narrative arc with one legit technical detail a layperson can follow. "
            "Not a press release — a moment, a decision, a consequence."
        ),
        "length": "140-200 words",
    },
}


VOICE_GUIDE = """\
You write LinkedIn posts for a senior engineer. The goal is credibility and \
specificity, not reach-for-the-sake-of-it engagement.

RULES — follow all of these:
- First person. Direct. No throat-clearing.
- Mix short sentences with long. No rhythmic sameness. Avoid starting three sentences in a row with the same word.
- Concrete specifics beat abstractions: numbers, names, decisions made.
- At most one emoji, only as a structural marker. Usually zero.
- At most 2-3 hashtags, only if genuinely relevant. Usually zero.
- The hook (first line) earns the scroll — a concrete statement, not a vague setup.
- No "what do you think?" / "thoughts?" closers. The post ends when it ends.
- Do not announce the post. Do not summarize what you will say. Start with substance.

BANNED — rewrite if any of these appear:
- "In today's rapidly evolving landscape"
- "I'm excited to announce" / "Thrilled to share"
- "Game changer"
- "Let's dive in"
- "At the end of the day"
- Any post ending with "Thoughts?"
- Sentences beginning with "Remember:" or "The truth is:"
"""


def system_prompt(cfg: Config) -> str:
    """Default personal voice. First person singular."""
    a = cfg.author
    positioning = "\n".join(f"- {p}" for p in a.positioning) or "- (none provided)"
    avoid = "\n".join(f"- {t}" for t in a.avoid_topics) or "- (none)"
    return (
        f"You are drafting LinkedIn posts for {a.name}.\n"
        f"Headline: {a.headline}\n\n"
        f"Positioning (things this person actually does):\n{positioning}\n\n"
        f"Avoid these topics:\n{avoid}\n\n"
        f"{VOICE_GUIDE}"
    )


def brand_system_prompt(cfg: Config, brand_key: str) -> str:
    """Company / product voice. First person plural, more polished.

    Falls back to the personal system_prompt if brand_key isn't configured.
    """
    brand = cfg.brand_voices.get(brand_key)
    if not brand:
        return system_prompt(cfg)
    positioning = "\n".join(f"- {p}" for p in brand.positioning) or "- (none provided)"
    brand_rules = "\n".join(f"- {r}" for r in brand.voice_rules) or "- (none provided)"
    avoid = "\n".join(f"- {t}" for t in cfg.author.avoid_topics) or "- (none)"
    return (
        f"You are drafting LinkedIn posts on behalf of {brand.display_name}, "
        f"posted to its company page.\n"
        f"Company headline: {brand.headline or '(none)'}\n\n"
        f"Positioning (what this company actually does):\n{positioning}\n\n"
        f"Company voice rules (these are primary — follow these even if they "
        f"conflict with habits from a personal voice):\n{brand_rules}\n\n"
        f"Avoid these topics:\n{avoid}\n\n"
        f"{VOICE_GUIDE}"
    )


def system_prompt_for_target(cfg: Config, target: Optional[str]) -> str:
    """Dispatch helper used by the generator. 'personal' or None → personal voice;
    any other target looks up a brand_voices entry."""
    if not target or target == "personal":
        return system_prompt(cfg)
    return brand_system_prompt(cfg, target)


def _format_events(events: list[dict]) -> str:
    if not events:
        return (
            "No new events this cycle. Write a reflection-style post about an ongoing\n"
            "piece of work from the positioning above — a tradeoff, a decision, a \n"
            "lesson from the last week or two. Concrete specifics, not generalities."
        )
    lines: list[str] = []
    for e in events:
        when = (e.get("event_timestamp") or "")[:10]
        head = f"- [{e.get('repo_name', '?')}] {e.get('event_type')} · {when}: {e.get('title') or ''}".strip()
        lines.append(head)
        body = (e.get("body") or "").strip()
        if body:
            snippet = body[:400]
            lines.append(f"    {snippet}")
        files = e.get("files_changed") or []
        if files:
            lines.append(f"    files: {', '.join(files[:8])}" + (" …" if len(files) > 8 else ""))
    return "\n".join(lines)


def user_prompt_for_angle(angle: str, events: list[dict]) -> str:
    spec = ANGLES[angle]
    return (
        f"Draft ONE LinkedIn post for the '{angle}' angle.\n\n"
        f"Target audience: {spec['audience']}\n"
        f"Intent: {spec['intent']}\n"
        f"Length: {spec['length']}\n\n"
        f"Recent engineering events you can draw on:\n{_format_events(events)}\n\n"
        f"Output ONLY the post body — no preface, no commentary, no markdown fences.\n"
        f"The first line is the hook. Do not label it."
    )


def contains_banned(text: str) -> list[str]:
    """Return a list of banned phrase / rule names found. Empty list = clean."""
    lower = text.lower()
    hits: list[str] = []
    for phrase in BANNED_PHRASES:
        if phrase in lower:
            hits.append(f"phrase: {phrase!r}")

    stripped = lower.rstrip().rstrip(".!?")
    for ending in BANNED_ENDINGS:
        if stripped.endswith(ending.rstrip(".!?")):
            hits.append(f"ending: {ending!r}")

    # Check start-of-sentence patterns on any line
    for line in text.splitlines():
        line_l = line.strip().lower()
        for bad in BANNED_SENTENCE_STARTS:
            if line_l.startswith(bad):
                hits.append(f"sentence-start: {bad!r}")
    return hits
