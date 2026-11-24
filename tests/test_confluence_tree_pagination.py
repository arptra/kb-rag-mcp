"""Direct-child pagination must preserve progress and the server's query options."""

from __future__ import annotations

import httpx
import pytest

from corporate_kb.confluence.client import ConfluenceClient
from corporate_kb.confluence.models import ConfluenceError, SourceConfig


def _client(handler):
    return ConfluenceClient(
        SourceConfig.model_validate(
            {
                "name": "Tree pagination",
                "index_id": "target",
                "base_url": "https://docs.example/confluence",
                "root_page_url": "1",
                "deployment": "data_center",
            }
        ),
        "test-token",
        transport=httpx.MockTransport(handler),
        retries=0,
    )


@pytest.mark.parametrize("include_response_start", [False, True])
def test_dc_offset_fallback_after_next_link_preserves_query_and_position(include_response_start):
    requested = []

    def handle(request):
        requested.append(request)
        start = int(request.url.params.get("start", "0"))
        if len(requested) > 1:
            assert request.url.params.get("limit") == "2"
            assert request.url.params.get("status") == "current"
        assert start == (len(requested) - 1) * 2
        if start == 0:
            return httpx.Response(
                200,
                json={
                    "results": [{"id": "2"}, {"id": "3"}],
                    "start": 0,
                    "limit": 2,
                    "_links": {"next": "?start=2&limit=2&status=current"},
                },
            )
        if start == 2:
            # Some gateways omit next on full pages. Fall back from the effective
            # request URL, even if the response does not repeat its start offset.
            return httpx.Response(
                200,
                json={
                    "results": [{"id": "4"}, {"id": "5"}],
                    "limit": 2,
                    **({"start": 2} if include_response_start else {}),
                },
            )
        assert start == 4
        return httpx.Response(200, json={"results": [{"id": "6"}], "start": 4, "limit": 2})

    client = _client(handle)
    try:
        assert list(client.children("1")) == [(str(number), "page") for number in range(2, 7)]
        assert len(requested) == 3
    finally:
        client.close()


def test_dc_ignoring_offsets_cannot_loop_forever_on_the_same_children():
    requested = []

    def handle(request):
        requested.append(request)
        # Bound the regression itself: distinct requested URLs alone cannot
        # detect a gateway that ignores offsets and repeats the first page.
        assert len(requested) <= 3, "Repeated results caused unbounded child requests"
        return httpx.Response(200, json={"results": [{"id": "2"}, {"id": "3"}], "limit": 2})

    client = _client(handle)
    try:
        with pytest.raises(ConfluenceError, match=r"pagination|progress|repeated") as error:
            list(client.children("1"))
        assert error.value.error_code == "pagination"
        assert len(requested) <= 3
    finally:
        client.close()


def test_overlapping_child_pages_with_new_ids_are_not_treated_as_a_loop():
    def handle(request):
        start = int(request.url.params.get("start", "0"))
        ids = {0: ["2", "3"], 2: ["3", "4"], 4: []}[start]
        return httpx.Response(
            200, json={"results": [{"id": item} for item in ids], "start": start, "limit": 2}
        )

    client = _client(handle)
    try:
        assert list(client.children("1")) == [(item, "page") for item in ["2", "3", "3", "4"]]
    finally:
        client.close()


def test_repeated_children_with_changed_metadata_still_fail_without_progress():
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        assert count <= 3
        return httpx.Response(
            200,
            json={
                "results": [{"id": "2", "title": f"Updated title {count}"}],
                "limit": 1,
                "_links": {"next": f"?start={count}&limit=1"},
            },
        )

    client = _client(handle)
    try:
        with pytest.raises(ConfluenceError, match="no progress") as error:
            list(client.children("1"))
        assert error.value.error_code == "pagination"
        assert count == 2
    finally:
        client.close()


def test_full_page_with_inconsistent_start_is_not_silently_skipped():
    def handle(request):
        return httpx.Response(200, json={"results": [{"id": "2"}], "start": 3, "limit": 1})

    client = _client(handle)
    try:
        with pytest.raises(ConfluenceError, match="unexpected start offset") as error:
            list(client.children("1"))
        assert error.value.error_code == "pagination"
    finally:
        client.close()


def test_dc_paginated_child_branch_reaches_its_deep_descendants():
    requested = []

    def handle(request):
        parent = request.url.path.split("/")[-3]
        start = int(request.url.params.get("start", "0"))
        requested.append((parent, start))
        if parent == "2":
            if start:
                assert request.url.params.get("limit") == "2"
            ids = {0: ["4", "5"], 2: ["6", "7"], 4: ["8"]}[start]
            return httpx.Response(
                200,
                json={
                    "results": [{"id": item} for item in ids],
                    "limit": 2,
                    **({"_links": {"next": "?start=2&limit=2"}} if not start else {}),
                },
            )
        ids = {"1": ["2", "3"], "8": ["9"], "9": ["10"]}.get(parent, [])
        return httpx.Response(200, json={"results": [{"id": item} for item in ids], "limit": 100})

    client = _client(handle)
    try:
        assert set(client.page_ids("1")) == {str(number) for number in range(1, 11)}
        assert ("2", 4) in requested
        assert ("10", 0) in requested
    finally:
        client.close()
