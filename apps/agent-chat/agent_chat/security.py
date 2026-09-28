"""Keep credentials out of observations and pin model connections to public addresses."""

import asyncio
import ipaddress
import json
import re
import socket
from urllib.parse import quote

import httpx

from .config import model_url

SECRET_KEY = re.compile(r"(?i)(api.?key|access.?token|private.?token|password|authorization|credential|secret|token)$")


class SecretFilter:
    """Retain credential values only in memory, including suffixes split across stream chunks."""

    def __init__(self):
        self.secrets = set()
        self.pending = {}

    def add(self, values):
        for value in values:
            if value:
                self.secrets.update({value, quote(value, safe=""), json.dumps(value)[1:-1]})

    def text(self, value: str) -> str:
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, "[redacted]")
        return value

    def clean(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {key: "[redacted]" if SECRET_KEY.search(key) else self.clean(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        return value

    def fragment(self, key, value: str, *, final=False) -> str:
        value = self.text(self.pending.pop(key, "") + value)
        keep = 0
        if not final:
            for secret in self.secrets:
                for length in range(1, min(len(secret), len(value) + 1)):
                    if value.endswith(secret[:length]):
                        keep = max(keep, length)
        if keep:
            self.pending[key] = value[-keep:]
            return value[:-keep]
        return value


async def public_address(host: str, port: int) -> str:
    """Reject mixed public/private DNS answers before returning an address for this connection."""
    answers = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = [item[4][0] for item in answers]
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("模型地址必须解析到公网地址，不能访问本地或内网")
    return addresses[0]


class PublicTransport(httpx.AsyncHTTPTransport):
    """Pin each request to checked DNS while preserving its TLS name and HTTP Host header.

    Redirects remain disabled by the caller. Proxy environment variables never
    participate in this transport, so they cannot bypass address validation.
    """

    def __init__(self, *, allow_query=False):
        super().__init__(trust_env=False)
        self.allow_query = allow_query

    async def handle_async_request(self, request):
        model_url(str(request.url.copy_with(query=None) if self.allow_query else request.url))
        host = request.url.host
        address = await public_address(host, request.url.port or (443 if request.url.scheme == "https" else 80))
        request.extensions["sni_hostname"] = host
        request.url = request.url.copy_with(host=address)
        return await super().handle_async_request(request)
