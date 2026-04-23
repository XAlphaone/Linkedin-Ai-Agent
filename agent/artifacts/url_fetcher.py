"""Fetch a URL and extract its main text content.

No heavy deps — requests + beautifulsoup4. Extraction is deliberately simple:
strip scripts/styles/nav/footer/aside/form tags, then take <article> or <main>
if present, otherwise the document body. Handles 95% of blogs, docs, and news
sites well enough to hand off to the generator. For edge cases (SPA-rendered,
paywalled) the extraction just comes back thinner; the generator still has the
note + URL to work with.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import requests

log = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 15
MAX_EXTRACTED_CHARS = 8000
USER_AGENT = (
    "Mozilla/5.0 (compatible; linkedin-agent/0.1; "
    "+https://github.com/XAlphaone/Linkedin-Ai-Agent)"
)


def fetch_and_extract(url: str) -> dict:
    """Return {title, text, error}. `error` is None on success, a message on failure.
    `text` is truncated to MAX_EXTRACTED_CHARS."""
    if not url or not url.strip():
        return {"title": "", "text": "", "error": "empty url"}

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            timeout=FETCH_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as e:
        log.warning("fetch failed for %s: %s", url, e)
        return {"title": "", "text": "", "error": f"fetch failed: {e}"}

    ctype = resp.headers.get("Content-Type", "")
    if "html" not in ctype.lower() and "xml" not in ctype.lower():
        # Plain text / markdown / JSON — still useful, just skip BS4
        text = _normalize(resp.text)[:MAX_EXTRACTED_CHARS]
        return {"title": "", "text": text, "error": None}

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {"title": "", "text": "", "error": "beautifulsoup4 not installed"}

    soup = BeautifulSoup(resp.text, "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()[:300]
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            title = og["content"].strip()[:300]

    # Remove boilerplate / non-content elements
    for tag in soup(["script", "style", "noscript", "nav", "footer",
                     "header", "aside", "form", "iframe", "svg", "button"]):
        tag.decompose()

    # Prefer <article> or <main>; fall back to body
    main = soup.find("article") or soup.find("main") or soup.body or soup

    text = main.get_text(separator=" ", strip=True)
    text = _normalize(text)[:MAX_EXTRACTED_CHARS]

    if not text:
        return {"title": title, "text": "", "error": "extraction yielded empty text"}

    return {"title": title, "text": text, "error": None}


def _normalize(s: Optional[str]) -> str:
    if not s:
        return ""
    # Collapse whitespace but keep paragraph breaks (blank lines).
    s = re.sub(r"\r\n?", "\n", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()
