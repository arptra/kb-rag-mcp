"""Exercise tree discovery through the real HTTP client and durable sync queue."""

from __future__ import annotations

import json
import re
from collections import Counter

import httpx
import pytest

from corporate_kb.confluence.client import ConfluenceClient
from corporate_kb.confluence.models import SourceConfig
from corporate_kb.confluence.registry import ConfluenceRegistry


class TreeApi:
    def __init__(self, *, cloud: bool, depth: int = 24):
        self.cloud = cloud
        self.depth = depth
        self.version = 1
        self.failures: Counter[str] = Counter()
        self.child_calls: list[str] = []
        self.page_calls: list[str] = []
        self.parents = {str(number): str(number - 1) for number in range(2, depth + 2)}
        self.parents.update({"100": "1", "101": "1", "102": "1", "103": "102"})
        self.tree = {str(number): [str(number + 1)] for number in range(1, depth + 1)}
        # Multiple API pages, repeated IDs and a cycle must not hide deeper descendants.
        self.tree["1"] += ["100", "2", "101", "102"]
        self.tree["102"] = ["103"]
        self.tree[str(depth + 1)] = ["1"]
        self.folders = {"8"} if cloud else set()

    @property
    def page_ids(self):
        return ({"1"} | set(self.parents)) - self.folders

    @property
    def base(self):
        return "https://team.atlassian.net/wiki" if self.cloud else "https://docs.example/confluence"

    def config(self):
        return SourceConfig(
            name="Deep documentation tree",
            index_id="target",
            base_url=self.base,
            root_page_url="1",
            username="reader@example.com" if self.cloud else None,
            deployment="cloud" if self.cloud else "data_center",
            analyze_images=False,
            schedule={"kind": "manual"},
        )

    def __call__(self, request: httpx.Request):
        path = request.url.path
        match = re.fullmatch(
            r"/wiki/api/v2/(?:pages|folders)/(\d+)/direct-children"
            if self.cloud else r"/confluence/rest/api/content/(\d+)/child/page",
            path,
        )
        if match:
            page_id = match[1]
            self.child_calls.append(page_id)
            if self.failures[page_id]:
                self.failures[page_id] -= 1
                return httpx.Response(503)
            offset_key = "cursor" if self.cloud else "start"
            offset = int(request.url.params.get(offset_key, 0))
            children = self.tree.get(page_id, [])
            rows = children[offset:offset + 2]
            payload = {
                "results": [{"id": child, "type": "folder" if child in self.folders else "page"}
                            for child in rows],
                "start": offset,
                "limit": 2,
                "size": len(rows),
            }
            if offset + len(rows) < len(children):
                payload["_links"] = {"next": f"{path}?{offset_key}={offset + 2}&limit=2"}
            return httpx.Response(200, json=payload)
        if path.endswith("/attachments") or path.endswith("/child/attachment"):
            return httpx.Response(200, json={"results": []})
        match = re.fullmatch(
            r"/wiki/api/v2/pages/(\d+)"
            if self.cloud else r"/confluence/rest/api/content/(\d+)",
            path,
        )
        assert match, f"Unexpected API request: {request.url}"
        page_id = match[1]
        assert page_id in self.page_ids
        self.page_calls.append(page_id)
        parent = self.parents.get(page_id)
        return httpx.Response(200, json={
            "id": page_id,
            "title": f"Page {page_id}",
            "version": {"number": self.version, "when": f"2026-01-0{self.version}T00:00:00Z"},
            "ancestors": [{"id": parent}] if parent else [],
            "body": {"storage": {"value": f"<p>Page {page_id} revision {self.version}</p>"}},
            "_links": {"webui": f"/pages/viewpage.action?pageId={page_id}"},
        })


def registry_for(tmp_path, api: TreeApi):
    published, visibility = [], []
    registry = ConfluenceRegistry(
        tmp_path / "state",
        index_exists=lambda index: index == "target",
        knowledge_root=lambda index: tmp_path / "knowledge" / index,
        publish=lambda index, **kwargs: published.append(index) or {"status": "completed"},
        page_visibility=lambda *args: visibility.append(args),
        client_factory=lambda config, token: ConfluenceClient(
            config, token, transport=httpx.MockTransport(api), retries=0,
        ),
    )
    source = registry.upsert_source(api.config().model_dump(), "test-token")
    return registry, source, published, visibility


def synchronize(registry, source):
    return registry.run_job(registry.queue_sync(source["id"])["id"])


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(ConfluenceRegistry, "RETRY_DELAY_SECONDS", 0)


@pytest.mark.parametrize("cloud", [False, True], ids=["data-center", "cloud"])
def test_sync_walks_every_depth_paginated_sibling_and_container(tmp_path, cloud):
    api = TreeApi(cloud=cloud)
    registry, source, published, _ = registry_for(tmp_path, api)

    job = synchronize(registry, source)

    assert job["status"] == "completed"
    assert job["result"]["discovery_complete"] is True
    assert job["result"]["discovered"] == len(api.page_ids)
    assert {page["page_id"] for page in registry.source_pages(source["id"])} == api.page_ids
    assert set(api.child_calls) == api.page_ids | api.folders
    assert api.child_calls.count("1") == 3  # Root pagination is fully consumed once.
    assert set(api.page_calls) == api.page_ids
    for page_id in ("25", "103"):
        text = registry._current_path("target", source["id"], page_id).read_text()
        assert f"Page {page_id} revision 1" in text
    assert published == ["target"]
    events = registry.job_log(job["id"], limit=1000)["items"]
    startup = next(event for event in events if "depth_limit" in event["details"])
    assert startup["details"]["depth_limit"] is None
    branches = {
        event["details"]["page_id"]: event["details"]
        for event in events if event["message"] == "Page tree branch checked"
    }
    assert branches["1"]["children_found"] == 5
    assert branches["1"]["new_branches"] == 4  # Duplicate child ID is not new work.
    assert branches["15"]["children_found"] == branches["15"]["new_branches"] == 1
    assert branches["25"]["children_found"] == 1
    assert branches["25"]["new_branches"] == 0  # The link back to the root is a cycle.
    final = next(event for event in events if event["message"] == "Page tree discovery completed")
    assert final["details"]["completed_branches"] == len(api.page_ids | api.folders)
    assert final["details"]["branches_remaining"] == 0
    assert "test-token" not in json.dumps(events)


@pytest.mark.parametrize("cloud", [False, True], ids=["data-center", "cloud"])
def test_unchanged_parent_is_walked_again_to_find_a_new_deeper_page(tmp_path, cloud):
    api = TreeApi(cloud=cloud)
    registry, source, published, _ = registry_for(tmp_path, api)
    assert synchronize(registry, source)["status"] == "completed"
    previous_pages = api.page_ids
    api.tree["25"].append("26")
    api.parents["26"] = "25"
    api.child_calls.clear()
    api.page_calls.clear()

    job = synchronize(registry, source)

    assert job["status"] == "completed"
    assert job["result"]["discovery_complete"] is True
    assert job["result"]["changed"] == 1
    assert job["result"]["unchanged"] == len(previous_pages)
    pages = {page["page_id"]: page for page in registry.source_pages(source["id"])}
    assert set(pages) == previous_pages | {"26"}
    assert pages["25"]["version"] == pages["26"]["version"] == 1
    assert pages["26"]["parent_id"] == "25"
    assert {"25", "26"} <= set(api.child_calls)
    # Existing pages need metadata only; only the new descendant needs its body.
    assert all(api.page_calls.count(page_id) == 1 for page_id in previous_pages)
    assert api.page_calls.count("26") == 2
    assert "Page 26 revision 1" in registry._current_path(
        "target", source["id"], "26"
    ).read_text()
    assert published == ["target", "target"]


@pytest.mark.parametrize("cloud", [False, True], ids=["data-center", "cloud"])
def test_retrying_a_deep_branch_discovers_its_remaining_descendants(tmp_path, cloud):
    api = TreeApi(cloud=cloud)
    api.failures["15"] = 1
    registry, source, published, _ = registry_for(tmp_path, api)

    job = synchronize(registry, source)

    assert job["status"] == "completed"
    assert api.child_calls.count("15") == 2
    retry_index = len(api.child_calls) - 1 - api.child_calls[::-1].index("15")
    assert api.child_calls.index("103") < retry_index
    assert {page["page_id"] for page in registry.source_pages(source["id"])} == api.page_ids
    assert job["result"]["discovery_complete"] is True
    assert registry.job_failures(job["id"])["items"] == []
    assert published == ["target"]


@pytest.mark.parametrize("cloud", [False, True], ids=["data-center", "cloud"])
def test_inaccessible_deep_branch_is_reported_and_old_descendants_are_retained(tmp_path, cloud):
    api = TreeApi(cloud=cloud)
    registry, source, published, visibility = registry_for(tmp_path, api)
    assert synchronize(registry, source)["status"] == "completed"
    api.version = 2
    api.failures["15"] = 20
    api.child_calls.clear()
    visibility.clear()

    job = synchronize(registry, source)

    assert job["status"] == "completed_with_warnings"
    assert job["result"]["discovery_complete"] is False
    assert api.child_calls.count("15") == 3
    pages = {page["page_id"]: page for page in registry.source_pages(source["id"])}
    assert set(pages) == api.page_ids
    assert pages["25"]["version"] == 1 and pages["25"]["state"] == "current"
    assert pages["103"]["version"] == 2 and pages["103"]["state"] == "current"
    assert all(is_visible for _, _, is_visible in visibility)
    failures = registry.job_failures(job["id"])["items"]
    assert len(failures) == 1
    assert failures[0]["page_id"] == "15"
    assert failures[0]["task_kind"] == "discovery"
    assert failures[0]["attempts"] == 3 and failures[0]["status_code"] == 503
    assert published == ["target", "target"]
    events = registry.job_log(job["id"], limit=1000)["items"]
    incomplete = [event for event in events if event["message"].startswith(
        "Page tree branch incomplete"
    )]
    assert len(incomplete) == 3
    assert all(event["details"]["page_id"] == "15" for event in incomplete)
    assert all(event["details"]["branches_remaining"] >= 1 for event in incomplete)
    final = next(
        event for event in events if event["message"].startswith("Page tree is incomplete")
    )
    assert final["details"]["discovery_complete"] is False
    assert final["details"]["branches_remaining"] == 1
    assert final["details"]["completed_branches"] == len(api.page_ids | api.folders) - 11
    assert "test-token" not in json.dumps(events)


@pytest.mark.parametrize("cloud", [False, True], ids=["data-center", "cloud"])
def test_client_walks_beyond_python_recursion_limit_without_recursive_calls(cloud):
    depth = 1100
    api = TreeApi(cloud=cloud, depth=depth)
    # The long chain uses IDs also used by the short-tree sibling fixtures.
    api.parents = {str(number): str(number - 1) for number in range(2, depth + 2)}
    api.tree = {str(number): [str(number + 1)] for number in range(1, depth + 1)}
    api.tree[str(depth + 1)] = ["1"]
    client = ConfluenceClient(api.config(), "test-token", transport=httpx.MockTransport(api))
    try:
        pages = list(client.page_ids("1"))
    finally:
        client.close()

    assert set(pages) == api.page_ids
    assert len(pages) == len(set(pages))
    assert len(api.child_calls) == depth + 1
