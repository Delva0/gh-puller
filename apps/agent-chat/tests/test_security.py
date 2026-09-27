"""Check URL pinning and credential scrubbing across arbitrary stream boundaries."""

import socket

import httpx
import pytest

from agent_chat.config import model_url
from agent_chat.security import PublicTransport, SecretFilter, public_address
from agent_chat.storage import PrivateStorage, StorageLimitError


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://user:pass@example.org/v1",
                                 "https://example.org/v1?api_key=secret", "https://example.org:8888/v1"])
def test_reject_credential_urls(url):
    with pytest.raises(ValueError, match="模型地址"):
        model_url(url)


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["169.254.169.254"], ["::1"],
                                      ["1.1.1.1", "10.0.0.1"], ["::ffff:127.0.0.1"]])
async def test_reject_nonpublic_dns(monkeypatch, addresses):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]

    import asyncio

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(ValueError, match="公网"):
        await public_address("example.org", 443)


async def test_dns_pinning_retains_host_and_tls_name(monkeypatch):
    async def resolve(host, port):
        assert host == "model.example" and port == 443
        return "1.1.1.1"

    async def send(self, request):
        assert request.url.host == "1.1.1.1"
        assert request.headers["Host"] == "model.example"
        assert request.extensions["sni_hostname"] == "model.example"
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"))

    monkeypatch.setattr("agent_chat.security.public_address", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    async with httpx.AsyncClient(transport=PublicTransport(), follow_redirects=False) as client:
        response = await client.post("https://model.example/v1/chat/completions")
    assert response.status_code == 200


def test_split_secrets_in_events_and_files(tmp_path):
    scrubber = SecretFilter()
    scrubber.add(["secret-credential"])
    parts = [scrubber.fragment("request", item) for item in ["hello se", "cret-", "credential world"]]
    parts.append(scrubber.fragment("request", "", final=True))
    assert "".join(parts) == "hello [redacted] world"
    storage = PrivateStorage(tmp_path, scrubber, 1000)
    with storage.binary("body") as target:
        target.write(b"hello secr")
        target.write(b"et-credential world")
    assert (tmp_path / "body").read_text() == "hello [redacted] world"
    storage.write("data.json", {"value": "secret-credential"})
    assert "secret-credential" not in (tmp_path / "data.json").read_text()
    with pytest.raises(StorageLimitError):
        storage.write("large", b"x" * 1000)
