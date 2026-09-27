"""Launch an isolated deterministic app for browser tests; production never imports this module."""

from agent_chat.app import create_app
from agent_chat.config import ServerSettings

from .fakes import FakeFactory

app = create_app(ServerSettings(password="test-private-passphrase", secure_cookie=False), agent_factory=FakeFactory())
