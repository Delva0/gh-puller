"""Validate public settings separately from memory-only credentials."""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from gh_puller.agents import AGENTS
from gh_puller.tools.tool_offload import OffloadPolicy
from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, SecretStr, field_validator

AgentKind = str
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
    options: dict[str, JsonValue] = Field(default_factory=dict)
    reasoning_effort: str = "high"
    thinking: bool = True
    max_tokens: int = Field(default=0, ge=0)

    _url = field_validator("base_url")(model_url)

    @field_validator("model")
    @classmethod
    def check_model(cls, value):
        if any(char.isspace() for char in value):
            raise ValueError("模型名不能包含空白")
        return value


class Credentials(RootModel[dict[str, SecretStr]]):
    root: dict[str, SecretStr] = Field(default_factory=dict)

    @field_validator("root")
    @classmethod
    def known_credentials(cls, value):
        known = {"api_key"} | {key for agent in AGENTS.values() for key in agent.credential_names}
        if value.keys() - known:
            raise ValueError("Unknown credentials")
        return value

    def values(self):
        return {key: value.get_secret_value() for key, value in self.root.items()}


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


def resources(server: ServerSettings):
    return {"node": bool(shutil.which("node")),
            "container": server.code_container if shutil.which("docker") else "",
            "workdir": server.code_workdir}


def resolve_settings(kind, settings):
    options = {**AGENTS[kind].configuration.public_defaults(), **settings.options}
    return settings.model_copy(update={"options": options})


def catalog(server: ServerSettings):
    entries = [agent.configuration.catalog(resources(server)) for agent in AGENTS.values()]
    for entry in entries:
        for item in entry["fields"]:
            if item["tool"] == "tool_results" and item["default"] is None:
                item["effective_default"] = getattr(OffloadPolicy(), item["key"].removeprefix("tool_result_"))
                item["description"] = ""
    return entries


def validate_capabilities(kind: AgentKind, settings: PublicSettings, server: ServerSettings):
    return AGENTS[kind].configuration.resolve(settings.options, resources(server))
