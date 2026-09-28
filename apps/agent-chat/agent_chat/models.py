"""Discover model IDs and test provider credentials without retaining secrets."""

import json

import httpx
from fastapi import HTTPException
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
