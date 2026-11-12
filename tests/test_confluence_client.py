"""HTTP contracts based on Atlassian's published Cloud v2 and DC response shapes."""

from __future__ import annotations

import base64

import httpx
import pytest

from corporate_kb.confluence.client import ConfluenceClient
from corporate_kb.confluence.models import Attachment, ConfluenceError, SourceConfig


def source(*, cloud: bool = False, **overrides) -> SourceConfig:
    return SourceConfig.model_validate(
        {
            "name": "API contract test",
            "index_id": "target",
            "base_url": "https://team.atlassian.net/wiki"
            if cloud
            else "https://docs.example.com/confluence",
            "root_page_url": "1",
            "username": "reader@example.com" if cloud else None,
            **overrides,
        }
    )


def test_cloud_walks_beyond_descendants_depth_and_through_every_container():
    requests = []
    # Twelve nesting levels exceed the descendants endpoint's maximum depth of ten.
    chain = [("pages", "1"), ("folders", "2"), ("databases", "3")]
    chain += [("whiteboards", "4"), ("embeds", "5")]
    chain += [("pages", str(number)) for number in range(6, 14)]
    kinds = {"pages": "page", "folders": "folder", "databases": "database"}
    kinds.update(whiteboards="whiteboard", embeds="embed")

    def handle(request):
        requests.append(request)
        assert request.url.path.endswith("/direct-children")
        parent = next(
            index
            for index, (collection, content_id) in enumerate(chain)
            if request.url.path == f"/wiki/api/v2/{collection}/{content_id}/direct-children"
        )
        if parent == len(chain) - 1:
            # Repeated links cannot cause an infinite traversal.
            return httpx.Response(200, json={"results": [{"id": "1", "type": "page"}]})
        collection, child_id = chain[parent + 1]
        return httpx.Response(
            200,
            json={"results": [{"id": child_id, "type": kinds[collection], "status": "current"}]},
        )

    client = ConfluenceClient(
        source(cloud=True), "test-token", transport=httpx.MockTransport(handle)
    )
    try:
        assert list(client.page_ids("1")) == ["1", *map(str, range(6, 14))]
        assert len(requests) == len(chain)
    finally:
        client.close()


def test_cloud_direct_children_pagination_supports_link_header_and_body_links():
    visited = []

    def handle(request):
        visited.append(str(request.url))
        if request.url.path != "/wiki/api/v2/pages/1/direct-children":
            return httpx.Response(200, json={"results": []})
        cursor = request.url.params.get("cursor")
        if cursor == "second":
            return httpx.Response(
                200,
                json={
                    "results": [{"id": "3", "type": "page"}],
                    "_links": {"next": "?cursor=third&limit=100"},
                },
            )
        if cursor == "third":
            return httpx.Response(200, json={"results": [{"id": "4", "type": "page"}]})
        return httpx.Response(
            200,
            json={"results": [{"id": "2", "type": "page"}]},
            headers={"Link": '</wiki/api/v2/pages/1/direct-children?cursor=second>; rel="next"'},
        )

    client = ConfluenceClient(
        source(cloud=True), "test-token", transport=httpx.MockTransport(handle)
    )
    try:
        assert list(client.page_ids("1")) == ["1", "2", "3", "4"]
        assert len(visited) == 6
    finally:
        client.close()


def test_cloud_rejects_incomplete_tree_when_limit_or_unknown_container_is_reached():
    for config, row, message in [
        (source(cloud=True, max_pages=1), {"id": "2", "type": "folder"}, "limit"),
        (source(cloud=True), {"id": "2", "type": "new-container"}, "unsupported"),
    ]:
        client = ConfluenceClient(
            config,
            "test-token",
            transport=httpx.MockTransport(
                lambda request, row=row: httpx.Response(200, json={"results": [row]})
            ),
        )
        try:
            with pytest.raises(ConfluenceError, match=message):
                list(client.page_ids("1"))
        finally:
            client.close()


def test_dc_history_uses_experimental_endpoint_and_reads_pinned_body():
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-pat"
        if request.url.path == "/confluence/rest/experimental/content/1/version":
            if request.url.params.get("start") == "2":
                return httpx.Response(
                    200, json={"results": [{"number": 1}], "start": 2, "limit": 2}
                )
            return httpx.Response(
                200,
                json={
                    "results": [{"number": 3, "when": "2026-01-03T00:00:00Z"}, {"number": 2}],
                    "start": 0,
                    "limit": 2,
                    "_links": {"next": "/rest/experimental/content/1/version?start=2&limit=2"},
                },
            )
        assert request.url.path == "/confluence/rest/api/content/1"
        assert request.url.params["status"] == "historical"
        assert request.url.params["version"] == "2"
        assert request.url.params["expand"] == "version,ancestors,body.storage"
        return httpx.Response(
            200,
            json={
                "id": "1",
                "title": "Historic title",
                "version": {"number": 2, "when": "2026-01-02T00:00:00Z"},
                "ancestors": [{"id": "9"}],
                "body": {
                    "storage": {"value": "<p>Historical content</p>", "representation": "storage"}
                },
                "_links": {"webui": "/display/TEST/Historic+title"},
            },
        )

    client = ConfluenceClient(source(), "test-pat", transport=httpx.MockTransport(handle))
    try:
        assert client.versions("1") == [3, 2, 1]
        page = client.page("1", version=2)
        assert page.version == 2 and page.parent_id == "9"
        assert page.updated_at == "2026-01-02T00:00:00Z"
        assert page.body == "<p>Historical content</p>"
        assert page.url == "https://docs.example.com/confluence/display/TEST/Historic+title"
        assert len(requests) == 3
    finally:
        client.close()


@pytest.mark.parametrize("status", [404, 405, 501])
def test_dc_unavailable_history_has_actionable_error(status):
    client = ConfluenceClient(
        source(), "test-pat", transport=httpx.MockTransport(lambda request: httpx.Response(status))
    )
    try:
        with pytest.raises(ConfluenceError, match="history API is unavailable") as error:
            client.versions("1")
        assert error.value.status_code == status
        assert "test-pat" not in str(error.value)
    finally:
        client.close()


def test_dc_attachment_version_expanded_and_changed_without_page_edit():
    version = 4

    def handle(request):
        assert request.url.path == "/confluence/rest/api/content/1/child/attachment"
        assert request.url.params["expand"] == "version"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "99",
                        "type": "attachment",
                        "title": "Architecture.svg",
                        "version": {"number": version, "when": f"2026-01-0{version}T00:00:00Z"},
                        "extensions": {"mediaType": "image/svg+xml", "fileSize": 10},
                        "_links": {
                            "download": (
                                f"/download/attachments/1/Architecture.svg?version={version}"
                            )
                        },
                    }
                ],
                "start": 0,
                "limit": 100,
                "size": 1,
            },
        )

    client = ConfluenceClient(source(), "test-pat", transport=httpx.MockTransport(handle))
    try:
        before = client.attachments("1")[0]
        version = 5
        after = client.attachments("1")[0]
        assert (before.id, before.version) != (after.id, after.version)
        assert after.updated_at == "2026-01-05T00:00:00Z"
        assert (
            after.download_url
            == "https://docs.example.com/confluence/download/attachments/1/Architecture.svg?version=5"
        )
        assert after.media_type == "image/svg+xml" and after.size == 10
    finally:
        client.close()


def test_cloud_attachment_uses_supported_versioned_download_and_strips_cdn_auth():
    requested = []
    expected_auth = "Basic " + base64.b64encode(b"reader@example.com:test-token").decode()

    def handle(request):
        requested.append(request)
        if request.url.host == "api.media.atlassian.com":
            assert "Authorization" not in request.headers
            return httpx.Response(200, content=b"image-content")
        assert request.headers["Authorization"] == expected_auth
        if request.url.path == "/wiki/api/v2/pages/1/attachments":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "att99",
                            "title": "Diagram.png",
                            "mediaType": "image/png",
                            "fileSize": 13,
                            "version": {"number": 7, "createdAt": "2026-01-07T00:00:00Z"},
                            "downloadLink": "/download/attachments/1/Diagram.png?api=v2",
                        }
                    ]
                },
            )
        assert request.url.path == "/wiki/rest/api/content/1/child/attachment/att99/download"
        assert request.url.params["version"] == "7"
        return httpx.Response(
            302, headers={"Location": "https://api.media.atlassian.com/file/id/binary?token=signed"}
        )

    client = ConfluenceClient(
        source(cloud=True), "test-token", retries=0, transport=httpx.MockTransport(handle)
    )
    try:
        attachment = client.attachments("1")[0]
        assert client.download(attachment) == b"image-content"
        assert len(requested) == 3
    finally:
        client.close()


def test_cloud_current_and_historical_page_and_versions_use_v2_contract():
    requested = []

    def handle(request):
        requested.append(request)
        if request.url.path.endswith("/versions"):
            return httpx.Response(200, json={"results": [{"number": 4}, {"number": 2}]})
        assert request.url.path == "/wiki/api/v2/pages/1"
        assert request.url.params["body-format"] == "storage"
        version = int(request.url.params.get("version", 4))
        return httpx.Response(
            200,
            json={
                "id": "1",
                "title": "Page",
                "parentId": "9",
                "version": {"number": version, "createdAt": f"2026-01-0{version}T00:00:00Z"},
                "body": {"storage": {"value": f"<p>version {version}</p>"}},
                "_links": {"webui": "/spaces/DOC/pages/1/Page"},
            },
        )

    client = ConfluenceClient(
        source(cloud=True), "test-token", transport=httpx.MockTransport(handle)
    )
    try:
        assert client.versions("1") == [4, 2]
        assert client.page("1").version == 4
        old = client.page("1", version=2)
        assert old.version == 2 and old.body == "<p>version 2</p>"
        assert old.url == "https://team.atlassian.net/wiki/spaces/DOC/pages/1/Page"
        assert len(requested) == 3
    finally:
        client.close()


@pytest.mark.parametrize(
    "destination",
    [
        "https://evil.example/steal",
        "http://api.media.atlassian.com/file",
        "https://api.media.atlassian.com.evil.example/file",
    ],
)
def test_cloud_download_rejects_untrusted_redirect_before_sending_credentials(destination):
    visited = []

    def handle(request):
        visited.append(request)
        return httpx.Response(302, headers={"Location": destination})

    client = ConfluenceClient(
        source(cloud=True), "test-token", transport=httpx.MockTransport(handle)
    )
    try:
        with pytest.raises(ConfluenceError, match="untrusted"):
            client.download(
                Attachment(
                    id="99",
                    title="a.png",
                    download_url="/rest/api/content/1/child/attachment/99/download",
                )
            )
        assert len(visited) == 1
    finally:
        client.close()
