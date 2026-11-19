from __future__ import annotations

import base64
import hashlib
import logging
from types import SimpleNamespace

import httpx
import pytest
import test_access_http as access_fixtures
from fastmcp import FastMCP
from test_confluence import registry

from corporate_kb.confluence.client import ConfluenceClient
from corporate_kb.confluence.http import register_confluence_routes
from corporate_kb.confluence.registry import ConfluenceRegistry

pki = access_fixtures.pki
secured = access_fixtures.secured
PREFIX = "/admin/api/confluence"
TOKEN = "private-confluence-token-never-return-123"


def _payload(**overrides):
    return {
        "name": "Existing corporate access",
        "index_id": "target",
        "base_url": "https://docs.example.test/confluence",
        "root_page_url": "1",
        "deployment": "data_center",
        "username": "ignored-on-data-center@example.test",
        "analyze_images": False,
        "schedule": {"kind": "manual"},
        **overrides,
    }


def _app(instance):
    server = FastMCP("confluence-credential-reuse-test")

    async def manager(request):
        return request.headers.get("x-test-role") == "manager"

    async def reader(request):
        return request.headers.get("x-test-role") == "reader"

    register_confluence_routes(
        server,
        instance,
        reader_authorized=reader,
        manager_authorized=manager,
        indexes=lambda: [{"id": "target"}, {"id": "another-index"}],
        gigacode=SimpleNamespace(status=lambda: {"configured": False}),
    )
    return server.http_app()


def _upstream(instance, requests, *, status=200, tls_error=False):
    def handle(request):
        requests.append(request)
        if tls_error:
            raise httpx.ConnectError("CERTIFICATE_VERIFY_FAILED", request=request)
        return httpx.Response(
            status,
            json={
                "id": request.url.path.rsplit("/", 1)[-1],
                "title": "Root page",
                "version": {"number": 3},
                "body": {},
                "_links": {},
            },
        )

    instance.client_factory = lambda conf, token: ConfluenceClient(
        conf, token, transport=httpx.MockTransport(handle), retries=0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("deployment", ["cloud", "data_center"])
async def test_reuse_checks_creates_edits_and_persists_actual_upstream_auth(
    tmp_path, caplog, deployment
):
    instance, _, _, _, _ = registry(tmp_path)
    instance.index_exists = lambda index: index in {"target", "another-index"}
    donor_values = _payload(deployment=deployment, username="owner@example.test")
    if deployment == "cloud":
        donor_values["base_url"] = "https://workspace.atlassian.net"
    donor = instance.upsert_source(donor_values, TOKEN)
    with instance._db() as db:
        encrypted = bytes(db.execute("SELECT credential FROM sources").fetchone()[0])
    requests = []
    _upstream(instance, requests)
    target = _payload(
        name="Independent root and index",
        index_id="another-index",
        root_page_url="27",
        base_url=donor_values["base_url"] + ("/wiki" if deployment == "cloud" else ""),
        # Selected access must supply its own authentication settings.
        deployment="auto",
        username="other@example.test",
        schedule={"kind": "weekly", "weekday": 4, "time": "12:40"},
        credential_source_id=donor["id"],
    )
    public_responses = []
    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(instance)),
            base_url="http://testserver",
            headers={"x-test-role": "manager"},
        ) as client:
            listed = await client.get(PREFIX + "/credentials")
            assert listed.status_code == 200, listed.text
            assert listed.headers["cache-control"] == "no-store"
            credential = listed.json()["credentials"][0]
            assert set(credential) == {
                "source_id",
                "name",
                "base_url",
                "deployment",
                "username",
                "auth_mode",
                "last_success_at",
            }
            assert credential["source_id"] == donor["id"]
            assert credential["auth_mode"] == ("basic" if deployment == "cloud" else "bearer")
            checked = await client.post(PREFIX + "/test", json=target)
            assert checked.status_code == 200, checked.text
            assert checked.json()["root_page_id"] == "27"
            assert len(instance.list_sources()) == 1  # Checking never creates a source.
            created = await client.post(PREFIX + "/sources", json=target)
            assert created.status_code == 200, created.text
            source, job = created.json()["source"], created.json()["job"]
            assert job["status"] == "queued"
            assert job["source_id"] == source["id"]
            assert source["index_id"] == "another-index"
            assert source["root_page_url"] == "27"
            assert source["schedule"]["kind"] == "weekly"
            assert source["schedule"]["weekday"] == 4
            assert source["schedule"]["time"] == "12:40"
            assert source["deployment"] == deployment
            assert source["username"] == "owner@example.test"
            instance.cancel_job(job["id"])
            instance.upsert_source(
                {"username": "old-access@example.test"},
                token="different-target-token",
                source_id=source["id"],
            )
            edited = await client.patch(
                PREFIX + f"/sources/{source['id']}",
                json={"credential_source_id": donor["id"], "root_page_url": "28"},
            )
            assert edited.status_code == 200, edited.text
            assert edited.json()["source"]["index_id"] == "another-index"
            assert edited.json()["source"]["root_page_url"] == "28"
            assert edited.json()["source"]["schedule"] == source["schedule"]
            assert edited.json()["source"]["username"] == "owner@example.test"
            public_responses.extend([listed.text, checked.text, created.text, edited.text])
        # The destination owns a durable copy, independent of donor deletion and restart.
        instance.delete_source(donor["id"])
        restarted, _, _, _, _ = registry(tmp_path)
        restarted.index_exists = instance.index_exists
        _upstream(restarted, requests)
        assert restarted.test_connection({}, source_id=source["id"])["root_page_id"] == "28"
        assert [item["source_id"] for item in restarted.list_credentials()] == [source["id"]]
    expected_auth = (
        "Basic " + base64.b64encode(f"owner@example.test:{TOKEN}".encode()).decode()
        if deployment == "cloud"
        else "Bearer " + TOKEN
    )
    assert len(requests) == 2
    assert [request.headers["authorization"] for request in requests] == [expected_auth] * 2
    api_path = "/wiki/api/v2/pages/" if deployment == "cloud" else "/confluence/rest/api/content/"
    assert [request.url.path for request in requests] == [api_path + "27", api_path + "28"]
    exposed = "\n".join(public_responses) + caplog.text
    for secret in (
        TOKEN,
        encrypted.decode(),
        hashlib.sha256(TOKEN.encode()).hexdigest(),
        expected_auth,
    ):
        assert secret not in exposed


@pytest.mark.parametrize(
    "new_base",
    [
        "https://other.example.test/confluence",
        "http://docs.example.test/confluence",
        "https://docs.example.test:8443/confluence",
        "https://docs.example.test/another-context",
        "https://docs.example.test",
    ],
)
@pytest.mark.parametrize("operation", ["test", "create", "edit", "test_existing", "edit_existing"])
def test_saved_token_cannot_move_to_another_origin_or_context_before_network(
    tmp_path, new_base, operation
):
    instance, _, _, _, _ = registry(tmp_path)
    donor = instance.upsert_source(_payload(), TOKEN)
    attempted = []

    def forbidden_factory(*args):
        attempted.append(args)
        raise AssertionError("scope validation must run before client creation")

    instance.client_factory = forbidden_factory
    values = _payload(name="Redirected access", base_url=new_base)
    with pytest.raises(ValueError):
        if operation == "test":
            instance.test_connection(values, credential_source_id=donor["id"])
        elif operation == "create":
            instance.upsert_source(values, credential_source_id=donor["id"])
        elif operation == "edit":
            instance.upsert_source(values, source_id=donor["id"], credential_source_id=donor["id"])
        elif operation == "test_existing":
            instance.test_connection(values, source_id=donor["id"])
        else:
            instance.upsert_source(values, source_id=donor["id"])
    assert attempted == []
    assert instance.get_source(donor["id"])["base_url"] == _payload()["base_url"]


@pytest.mark.parametrize(
    ("original", "equivalent"),
    [
        ("https://docs.example.test/confluence", "https://DOCS.EXAMPLE.TEST:443/confluence/"),
        ("http://docs.example.test/confluence", "http://docs.example.test:80/confluence"),
        ("https://bücher.example/confluence", "https://xn--bcher-kva.example/confluence"),
        (
            "https://[2001:db8:0:0:0:0:0:1]/confluence",
            "https://[2001:db8::1]:443/confluence",
        ),
    ],
)
def test_canonical_origin_equivalence_and_explicit_new_token_scope_change(
    tmp_path, original, equivalent
):
    instance, _, _, _, _ = registry(tmp_path)
    donor = instance.upsert_source(_payload(base_url=original), TOKEN)
    reused = instance.upsert_source(_payload(base_url=equivalent), credential_source_id=donor["id"])
    assert reused["credential_configured"] is True
    changed = instance.upsert_source(
        {"base_url": "https://new.example.test/other"},
        token="explicit-new-token",
        source_id=reused["id"],
    )
    assert changed["base_url"] == "https://new.example.test/other"


def test_saved_token_does_not_cross_distinct_idna_hosts(tmp_path):
    instance, _, _, _, _ = registry(tmp_path)
    donor = instance.upsert_source(_payload(base_url="https://faß.example/confluence"), TOKEN)
    attempted = []

    def forbidden_factory(*args):
        attempted.append(args)
        raise AssertionError("IDNA scope must be checked before client creation")

    instance.client_factory = forbidden_factory
    different = _payload(base_url="https://fass.example/confluence")
    with pytest.raises(ValueError):
        instance.test_connection(different, credential_source_id=donor["id"])
    with pytest.raises(ValueError):
        instance.upsert_source(different, credential_source_id=donor["id"])
    assert attempted == []
    assert len(instance.list_sources()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["test", "create", "edit"])
async def test_invalid_reuse_selection_is_rejected_without_publishing_or_contacting_upstream(
    tmp_path, operation
):
    instance, _, _, _, _ = registry(tmp_path)
    donor = instance.upsert_source(_payload(), TOKEN)
    archived = instance.upsert_source(_payload(name="Archived"), "archived-token")
    instance.delete_source(archived["id"])
    requests = []
    _upstream(instance, requests)
    method = "PATCH" if operation == "edit" else "POST"
    path = (
        f"/sources/{donor['id']}"
        if operation == "edit"
        else "/sources"
        if operation == "create"
        else "/test"
    )
    cases = [
        ({"credential_source_id": donor["id"], "token": TOKEN}, 400),
        ({"credential_source_id": donor["id"], "token": ""}, 400),
        ({"credential_source_id": 42}, 400),
        ({"credential_source_id": []}, 400),
        ({"credential_source_id": ""}, 400),
        ({"credential_source_id": "missing"}, 404),
        ({"credential_source_id": archived["id"]}, 404),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(instance)),
        base_url="http://testserver",
        headers={"x-test-role": "manager"},
    ) as client:
        for selection, expected in cases:
            response = await client.request(method, PREFIX + path, json=_payload(**selection))
            assert response.status_code == expected, (selection, response.text)
            assert TOKEN not in response.text
    assert requests == []
    assert instance.jobs() == []
    assert len(instance.list_sources()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [401, 429, "tls"])
async def test_upstream_failure_has_safe_metadata_and_does_not_revoke_shared_access(
    tmp_path, caplog, failure
):
    instance, _, _, _, _ = registry(tmp_path)
    donor = instance.upsert_source(_payload(), TOKEN)
    sibling = instance.upsert_source(
        _payload(name="Other root", root_page_url="2"), credential_source_id=donor["id"]
    )
    requests = []
    _upstream(
        instance, requests, status=200 if failure == "tls" else failure, tls_error=failure == "tls"
    )
    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(instance)),
            base_url="http://testserver",
            headers={"x-test-role": "manager"},
        ) as client:
            response = await client.post(
                PREFIX + "/test", json=_payload(credential_source_id=donor["id"])
            )
    assert response.status_code == 409, response.text
    if failure == "tls":
        assert response.json()["error_code"] == "tls_certificate"
    else:
        assert response.json()["upstream_status"] == failure
    assert TOKEN not in response.text + caplog.text
    assert {row["source_id"] for row in instance.list_credentials()} == {donor["id"], sibling["id"]}
    assert instance.get_source(donor["id"])["credential_configured"] is True
    assert instance.get_source(sibling["id"])["credential_configured"] is True
    _upstream(instance, requests)
    assert instance.test_connection({}, source_id=sibling["id"])["status"] == "connected"
    assert requests[-1].headers["authorization"] == "Bearer " + TOKEN


@pytest.mark.asyncio
async def test_credentials_and_mutations_require_admin_and_cookie_csrf(secured):
    app, _, store, certificates = secured
    instance = next(item for item in app.state.integrations if isinstance(item, ConfluenceRegistry))
    donor = instance.upsert_source(_payload(index_id="default"), TOKEN)
    requests = []
    _upstream(instance, requests)
    user = store.enroll(certificates["client"])
    target = _payload(index_id="default", credential_source_id=donor["id"])
    writes = [
        ("POST", "/test", target),
        ("POST", "/sources", target),
        ("PATCH", f"/sources/{donor['id']}", {"credential_source_id": donor["id"]}),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        for headers in ({}, {"authorization": "Bearer " + user.token}):
            listed = await client.get(PREFIX + "/credentials", headers=headers)
            assert listed.status_code == 401
            for method, suffix, payload in writes:
                response = await client.request(
                    method, PREFIX + suffix, json=payload, headers=headers
                )
                assert response.status_code == 401
        login = await client.post(
            "/access/api/login", json={"username": "admin", "password": access_fixtures.PASSWORD}
        )
        assert login.status_code == 200, login.text
        assert (await client.get(PREFIX + "/credentials")).status_code == 200
        for headers in (
            {},
            {"x-csrf-token": "invalid", "origin": "https://testserver"},
            {"x-csrf-token": login.json()["csrf_token"], "origin": "https://other.test"},
        ):
            for method, suffix, payload in writes:
                response = await client.request(
                    method, PREFIX + suffix, json=payload, headers=headers
                )
                assert response.status_code == 401
        assert requests == []
        client.headers.update(
            {"x-csrf-token": login.json()["csrf_token"], "origin": "https://testserver"}
        )
        checked = await client.post(PREFIX + "/test", json=target)
        assert checked.status_code == 200, checked.text
        edited = await client.patch(
            PREFIX + f"/sources/{donor['id']}", json={"credential_source_id": donor["id"]}
        )
        assert edited.status_code == 200, edited.text
        created = await client.post(PREFIX + "/sources", json=target)
        assert created.status_code == 200, created.text
        assert created.json()["job"]["status"] == "queued"
    assert len(requests) == 1
