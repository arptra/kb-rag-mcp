"""Bounded Cloud v2 / Data Center REST client with credential-safe pagination."""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import httpx

from corporate_kb.confluence.models import (
    Attachment,
    ConfluenceError,
    Page,
    SourceConfig,
    SyncCancelled,
)


class ConfluenceClient:
    def __init__(
        self,
        config: SourceConfig,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        cancel: threading.Event | None = None,
        retries: int = 3,
    ) -> None:
        self.config = config
        self.cancel = cancel or threading.Event()
        self.retries = retries
        parsed = urlsplit(config.base_url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.cloud = config.effective_cloud
        base = config.base_url
        if self.cloud and not urlsplit(base).path.rstrip("/"):
            base += "/wiki"
        self.base = base.rstrip("/")
        self.api = self.base + ("/api/v2" if self.cloud else "/rest/api")
        headers = {"Accept": "application/json"}
        auth = None
        if config.username:
            auth = httpx.BasicAuth(config.username, token)
        else:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.Client(
            headers=headers,
            auth=auth,
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
            transport=transport,
        )
        self._download_http = httpx.Client(
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self.http.close()
        self._download_http.close()

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise SyncCancelled("Confluence synchronization cancelled")

    def _trusted_url(self, url: str) -> str:
        context = urlsplit(self.base).path.rstrip("/")
        if (
            url.startswith("/")
            and not url.startswith("//")
            and context
            and not url.startswith(context + "/")
            and url != context
        ):
            url = context + url
        resolved = urljoin(self.base + "/", url)
        parsed, original = urlsplit(resolved), urlsplit(self.origin)
        if (
            (parsed.scheme, parsed.hostname, parsed.port)
            != (original.scheme, original.hostname, original.port)
            or parsed.username
            or parsed.password
        ):
            raise ConfluenceError("Confluence returned an untrusted cross-origin URL")
        return resolved

    def _request(self, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        target = self._trusted_url(url)
        for attempt in range(self.retries + 1):
            self._check_cancel()
            try:
                response = self.http.get(target, params=params)
            except httpx.HTTPError:
                if attempt >= self.retries:
                    raise ConfluenceError("Confluence network request failed") from None
                self._wait(min(2**attempt, 15))
                continue
            if response.status_code in {429, 502, 503, 504} and attempt < self.retries:
                self._wait(self._retry_delay(response, attempt))
                continue
            if response.is_redirect:
                # Do not follow a login redirect or forward service credentials to a CDN.
                raise ConfluenceError(
                    "Confluence redirected the API request; verify API URL and token"
                )
            if response.is_error:
                labels = {
                    401: "Authentication failed",
                    403: "Page access denied",
                    404: "Page unavailable",
                }
                raise ConfluenceError(
                    labels.get(response.status_code, f"Confluence HTTP {response.status_code}"),
                    status_code=response.status_code,
                )
            return response
        raise ConfluenceError("Confluence request exhausted retries")

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        value = response.headers.get("Retry-After", "")
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = float(2**attempt)
        return max(0, min(seconds, 120))

    def _wait(self, seconds: float) -> None:
        if self.cancel.wait(seconds):
            raise SyncCancelled("Confluence synchronization cancelled")

    def _json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._request(url, params=params)
        try:
            result = response.json()
        except ValueError:
            raise ConfluenceError("Confluence returned invalid JSON") from None
        if not isinstance(result, dict):
            raise ConfluenceError("Confluence returned an unexpected response")
        return result

    def _paginate(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        seen: set[str] = set()
        while url:
            self._check_cancel()
            identity = url + repr(params)
            if identity in seen:
                raise ConfluenceError("Confluence pagination loop detected")
            seen.add(identity)
            response = self._request(url, params=params)
            try:
                payload = response.json()
                rows = payload["results"]
                if not isinstance(rows, list):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise ConfluenceError("Confluence returned invalid pagination data") from None
            for row in rows:
                if isinstance(row, dict):
                    yield row
            next_url = (payload.get("_links") or {}).get("next")
            if not next_url:
                next_url = response.links.get("next", {}).get("url")
            # DC may omit next but provide start/limit/size; never guess on Cloud cursor APIs.
            if not next_url and not self.cloud and len(rows) == int(payload.get("limit", 100)):
                start = int(payload.get("start", (params or {}).get("start", 0)))
                params = {**(params or {}), "start": start + len(rows)}
                continue
            if next_url:
                next_text = str(next_url)
                url = self._trusted_url(
                    next_text
                    if next_text.startswith("/")
                    else urljoin(str(response.url), next_text)
                )
                params = None
            else:
                url = ""

    def root_id(self) -> str:
        value = self.config.root_page_url
        if value.isdigit():
            return value
        parsed = urlsplit(value)
        query_id = parse_qs(parsed.query).get("pageId", [None])[0]
        if query_id and query_id.isdigit():
            return query_id
        match = re.search(r"/pages/(\d+)(?:/|$)", parsed.path)
        if match:
            return match.group(1)
        match = re.search(r"/display/([^/]+)/(.+)", parsed.path)
        if match:
            params = {
                "spaceKey": unquote(match.group(1)),
                "title": unquote(match.group(2)).replace("+", " "),
                "limit": 2,
            }
            result = self._json(self.base + "/rest/api/content", params=params).get("results", [])
            if len(result) == 1:
                return str(result[0]["id"])
        raise ConfluenceError("Use a root page URL containing /pages/<id> or pageId=<id>")

    def page_ids(self, root_id: str) -> Iterator[str]:
        yield root_id
        seen = {root_id}
        if self.cloud:
            # The descendants API defaults to depth=2 and caps depth at 10. Walk
            # direct children instead, including containers that can contain pages.
            collections = {
                "page": "pages",
                "folder": "folders",
                "database": "databases",
                "whiteboard": "whiteboards",
                "embed": "embeds",
            }
            pending_cloud = [("page", root_id)]
            while pending_cloud:
                kind, parent = pending_cloud.pop()
                for item in self._paginate(
                    f"{self.api}/{collections[kind]}/{parent}/direct-children",
                    params={"limit": 100},
                ):
                    child_kind = item.get("type", "page")
                    if child_kind not in collections:
                        raise ConfluenceError(
                            "Confluence returned an unsupported content type; "
                            "tree discovery is incomplete"
                        )
                    child_id = str(item["id"])
                    if child_id in seen:
                        continue
                    seen.add(child_id)
                    # Bound the whole traversal, including non-page containers.
                    if len(seen) > self.config.max_pages:
                        raise ConfluenceError("Page limit exceeded; tree discovery is incomplete")
                    pending_cloud.append((child_kind, child_id))
                    if child_kind == "page":
                        yield child_id
        else:
            pending = [root_id]
            while pending:
                parent = pending.pop()
                for item in self._paginate(
                    f"{self.api}/content/{parent}/child/page", params={"limit": 100}
                ):
                    page_id = str(item["id"])
                    if page_id in seen:
                        continue
                    seen.add(page_id)
                    if len(seen) > self.config.max_pages:
                        raise ConfluenceError("Page limit exceeded; tree discovery is incomplete")
                    pending.append(page_id)
                    yield page_id

    def page(self, page_id: str, *, body: bool = True, version: int | None = None) -> Page:
        params: dict[str, Any] = {}
        if self.cloud:
            endpoint = f"{self.api}/pages/{page_id}"
            if body:
                params["body-format"] = "storage"
        else:
            endpoint = f"{self.api}/content/{page_id}"
            params["expand"] = "version,ancestors" + (",body.storage" if body else "")
            if version is not None:
                params["status"] = "historical"
        if version is not None:
            params["version"] = version
        item = self._json(endpoint, params=params)
        version_data = item.get("version") or {}
        ancestors = item.get("ancestors") or []
        links = item.get("_links") or {}
        body_data = item.get("body") or {}
        storage = body_data.get("storage") or {}
        return Page(
            id=str(item["id"]),
            title=str(item.get("title", page_id)),
            version=int(version_data.get("number", version or 1)),
            updated_at=version_data.get("createdAt") or version_data.get("when"),
            parent_id=str(item["parentId"])
            if item.get("parentId")
            else (str(ancestors[-1]["id"]) if ancestors else None),
            url=self._trusted_url(links.get("webui", f"pages/viewpage.action?pageId={page_id}")),
            body=str(storage.get("value", "")),
        )

    def attachments(self, page_id: str) -> list[Attachment]:
        endpoint = (
            f"{self.api}/pages/{page_id}/attachments"
            if self.cloud
            else f"{self.api}/content/{page_id}/child/attachment"
        )
        result = []
        params: dict[str, Any] = {"limit": 100}
        if not self.cloud:
            params["expand"] = "version"
        for item in self._paginate(endpoint, params=params):
            extension = item.get("extensions") or {}
            links = item.get("_links") or {}
            version_data = item.get("version") or {}
            version = int(version_data.get("number", 1))
            download: str | None
            if self.cloud:
                # downloadLink may point to a retired browser download route.
                # The supported REST endpoint supplies the signed media redirect.
                attachment_id = quote(str(item["id"]), safe="")
                download = (
                    f"{self.base}/rest/api/content/{quote(page_id, safe='')}/child/attachment/"
                    f"{attachment_id}/download?version={version}"
                )
            else:
                download = item.get("downloadLink") or links.get("download")
            if not download:
                continue
            result.append(
                Attachment(
                    id=str(item["id"]),
                    title=str(item.get("title", item["id"])),
                    version=version,
                    media_type=str(
                        item.get("mediaType")
                        or extension.get("mediaType")
                        or "application/octet-stream"
                    ),
                    size=item.get("fileSize") or extension.get("fileSize"),
                    updated_at=version_data.get("createdAt") or version_data.get("when"),
                    download_url=self._trusted_url(str(download)),
                )
            )
        return result

    def versions(self, page_id: str) -> list[int]:
        endpoint = (
            f"{self.api}/pages/{page_id}/versions"
            if self.cloud
            else f"{self.base}/rest/experimental/content/{page_id}/version"
        )
        try:
            return sorted(
                {int(item["number"]) for item in self._paginate(endpoint, params={"limit": 100})},
                reverse=True,
            )
        except ConfluenceError as exc:
            if not self.cloud and exc.status_code in {404, 405, 501}:
                raise ConfluenceError(
                    "Confluence Data Center history API is unavailable "
                    "(/rest/experimental/content/{id}/version); "
                    "verify server support or disable importing existing history",
                    status_code=exc.status_code,
                ) from None
            raise

    def download(self, attachment: Attachment) -> bytes:
        if attachment.size is not None and attachment.size > self.config.max_attachment_bytes:
            raise ConfluenceError("Attachment exceeds configured size limit")
        target = self._trusted_url(attachment.download_url)
        # Cloud media uses signed URLs. Only its explicit media domain is accepted;
        # a separate client ensures Confluence Basic/Bearer credentials never leave the origin.
        client = self.http
        redirects = 0
        attempt = 0
        while True:
            self._check_cancel()
            try:
                with client.stream("GET", target, headers={"Accept": "*/*"}) as response:
                    if response.is_redirect:
                        redirects += 1
                        if redirects > 3:
                            raise ConfluenceError("Attachment redirect limit exceeded")
                        destination = urljoin(target, response.headers.get("Location", ""))
                        parsed = urlsplit(destination)
                        if f"{parsed.scheme}://{parsed.netloc}" == self.origin:
                            target, client = self._trusted_url(destination), self.http
                        elif (
                            self.cloud
                            and parsed.scheme == "https"
                            and parsed.hostname is not None
                            and (
                                parsed.hostname == "api.media.atlassian.com"
                                or parsed.hostname.endswith(".media.atlassian.com")
                            )
                            and not parsed.username
                            and not parsed.password
                            and parsed.port in {None, 443}
                        ):
                            target, client = destination, self._download_http
                        else:
                            raise ConfluenceError("Attachment redirected to an untrusted origin")
                        continue
                    if response.status_code in {429, 502, 503, 504} and attempt < self.retries:
                        self._wait(self._retry_delay(response, attempt))
                        attempt += 1
                        continue
                    if response.is_error:
                        raise ConfluenceError(
                            f"Attachment HTTP {response.status_code}",
                            status_code=response.status_code,
                        )
                    chunks, size = [], 0
                    for chunk in response.iter_bytes(64 * 1024):
                        self._check_cancel()
                        size += len(chunk)
                        if size > self.config.max_attachment_bytes:
                            raise ConfluenceError("Attachment exceeds configured size limit")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except httpx.HTTPError:
                if attempt >= self.retries:
                    raise ConfluenceError("Attachment network request failed") from None
                self._wait(min(2**attempt, 15))
                attempt += 1
