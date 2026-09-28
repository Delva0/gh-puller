"""Bridge native configuration validation to browser-owned, memory-only credential checks."""

import hashlib

from fastapi import HTTPException
from gh_puller.agents import AGENTS
from pydantic import BaseModel, Field, JsonValue

from .config import Credentials, resources


class ConfigurationCheck(BaseModel):
    agents: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    tools: dict[str, JsonValue] = Field(default_factory=dict)
    credentials: Credentials = Field(default_factory=Credentials)
    session_id: str | None = None


class CredentialChecks:
    def __init__(self):
        self.results = {}

    def get(self, owner, name, value):
        entry = self.results.get((owner, name))
        return entry[1] if entry and entry[0] == hashlib.sha256(value.encode()).digest() else None

    async def test(self, owner, name, value, transport=None):
        spec = next((agent.configuration.credentials[name] for agent in AGENTS.values()
                     if name in agent.configuration.credentials), None)
        if not spec or not spec.validator:
            raise HTTPException(422, "该凭据尚无连接测试接口")
        if not value.strip():
            raise HTTPException(422, "请先输入 API Key")
        fingerprint = hashlib.sha256(value.encode()).digest()
        self.results[owner, name] = fingerprint, None
        result = await spec.validator(value, transport=transport)
        if self.results.get((owner, name), (None,))[0] == fingerprint:
            self.results[owner, name] = fingerprint, result
        if not result["valid"]:
            raise HTTPException(422, result["reason"])
        return {"ok": True}

    def forget(self, owner):
        self.results = {key: value for key, value in self.results.items() if key[0] != owner}


def validate_configuration(body, manager, owner, checks):
    credentials = {}
    if body.session_id:
        credentials.update(manager.get(owner, body.session_id).credentials)
    credentials.update({key: value for key, value in body.credentials.values().items() if value})
    checked = {key: checks.get(owner, key, value) for key, value in credentials.items()}
    reports, tools, fields = {}, {}, {}
    for name, agent in AGENTS.items():
        config = agent.configuration
        overrides = {key: body.tools[key] for key in config.owners if key in body.tools}
        overrides.update(body.agents.get(name, {}))
        report = config.validate(overrides, credentials, resources(manager.settings), checked)
        reports[name] = report
        for key in (*config.owners, *config.credentials):
            fields[key] = report["fields"][key]
        for item in report["tools"]:
            previous = tools.get(item["id"])
            if previous:
                merged = {**item,
                          "configuration": list(dict.fromkeys((*previous["configuration"], *item["configuration"]))),
                          "valid": previous["valid"] and item["valid"],
                          "issues": {**previous["issues"], **item["issues"]}}
            else:
                merged = item
            tools[item["id"]] = merged
    return {"agents": reports, "tools": list(tools.values()), "fields": fields}
