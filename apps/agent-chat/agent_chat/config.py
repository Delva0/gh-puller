"""Validate public settings separately from memory-only credentials."""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

AgentKind = Literal["github", "gitcode", "code", "web"]
DEFAULT_BASE_URL = "https://st8tp3ajl0df3n8b8l8qu.apigateway-cn-beijing.volceapi.com/v1"


def model_url(value: str) -> str:
    url = urlsplit(value)
    if url.scheme not in {"http", "https"} or not url.hostname:
        raise ValueError("模型地址须为公网 HTTP(S) URL")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError("模型地址不能包含凭据、查询参数或片段")
    if url.port not in {None, 80, 443}:
        raise ValueError("模型地址仅支持 80 或 443 端口")
    return value.rstrip("/")


class PublicSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    base_url: str = Field(default=DEFAULT_BASE_URL, max_length=2048)
    model: str = Field(default="deepseek-v4.1-flash", min_length=1, max_length=200)
    backend: str = "dsl"
    ptc: Literal["off", "A", "B"] = "off"
    max_steps: int = Field(default=32, ge=1, le=128)
    concurrency: int = Field(default=8, ge=1, le=16)
    reasoning_effort: Literal["low", "high", "max"] = "high"
    thinking: bool = True
    max_tokens: int = Field(default=8192, ge=256, le=32768)
    web_search_backend: Literal["auto", "brave", "duckduckgo"] = "brave"
    web_search_concurrency: int = Field(default=1, ge=1, le=8)
    web_search_interval: float = Field(default=2, ge=0, le=60, allow_inf_nan=False)
    multimodal: bool = True

    _url = field_validator("base_url")(model_url)

    @field_validator("model")
    @classmethod
    def check_model(cls, value):
        if any(char.isspace() for char in value):
            raise ValueError("模型名不能包含空白")
        return value


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = SecretStr("")
    github_token: SecretStr = SecretStr("")
    gitcode_token: SecretStr = SecretStr("")
    brave_api_key: SecretStr = SecretStr("")

    def values(self):
        return {key: getattr(self, key).get_secret_value() for key in type(self).model_fields}


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[\w-]+$")
    prompt: str = Field(min_length=1, max_length=16000)
    settings: PublicSettings
    credentials: Credentials = Field(default_factory=Credentials)


@dataclass
class ServerSettings:
    password: str
    secure_cookie: bool = True
    idle_seconds: float = 3600
    run_seconds: float = 900
    max_sessions: int = 8
    max_running: int = 2
    event_bytes: int = 16 * 1024 * 1024
    storage_bytes: int = 64 * 1024 * 1024
    temp_root: str | None = None
    code_container: str = ""
    code_workdir: str = "/workspace"
    static_dir: Path = Path(__file__).resolve().parents[1] / "web" / "dist"
    revision: str = "development"

    @classmethod
    def from_env(cls):
        password = os.environ.get("CHAT_ACCESS_PASSWORD", "")
        if len(password) < 12:
            raise ValueError("Set CHAT_ACCESS_PASSWORD to a private passphrase of at least 12 characters")
        return cls(
            password=password, secure_cookie=os.getenv("CHAT_SECURE_COOKIE", "true").lower() != "false",
            code_container=os.getenv("CHAT_CODE_CONTAINER", ""),
            code_workdir=os.getenv("CHAT_CODE_WORKDIR", "/workspace"),
            static_dir=Path(os.getenv("CHAT_STATIC_DIR", str(cls.static_dir))),
            revision=os.getenv("RENDER_GIT_COMMIT") or os.getenv("APP_REVISION", "development"),
        )


def catalog(server: ServerSettings):
    node = bool(shutil.which("node"))
    code = bool(server.code_container and shutil.which("docker"))
    return [
        {"id": "github", "name": "GitHub", "available": True, "reason": "",
         "backends": ["dsl", "rest", "graphql", "split"], "ptc": node, "web": True},
        {"id": "gitcode", "name": "GitCode", "available": True, "reason": "",
         "backends": ["dsl", "rest"], "ptc": node, "web": True},
        {"id": "code", "name": "Code", "available": code,
         "reason": "" if code else "当前服务未配置 Docker 容器连接",
         "backends": [], "ptc": False, "web": False},
        {"id": "web", "name": "Web", "available": True, "reason": "",
         "backends": [], "ptc": False, "web": True},
    ]


def validate_capabilities(kind: AgentKind, settings: PublicSettings, server: ServerSettings):
    capability = next(item for item in catalog(server) if item["id"] == kind)
    if not capability["available"]:
        raise ValueError(capability["reason"])
    if settings.backend not in (capability["backends"] or [""]):
        raise ValueError("该 agent 不支持所选查询后端")
    if settings.ptc != "off" and not capability["ptc"]:
        raise ValueError("该 agent 或当前服务不支持 PTC")
