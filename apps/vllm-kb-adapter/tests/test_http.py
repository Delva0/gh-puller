"""Public HTTP endpoint compatibility tests."""

from typing import Any

from fastapi.testclient import TestClient

from vllm_kb_adapter.app import create_app
from vllm_kb_adapter.snapshots import SnapshotRegistry


class FakeUpstream:
    def __init__(self) -> None:
        self.closed = False

    async def request(self, _method: str, _params: dict[str, Any]) -> dict[str, Any]:
        return {"tools": []}

    async def aclose(self) -> None:
        self.closed = True


def test_endpoint_accepts_vllm_kb_request_without_json_accept_header(
    registry: SnapshotRegistry,
) -> None:
    upstream = FakeUpstream()
    app = create_app(registry, upstream)
    with TestClient(app) as client:
        response = client.post(
            "/gh-puller/graph",
            headers={"Accept": ""},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )

        assert response.status_code == 200
        assert response.json() == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
    assert upstream.closed is True


def test_endpoint_returns_json_rpc_parse_error(registry: SnapshotRegistry) -> None:
    with TestClient(create_app(registry, FakeUpstream())) as client:
        response = client.post(
            "/gh-puller/graph",
            headers={"Content-Type": "application/json"},
            content=b"not json",
        )

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32700
