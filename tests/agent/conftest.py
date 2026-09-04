"""Reset global Agent monitor state after every local Agent test."""

import asyncio

import pytest_asyncio

from gh_puller import agent
from gh_puller.agent.events import set_active_bus


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_monitor():
    yield
    agent.configure(ws_urls=[], otel_urls=[])
    set_active_bus(None)
    await asyncio.sleep(0)
