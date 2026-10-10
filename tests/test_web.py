from __future__ import annotations

from fastapi.testclient import TestClient

from src.config import Settings
from src.web import WebServer


class DummyBot:
    def is_ready(self) -> bool:
        return False

    def get_guild(self, _guild_id: int):
        return None


class DummyManager:
    def add_listener(self, _listener):
        return None

    def remove_listener(self, _listener):
        return None

    def lavalink_connected(self) -> bool:
        return False


def make_server() -> WebServer:
    settings = Settings(discord_token="token", discord_guild_id=1, public_base_url="https://music.orza.mx")
    return WebServer(DummyBot(), DummyManager(), settings)


def test_health_is_public_and_reports_pending_oauth() -> None:
    client = TestClient(make_server().app)
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "discord": False,
        "lavalink": False,
        "oauthConfigured": False,
    }


def test_api_requires_a_discord_session() -> None:
    client = TestClient(make_server().app)
    response = client.get("/api/state")
    assert response.status_code == 401


def test_login_is_closed_when_oauth_is_not_configured() -> None:
    client = TestClient(make_server().app)
    response = client.get("/auth/discord", follow_redirects=False)
    assert response.status_code == 503


def test_oauth_state_is_signed() -> None:
    server = make_server()
    nonce = "nonce"
    import hashlib
    import hmac

    signature = hmac.new(server._state_secret, nonce.encode(), hashlib.sha256).hexdigest()
    assert server._valid_state(f"{nonce}.{signature}")
    assert not server._valid_state(f"{nonce}.invalid")
