"""Load and validate config.yaml + .env."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator


class Author(BaseModel):
    name: str
    headline: str
    positioning: list[str] = Field(default_factory=list)
    avoid_topics: list[str] = Field(default_factory=list)


class BrandVoice(BaseModel):
    """A company/product voice alternative to the default personal voice.

    `linkedin_org_urn` is filled in once the org OAuth flow discovers the
    target company page — Phase 3b uses it to route publish calls.
    """
    display_name: str
    headline: str = ""
    positioning: list[str] = Field(default_factory=list)
    voice_rules: list[str] = Field(default_factory=list)
    linkedin_org_urn: Optional[str] = None


class RepoConfig(BaseModel):
    name: str
    type: Literal["local", "github", "rss", "telemetry"]
    path: Optional[str] = None
    url: Optional[str] = None
    branch: str = "main"
    enabled: bool = True

    @field_validator("path")
    @classmethod
    def _path_for_local(cls, v, info):
        return v

    def path_or_url(self) -> str:
        return self.path if self.type == "local" else (self.url or "")


class RedditScan(BaseModel):
    """Config for the reddit opportunity scanner.
    subreddits: names without the r/ prefix. Posts from these are pulled.
    queries: Reddit full-text searches (run against all of reddit); useful
      for surfacing 'I wish there was an app that...' pain points across
      subreddits you didn't list.
    """
    enabled: bool = True
    scan_interval_hours: int = 6
    per_source_limit: int = 25
    subreddits: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)


class Generation(BaseModel):
    model: str = "claude-sonnet-4-6"
    variants_per_day: int = 3
    poll_interval_hours: int = 2
    daily_generate_cron: str = "0 7 * * *"
    generate_images: bool = True
    image_model: str = "grok-imagine-image-pro"
    image_aspect_ratio: str = "2:1"
    image_resolution: str = "2k"


class Server(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class Config(BaseModel):
    author: Author
    repos: list[RepoConfig] = Field(default_factory=list)
    # Keys are short slugs ('blitzpicks', 'e360', ...) used as `target` in the
    # generation flow. Absent from this dict → 'personal' is the only option.
    brand_voices: dict[str, BrandVoice] = Field(default_factory=dict)
    reddit_scan: RedditScan = RedditScan()
    generation: Generation = Generation()
    server: Server = Server()

    xai_api_key: str = ""
    github_token: str = ""

    linkedin_client_id: str = ""
    linkedin_client_secret: str = ""
    linkedin_redirect_uri: str = "http://127.0.0.1:8765/auth/linkedin/callback"

    # Separate LinkedIn app for Community Management API (company page posts).
    # LinkedIn requires it to be a distinct app from the member-scope one.
    linkedin_org_client_id: str = ""
    linkedin_org_client_secret: str = ""
    linkedin_org_redirect_uri: str = "http://127.0.0.1:8765/auth/linkedin/org/callback"

    ingest_token: str = ""  # gates POST /ingest; if empty, endpoint is disabled

    # Reddit scanner uses public .json endpoints only — no OAuth needed since
    # self-service app creation was removed in Nov 2025 (Responsible Builder
    # Policy). Just a real User-Agent; Reddit 429s generic ones.
    reddit_user_agent: str = ""


def load_config(
    config_path: str | Path = "config.yaml",
    env_path: str | Path = ".env",
) -> Config:
    load_dotenv(env_path)
    data: dict = {}
    if Path(config_path).exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    data["xai_api_key"] = os.environ.get("XAI_API_KEY", "")
    data["github_token"] = os.environ.get("GITHUB_TOKEN", "")
    data["linkedin_client_id"] = os.environ.get("LINKEDIN_CLIENT_ID", "")
    data["linkedin_client_secret"] = os.environ.get("LINKEDIN_CLIENT_SECRET", "")
    env_redirect = os.environ.get("LINKEDIN_REDIRECT_URI", "").strip()
    if env_redirect:
        data["linkedin_redirect_uri"] = env_redirect
    data["linkedin_org_client_id"] = os.environ.get("LINKEDIN_ORG_CLIENT_ID", "")
    data["linkedin_org_client_secret"] = os.environ.get("LINKEDIN_ORG_CLIENT_SECRET", "")
    env_org_redirect = os.environ.get("LINKEDIN_ORG_REDIRECT_URI", "").strip()
    if env_org_redirect:
        data["linkedin_org_redirect_uri"] = env_org_redirect
    data["ingest_token"] = os.environ.get("INGEST_TOKEN", "")
    data["reddit_user_agent"] = os.environ.get("REDDIT_USER_AGENT", "")
    return Config.model_validate(data)
