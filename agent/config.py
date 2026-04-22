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


class RepoConfig(BaseModel):
    name: str
    type: Literal["local", "github"]
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
    generation: Generation = Generation()
    server: Server = Server()

    xai_api_key: str = ""
    github_token: str = ""


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
    return Config.model_validate(data)
