import pytest
from unittest.mock import AsyncMock, Mock, patch
from app.auth import build_github_auth_url, exchange_github_code, get_github_profile


def test_build_github_auth_url():
    url = build_github_auth_url("myclientid", "https://example.com/auth/callback")
    assert "client_id=myclientid" in url
    assert "scope=read:user" in url
    assert "redirect_uri=https://example.com/auth/callback" in url
    assert url.startswith("https://github.com/login/oauth/authorize")


def test_build_github_auth_url_includes_state_when_given():
    url = build_github_auth_url("myclientid", "https://example.com/auth/callback", state="xyz123")
    assert "state=xyz123" in url


@pytest.mark.asyncio
async def test_exchange_github_code_success():
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"access_token": "gho_abc123", "token_type": "bearer"}

    with patch("app.auth.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        token = await exchange_github_code("code123", "cid", "csecret", "https://cb.example.com")
    assert token == "gho_abc123"


@pytest.mark.asyncio
async def test_exchange_github_code_failure():
    mock_resp = Mock()
    mock_resp.status_code = 401
    mock_resp.json.return_value = {}

    with patch("app.auth.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        token = await exchange_github_code("bad_code", "cid", "csecret", "https://cb.example.com")
    assert token is None


@pytest.mark.asyncio
async def test_get_github_profile_success():
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": 42, "login": "octocat", "avatar_url": "https://avatars.example.com/42"}

    with patch("app.auth.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        profile = await get_github_profile("gho_abc123")
    assert profile == {"id": 42, "login": "octocat", "avatar_url": "https://avatars.example.com/42"}


@pytest.mark.asyncio
async def test_get_github_profile_failure():
    mock_resp = Mock()
    mock_resp.status_code = 401

    with patch("app.auth.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        profile = await get_github_profile("bad_token")
    assert profile is None
