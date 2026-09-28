"""Discover OpenAI-compatible model IDs without retaining connection credentials."""

import json

import httpx
from fastapi import HTTPException
from gh_puller.tools.gitcode_api import API_ORIGIN as GITCODE_ORIGIN
from gh_puller.tools.github_api import API_ORIGIN as GITHUB_ORIGIN
from gh_puller.tools.tool_web import BRAVE_SEARCH_URL
from pydantic import BaseModel, Field, SecretStr, field_validator

from .config import model_url
from .security import PublicTransport, SecretFilter


class ModelConnection(BaseModel):
    base_url: str = Field(max_length=2048)
    api_key: SecretStr = SecretStr("")
    session_id: str | None = None

    _url = field_validator("base_url")(model_url)


class CredentialCheck(BaseModel):
    name: str = Field(max_length=100)
    value: SecretStr


async def check_credential(connection, transport=None):
    key = connection.value.get_secret_value()
    if not key:
        raise HTTPException(422, "请先输入 API Key")
    endpoints = {
        "github_token": (GITHUB_ORIGIN + "/user", {"Authorization": "Bearer " + key}),
        "gitcode_token": (GITCODE_ORIGIN + "/api/v5/user", {"Authorization": "Bearer " + key}),
        "brave_api_key": (BRAVE_SEARCH_URL + "?q=connection+test&count=1",
                          {"X-Subscription-Token": key}),
    }
    if connection.name not in endpoints:
        raise HTTPException(422, "该凭据尚无连接测试接口")
    url, headers = endpoints[connection.name]
    try:
        async with (
            httpx.AsyncClient(transport=transport or PublicTransport(allow_query=True), timeout=15,
                              follow_redirects=False) as client,
            client.stream("GET", url, headers=headers) as response,
        ):
            if response.status_code in {401, 403}:
                raise HTTPException(422, "API Key 验证失败")
            response.raise_for_status()
            return {"ok": True}
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "连接测试失败，请检查凭据、额度和网络") from None


async def discover(connection, manager, owner, transport=None):
    key = connection.api_key.get_secret_value()
    if not key and connection.session_id:
        session = manager.get(owner, connection.session_id)
        if session.credential_base_url == connection.base_url:
            key = session.credentials.get("api_key", "")
    if not key:
        raise HTTPException(422, "请先输入模型 API Key")
    try:
        async with (
            httpx.AsyncClient(transport=transport or PublicTransport(), timeout=15, follow_redirects=False) as client,
            client.stream("GET", connection.base_url + "/models",
                          headers={"Authorization": "Bearer " + key}) as response,
        ):
            if response.status_code in {401, 403}:
                raise HTTPException(422, "模型 API Key 验证失败")
            if response.status_code in {404, 405}:
                raise HTTPException(422, "该模型服务未提供 /models 列表接口")
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise ValueError("Model list too large")
            data = json.loads(body)
            ids = sorted({item["id"] for item in data["data"]
                          if isinstance(item, dict) and isinstance(item.get("id"), str)
                          and 0 < len(item["id"]) <= 200 and not any(c.isspace() for c in item["id"])})
            if not ids:
                raise HTTPException(422, "模型服务返回了空模型列表")
            scrubber = SecretFilter()
            scrubber.add([key, manager.settings.password])
            return {"models": scrubber.clean(ids)}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        # Provider bodies can echo Authorization; only fixed diagnostic messages leave this boundary.
        raise HTTPException(502, "无法读取模型列表，请检查模型地址、网络及服务状态") from None
