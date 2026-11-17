"""Authentication mode selection and safe authentication failure diagnostics."""

from __future__ import annotations

import base64
import logging

import httpx
import pytest

from corporate_kb.confluence.client import ConfluenceClient
from corporate_kb.confluence.models import ConfluenceError, SourceConfig
from corporate_kb.confluence.registry import ConfluenceRegistry

EMAIL = "private-confluence-account@example.com"
TOKEN = "private-confluence-personal-token"


def source_config(**values):
    return SourceConfig.model_validate(
        {
            "name": "Authentication check",
            "index_id": "docs",
            "base_url": "https://confluence.example.com",
            "root_page_url": "123",
            "schedule": {"kind": "manual"},
            **values,
        }
    )


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
@pytest.mark.parametrize(
    "deployment,base_url,username,expected_mode",
    [
        ("data_center", "https://confluence.example.com", None, "bearer"),
        ("data_center", "https://confluence.example.com", EMAIL, "bearer"),
        ("data_center", "https://team.atlassian.net/wiki", EMAIL, "bearer"),
        ("auto", "https://confluence.example.com", None, "bearer"),
        ("auto", "https://confluence.example.com", EMAIL, "basic"),
        ("auto", "https://team.atlassian.net/wiki", EMAIL, "basic"),
        ("cloud", "https://confluence.example.com", EMAIL, "basic"),
    ],
)
def test_selected_auth_mode_controls_real_request_and_safe_logs(
    caplog, level, deployment, base_url, username, expected_mode
):
    config = source_config(deployment=deployment, base_url=base_url, username=username)
    assert config.auth_mode == expected_mode
    assert "auth_mode" not in config.model_dump()
    requests = []
    encoded_credentials = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    expected_authorization = (
        f"Basic {encoded_credentials}" if expected_mode == "basic" else f"Bearer {TOKEN}"
    )

    def handle(request):
        requests.append(request)
        assert request.headers["Authorization"] == expected_authorization
        return httpx.Response(200, json={"id": "123"})

    with caplog.at_level(level):
        client = ConfluenceClient(config, TOKEN, transport=httpx.MockTransport(handle))
        try:
            assert client._request(client.api + "/content/123").status_code == 200
        finally:
            client.close()

    assert len(requests) == 1
    assert f"auth={expected_mode}" in caplog.text
    for secret in (EMAIL, TOKEN, encoded_credentials):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    "deployment,expected_mode", [("data_center", "bearer"), ("cloud", "basic")]
)
def test_401_explains_sent_mode_without_leaking_secrets_or_retrying(
    caplog, monkeypatch, deployment, expected_mode
):
    config = source_config(deployment=deployment, username=EMAIL)
    encoded_credentials = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    unsafe_response = f"Authorization: Bearer {TOKEN}; account={EMAIL}; Basic {encoded_credentials}"
    requests, waits = [], []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            401,
            text=unsafe_response,
            headers={
                "WWW-Authenticate": f'Basic realm="{unsafe_response}"',
                "Set-Cookie": f"session={TOKEN}",
                "X-Seraph-LoginReason": unsafe_response,
            },
        )

    with caplog.at_level(logging.DEBUG):
        client = ConfluenceClient(config, TOKEN, transport=httpx.MockTransport(handle), retries=3)
        monkeypatch.setattr(client, "_wait", waits.append)
        try:
            with pytest.raises(ConfluenceError, match="Authentication failed") as caught:
                client.page("123", body=False)
        finally:
            client.close()

    failure = caught.value
    assert failure.status_code == 401
    assert failure.error_code == "authentication"
    assert "HTTP 401" in str(failure)
    assert f"auth={expected_mode}" in str(failure)
    assert "Confluence or its gateway" in str(failure)
    assert len(requests) == 1 and waits == []
    for secret in (EMAIL, TOKEN, encoded_credentials, unsafe_response):
        assert secret not in str(failure) + caplog.text


def test_saved_data_center_source_ignores_stale_email_without_resaving(tmp_path, caplog):
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.path == "/rest/api/content/123"
        return httpx.Response(200, json={"id": "123", "title": "Root", "version": {"number": 1}})

    registry = ConfluenceRegistry(
        tmp_path / "state",
        index_exists=lambda index: index == "docs",
        knowledge_root=lambda index: tmp_path / "knowledge" / index,
        publish=lambda index, **kwargs: {},
        client_factory=lambda config, token: ConfluenceClient(
            config, token, transport=httpx.MockTransport(handle)
        ),
    )
    config = source_config(deployment="data_center", username=EMAIL)
    saved = registry.upsert_source(config.model_dump(), TOKEN)
    assert saved["username"] == EMAIL
    with caplog.at_level(logging.INFO):
        result = registry.test_connection({}, source_id=saved["id"])

    assert result["status"] == "connected"
    assert result["deployment"] == "data_center"
    assert len(requests) == 1
    assert "auth=bearer" in caplog.text
    assert "auth=basic" not in caplog.text
    for secret in (EMAIL, TOKEN):
        assert secret not in caplog.text
