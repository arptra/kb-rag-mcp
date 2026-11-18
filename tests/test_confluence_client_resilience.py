"""Malformed API records fail safely and branches can be discovered independently."""

from __future__ import annotations

import logging

import httpx
import pytest

from corporate_kb.confluence.client import ConfluenceClient
from corporate_kb.confluence.models import ConfluenceError, SourceConfig


def client_for(payload, *, cloud=False, **overrides):
    config = SourceConfig.model_validate(
        {
            "name": "Resilience",
            "index_id": "index",
            "base_url": "https://team.atlassian.net/wiki"
            if cloud
            else "https://docs.example/confluence",
            "root_page_url": "1",
            "username": "reader@example.com" if cloud else None,
            **overrides,
        }
    )
    return ConfluenceClient(
        config,
        "private-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"id": None},
        {"id": "<private-document>"},
        {"id": True},
        {"version": "<private-document>"},
        {"version": [1]},
        {"version": {"number": None}},
        {"version": {"number": "<private-document>"}},
        {"version": {"number": True}},
        {"version": {"number": 1, "when": {"private": "document"}}},
        {"body": "<private-document>"},
        {"body": {"storage": "<private-document>"}},
        {"body": {"storage": {"value": ["<private-document>"]}}},
        {"_links": ["<private-document>"]},
        {"_links": {"webui": {"private": "document"}}},
        {"_links": {"webui": "https://[invalid"}},
        {"ancestors": "<private-document>"},
        {"ancestors": [None]},
        {"ancestors": [{}]},
        {"parentId": {}},
        {"title": {"private": "document"}},
    ],
)
def test_malformed_page_is_safe_diagnostic_instead_of_attribute_error(patch, caplog):
    client = client_for({"id": "1", "version": {"number": 1}, **patch})
    try:
        with caplog.at_level(logging.WARNING), pytest.raises(ConfluenceError) as error:
            client.page("1")
        assert error.value.error_code == "invalid_response"
        assert "invalid" in str(error.value)
        assert "<private-document>" not in str(error.value) + caplog.text
        assert "private-token" not in str(error.value) + caplog.text
        assert "category=" in caplog.text
    finally:
        client.close()


def test_nullable_optional_page_fields_preserve_supported_response():
    client = client_for(
        {"id": "1", "version": None, "body": None, "ancestors": None, "_links": None}
    )
    try:
        page = client.page("1")
        assert page.id == "1" and page.version == 1
        assert page.body == "" and page.parent_id is None
        assert page.url == "https://docs.example/confluence/pages/viewpage.action?pageId=1"
    finally:
        client.close()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"results": None},
        {"results": [None]},
        {"results": ["<private-document>"]},
        {"results": [], "_links": "<private-document>"},
        {"results": [], "_links": {"next": {"private": "document"}}},
        {"results": [], "_links": {"next": "https://[invalid"}},
        {"results": [], "limit": None},
        {"results": [], "limit": 0},
        {"results": [{"id": "2"}], "limit": 1, "start": {"private": "document"}},
    ],
)
def test_malformed_pagination_cannot_silently_drop_rows_or_finish_tree(payload):
    client = client_for(payload)
    try:
        with pytest.raises(ConfluenceError) as error:
            list(client.children("1"))
        assert error.value.error_code == "invalid_response"
        assert "<private-document>" not in str(error.value)
    finally:
        client.close()


@pytest.mark.parametrize(
    "patch",
    [
        {"id": None},
        {"extensions": "<private-document>"},
        {"_links": ["<private-document>"]},
        {"_links": {"download": {"private": "document"}}},
        {"version": [1]},
        {"version": {"number": None}},
        {"fileSize": {"private": "document"}},
        {"fileSize": -1},
        {"mediaType": ["private-document"]},
    ],
)
def test_malformed_attachment_metadata_is_safe_error(patch):
    client = client_for(
        {"results": [{"id": "att2", "_links": {"download": "/download/att2"}, **patch}]}
    )
    try:
        with pytest.raises(ConfluenceError) as error:
            client.attachments("1")
        assert error.value.error_code == "invalid_response"
        assert "<private-document>" not in str(error.value)
    finally:
        client.close()


def test_nullable_attachment_fields_and_zero_size_remain_supported():
    client = client_for(
        {
            "results": [
                {
                    "id": "att2",
                    "version": None,
                    "extensions": None,
                    "_links": None,
                    "downloadLink": "/download/att2",
                    "fileSize": 0,
                }
            ]
        }
    )
    try:
        attachment = client.attachments("1")[0]
        assert attachment.id == "att2" and attachment.version == 1
        assert attachment.size == 0
    finally:
        client.close()


@pytest.mark.parametrize("number", [None, True, 0, {}, "private-document"])
def test_malformed_history_record_is_safe_error(number):
    client = client_for({"results": [{"number": number}]})
    try:
        with pytest.raises(ConfluenceError) as error:
            client.versions("1")
        assert error.value.error_code == "invalid_response"
    finally:
        client.close()


@pytest.mark.parametrize("result", [None, [None], [{}], [{"id": None}]])
def test_malformed_root_lookup_is_safe_error(result):
    client = client_for(
        {"results": result}, root_page_url="https://docs.example/confluence/display/KB/Root"
    )
    try:
        with pytest.raises(ConfluenceError) as error:
            client.root_id()
        assert error.value.error_code == "invalid_response"
    finally:
        client.close()


@pytest.mark.parametrize("kind", ["page", "folder", "database", "whiteboard", "embed"])
def test_cloud_children_public_method_uses_each_container_endpoint(kind):
    client = client_for({"results": [{"id": "2", "type": "folder"}]}, cloud=True)
    visited = []
    client.http.event_hooks["request"] = [lambda request: visited.append(request.url.path)]
    try:
        assert list(client.children("1", kind=kind)) == [("2", "folder")]
        assert visited == [f"/wiki/api/v2/{kind}s/1/direct-children"]
    finally:
        client.close()


def test_dc_children_returns_ids_and_page_kind_without_walking_descendants():
    client = client_for({"results": [{"id": 2}, {"id": "3", "type": "page"}]})
    visited = []
    client.http.event_hooks["request"] = [lambda request: visited.append(request.url.path)]
    try:
        assert list(client.children("1")) == [("2", "page"), ("3", "page")]
        assert visited == ["/confluence/rest/api/content/1/child/page"]
    finally:
        client.close()


@pytest.mark.parametrize(
    "row",
    [
        {"id": None},
        {"id": "../private"},
        {"id": True},
        {"id": "0"},
        {"id": "2", "type": None},
        {"id": "2", "type": {}},
        {"id": "2", "type": "private-unknown-type"},
    ],
)
def test_children_rejects_invalid_child_id_or_unknown_kind(row):
    client = client_for({"results": [row]}, cloud=True)
    try:
        with pytest.raises(ConfluenceError) as error:
            list(client.children("1"))
        assert error.value.error_code == "invalid_response"
        assert "private" not in str(error.value)
    finally:
        client.close()


@pytest.mark.parametrize("content_id,kind", [("../private", "page"), ("1", "unknown")])
def test_children_rejects_invalid_parent_before_request(content_id, kind):
    client = client_for({"results": []})
    try:
        with pytest.raises(ConfluenceError) as error:
            list(client.children(content_id, kind=kind))
        assert error.value.error_code == "invalid_response"
        assert client._request_number == 0
    finally:
        client.close()
