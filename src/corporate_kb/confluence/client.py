"""Bounded Cloud v2 / Data Center REST client with credential-safe pagination."""

from __future__ import annotations

import logging
import os
import re
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import httpx

from corporate_kb.confluence.diagnostics import network_error, safe_http_logging
from corporate_kb.confluence.models import (
    Attachment,
    ConfluenceError,
    Page,
    SourceConfig,
    SyncCancelled,
)

logger = logging.getLogger(__name__)

_CLOUD_COLLECTIONS = {
    "page": "pages",
    "folder": "folders",
    "database": "databases",
    "whiteboard": "whiteboards",
    "embed": "embeds",
}


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
        self.trace_id = uuid.uuid4().hex[:12]
        self._request_number = 0
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
        if config.auth_mode == "basic":
            auth = httpx.BasicAuth(config.username or "", token)
        else:
            headers["Authorization"] = f"Bearer {token}"
        logger.info(
            "Confluence client configuration: trace=%s origin=%s deployment=%s auth=%s "
            "tls_verify=true ca=%s proxy_env=%s no_proxy_env=%s "
            "connect_timeout=10s read_timeout=30s retries=%d",
            self.trace_id,
            self.origin,
            "cloud" if self.cloud else "data_center",
            config.auth_mode,
            "SSL_CERT_FILE"
            if os.environ.get("SSL_CERT_FILE")
            else ("SSL_CERT_DIR" if os.environ.get("SSL_CERT_DIR") else "default"),
            any(
                os.environ.get(key)
                for key in (
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                )
            ),
            bool(os.environ.get("NO_PROXY") or os.environ.get("no_proxy")),
            retries,
        )
        try:
            self.http = httpx.Client(
                headers=headers,
                auth=auth,
                timeout=httpx.Timeout(30, connect=10),
                follow_redirects=False,
                transport=transport,
            )
            try:
                self._download_http = httpx.Client(
                    timeout=httpx.Timeout(30, connect=10),
                    follow_redirects=False,
                    transport=transport,
                )
            except Exception:
                self.http.close()
                raise
        except (OSError, ValueError) as exc:
            # CA files and proxy URLs may fail before the first request. Keep
            # private filesystem paths and proxy credentials out of the response.
            logger.warning("Confluence HTTP client setup failed: %s", type(exc).__name__)
            raise ConfluenceError(
                "Confluence HTTP client configuration failed. Check the exported "
                "SSL_CERT_FILE/SSL_CERT_DIR and HTTP_PROXY/HTTPS_PROXY/ALL_PROXY settings "
                "of the backend process.",
                error_code="client_configuration",
            ) from None

    def close(self) -> None:
        with safe_http_logging():
            self.http.close()
            self._download_http.close()
        logger.info(
            "Confluence client closed: trace=%s requests=%d", self.trace_id, self._request_number
        )

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
        try:
            resolved = urljoin(self.base + "/", url)
            parsed, original = urlsplit(resolved), urlsplit(self.origin)
            trusted_origin = (parsed.scheme, parsed.hostname, parsed.port) == (
                original.scheme,
                original.hostname,
                original.port,
            )
        except ValueError:
            raise self._invalid_response("URL", "link") from None
        if (
            not trusted_origin
            or parsed.username
            or parsed.password
        ):
            raise ConfluenceError("Confluence returned an untrusted cross-origin URL")
        return resolved

    def _request(self, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        target = self._trusted_url(url)
        for attempt in range(self.retries + 1):
            self._check_cancel()
            request_id, started = self._request_started(target, attempt)
            try:
                with safe_http_logging():
                    response = self.http.get(target, params=params)
            except httpx.HTTPError as exc:
                failure = self._network_failure(exc, target, attempt, elapsed=monotonic() - started)
                if attempt >= self.retries:
                    raise failure from None
                self._retry_wait(min(2**attempt, 15), failure.error_code or "network")
                continue
            self._response_received(request_id, response, started)
            if response.status_code in {429, 502, 503, 504} and attempt < self.retries:
                self._retry_wait(
                    self._retry_delay(response, attempt), f"HTTP {response.status_code}"
                )
                continue
            if response.is_redirect:
                logger.warning(
                    "Confluence API redirect rejected: trace=%s status=%d; check API URL or SSO",
                    self.trace_id,
                    response.status_code,
                )
                # Do not follow a login redirect or forward service credentials to a CDN.
                raise ConfluenceError(
                    "Confluence redirected the API request; verify API URL and token"
                )
            if response.is_error:
                logger.warning(
                    "Confluence HTTP request failed: origin=%s auth=%s status=%d",
                    self.origin,
                    self.config.auth_mode,
                    response.status_code,
                )
                if response.status_code == 401:
                    hint = (
                        "Check the Confluence personal access token (PAT), its expiry and "
                        "whether the gateway forwards Bearer authentication. Enter only the "
                        "token value, without the Bearer prefix."
                        if self.config.auth_mode == "bearer"
                        else "Check the account email/username and token. For a Data Center "
                        "personal access token select Data Center to use Bearer authentication."
                    )
                    raise ConfluenceError(
                        "Authentication failed: HTTP 401 from Confluence or its gateway "
                        f"(auth={self.config.auth_mode}, "
                        f"deployment={'cloud' if self.cloud else 'data_center'}, "
                        f"request={request_id}). {hint}",
                        status_code=401,
                        error_code="authentication",
                    )
                labels = {
                    403: "Page access denied",
                    404: "Page unavailable",
                }
                raise ConfluenceError(
                    labels.get(response.status_code, f"Confluence HTTP {response.status_code}"),
                    status_code=response.status_code,
                )
            return response
        raise ConfluenceError("Confluence request exhausted retries")

    def _request_started(
        self, target: str, attempt: int, *, attachment: bool = False
    ) -> tuple[str, float]:
        self._request_number += 1
        request_id = f"{self.trace_id}/{self._request_number}"
        parsed = urlsplit(target)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        # API paths help diagnose wrong context prefixes. CDN paths/queries may
        # themselves be signed; only their origin belongs in diagnostics.
        endpoint = origin + (
            parsed.path if origin == self.origin and not attachment else "/<attachment>"
        )
        logger.info(
            "Confluence HTTP request started: request=%s method=GET endpoint=%s "
            "operation=%s attempt=%d/%d",
            request_id,
            endpoint,
            "attachment" if attachment else "api",
            attempt + 1,
            self.retries + 1,
        )
        return request_id, monotonic()

    @staticmethod
    def _response_received(request_id: str, response: httpx.Response, started: float) -> None:
        logger.info(
            "Confluence HTTP response received: request=%s status=%d elapsed=%.3fs",
            request_id,
            response.status_code,
            monotonic() - started,
        )

    def _retry_wait(self, seconds: float, reason: str) -> None:
        logger.info(
            "Confluence retry scheduled: trace=%s reason=%s wait=%.1fs",
            self.trace_id,
            reason,
            seconds,
        )
        self._wait(seconds)

    def _network_failure(
        self,
        exc: httpx.HTTPError,
        target: str,
        attempt: int,
        *,
        attachment: bool = False,
        elapsed: float = 0,
    ) -> ConfluenceError:
        failure = network_error(exc, attachment=attachment)
        parsed = urlsplit(target)
        # No path/query: attachment redirects can contain signed credentials.
        origin = f"{parsed.scheme}://{parsed.netloc}"
        auth = self.config.auth_mode if origin == self.origin else "none"
        logger.warning(
            "Confluence request failed: trace=%s operation=%s origin=%s auth=%s attempt=%d/%d "
            "code=%s elapsed=%.3fs; %s",
            self.trace_id,
            "attachment" if attachment else "api",
            origin,
            auth,
            attempt + 1,
            self.retries + 1,
            failure.error_code,
            elapsed,
            failure,
        )
        return failure

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

    def _invalid_response(self, category: str, field: str) -> ConfluenceError:
        # Never interpolate API values here: malformed fields can contain document
        # content, signed URLs, or secrets returned by a gateway instead of JSON.
        logger.warning(
            "Confluence invalid API response: trace=%s category=%s field=%s",
            self.trace_id,
            category,
            field,
        )
        return ConfluenceError(
            f"Confluence returned invalid {category} response ({field}); "
            "this item could not be read safely",
            error_code="invalid_response",
        )

    def _mapping(self, value: Any, category: str, field: str) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise self._invalid_response(category, field)
        return value

    def _integer(self, value: Any, category: str, field: str, *, minimum: int = 1) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise self._invalid_response(category, field)
        try:
            result = int(value)
        except ValueError:
            raise self._invalid_response(category, field) from None
        if result < minimum:
            raise self._invalid_response(category, field)
        return result

    def _content_id(self, value: Any, category: str, field: str = "id") -> str:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise self._invalid_response(category, field)
        result = str(value)
        if not re.fullmatch(r"[0-9]+", result) or not result.lstrip("0"):
            raise self._invalid_response(category, field)
        return result

    def _optional_text(self, value: Any, category: str, field: str) -> str | None:
        if value is not None and not isinstance(value, str):
            raise self._invalid_response(category, field)
        return value

    def _json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        category: str = "JSON",
    ) -> dict[str, Any]:
        response = self._request(url, params=params)
        try:
            result = response.json()
        except ValueError:
            raise self._invalid_response(category, "JSON") from None
        if not isinstance(result, dict):
            raise self._invalid_response(category, "object")
        return result

    def _paginate(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        category: str = "pagination",
    ) -> Iterator[dict[str, Any]]:
        seen: set[str] = set()
        seen_children: set[str] = set()
        while url:
            self._check_cancel()
            identity = url + repr(params)
            if identity in seen:
                raise ConfluenceError(
                    "Confluence pagination loop detected", error_code="pagination"
                )
            seen.add(identity)
            response = self._request(url, params=params)
            try:
                payload = response.json()
            except ValueError:
                raise self._invalid_response(category, "JSON") from None
            if not isinstance(payload, dict):
                raise self._invalid_response(category, "object")
            rows = payload.get("results")
            if not isinstance(rows, list):
                raise self._invalid_response(category, "results")
            child_ids: set[str] = set()
            for row in rows:
                if not isinstance(row, dict):
                    raise self._invalid_response(category, "results entry")
                if category == "children":
                    child_ids.add(self._content_id(row.get("id"), category))
                yield row
            if child_ids:
                # Some gateways ignore start/cursor while returning successful
                # responses. New request URLs alone cannot detect this loop.
                # Overlapping pages remain valid as long as they add a new child.
                if child_ids <= seen_children:
                    raise ConfluenceError(
                        "Confluence child pagination made no progress: repeated child pages",
                        error_code="pagination",
                    )
                seen_children.update(child_ids)
            links = self._mapping(payload.get("_links"), category, "_links")
            next_url = self._optional_text(links.get("next"), category, "_links.next")
            if not next_url:
                next_url = response.links.get("next", {}).get("url")
            # DC may omit next but provide start/limit/size; never guess on Cloud cursor APIs.
            if not next_url and not self.cloud:
                request_params = response.url.params
                limit = self._integer(
                    payload.get("limit", request_params.get("limit", 100)), category, "limit"
                )
                if len(rows) == limit:
                    requested_start = self._integer(
                        request_params.get("start", 0), category, "start", minimum=0
                    )
                    start = self._integer(
                        payload.get("start", requested_start),
                        category,
                        "start",
                        minimum=0,
                    )
                    if start != requested_start:
                        raise ConfluenceError(
                            "Confluence pagination returned an unexpected start offset",
                            error_code="pagination",
                        )
                    # Passing params={start: ...} would replace the whole query
                    # from the previous next link, losing limit and filters.
                    url = str(response.url.copy_set_param("start", start + len(rows)))
                    params = None
                    continue
            if next_url:
                next_text = str(next_url)
                try:
                    url = self._trusted_url(
                        next_text
                        if next_text.startswith("/")
                        else urljoin(str(response.url), next_text)
                    )
                except ValueError:
                    raise self._invalid_response(category, "_links.next") from None
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
            result = self._json(
                self.base + "/rest/api/content", params=params, category="root lookup"
            ).get("results", [])
            if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
                raise self._invalid_response("root lookup", "results")
            if len(result) == 1:
                return self._content_id(result[0].get("id"), "root lookup")
        raise ConfluenceError("Use a root page URL containing /pages/<id> or pageId=<id>")

    def children(self, content_id: str, *, kind: str = "page") -> Iterator[tuple[str, str]]:
        """Read one branch so failed branches can be retried independently."""
        content_id = self._content_id(content_id, "children", "parent id")
        if (
            not isinstance(kind, str)
            or kind not in _CLOUD_COLLECTIONS
            or (not self.cloud and kind != "page")
        ):
            raise self._invalid_response("children", "unsupported parent content type")
        endpoint = (
            f"{self.api}/{_CLOUD_COLLECTIONS[kind]}/{content_id}/direct-children"
            if self.cloud
            else f"{self.api}/content/{content_id}/child/page"
        )
        for item in self._paginate(endpoint, params={"limit": 100}, category="children"):
            child_kind = item.get("type", "page") if self.cloud else "page"
            if not isinstance(child_kind, str) or child_kind not in _CLOUD_COLLECTIONS:
                raise self._invalid_response("children", "unsupported content type")
            yield self._content_id(item.get("id"), "children"), child_kind

    def page_ids(self, root_id: str) -> Iterator[str]:
        root_id = self._content_id(root_id, "children", "root id")
        yield root_id
        seen = {root_id}
        # The Cloud descendants API has a depth cap. Direct children also permit
        # walking through folders, databases, whiteboards and embeds without it.
        pending = [(root_id, "page")]
        while pending:
            parent, kind = pending.pop()
            for child_id, child_kind in self.children(parent, kind=kind):
                if child_id in seen:
                    continue
                seen.add(child_id)
                # Bound the whole traversal, including non-page containers.
                if len(seen) > self.config.max_pages:
                    raise ConfluenceError("Page limit exceeded; tree discovery is incomplete")
                pending.append((child_id, child_kind))
                if child_kind == "page":
                    yield child_id

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
        item = self._json(endpoint, params=params, category="page")
        version_data = self._mapping(item.get("version"), "page", "version")
        ancestors = item.get("ancestors")
        if ancestors is None:
            ancestors = []
        if not isinstance(ancestors, list) or any(not isinstance(row, dict) for row in ancestors):
            raise self._invalid_response("page", "ancestors")
        links = self._mapping(item.get("_links"), "page", "_links")
        body_data = self._mapping(item.get("body"), "page", "body")
        storage = self._mapping(body_data.get("storage"), "page", "body.storage")
        parent = item.get("parentId")
        parent_id = (
            self._content_id(parent, "page", "parentId")
            if parent is not None
            else self._content_id(ancestors[-1].get("id"), "page", "ancestors.id")
            if ancestors
            else None
        )
        webui = self._optional_text(links.get("webui"), "page", "_links.webui")
        return Page(
            id=self._content_id(item.get("id"), "page"),
            title=self._optional_text(item.get("title"), "page", "title") or page_id,
            version=self._integer(
                version_data.get("number", version or 1), "page", "version.number"
            ),
            updated_at=self._optional_text(
                version_data.get("createdAt") or version_data.get("when"), "page", "version date"
            ),
            parent_id=parent_id,
            url=self._trusted_url(webui or f"pages/viewpage.action?pageId={page_id}"),
            body=self._optional_text(storage.get("value"), "page", "body.storage.value") or "",
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
        for item in self._paginate(endpoint, params=params, category="attachments"):
            extension = self._mapping(item.get("extensions"), "attachments", "extensions")
            links = self._mapping(item.get("_links"), "attachments", "_links")
            version_data = self._mapping(item.get("version"), "attachments", "version")
            version = self._integer(version_data.get("number", 1), "attachments", "version.number")
            raw_id = item.get("id")
            if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)) or not str(raw_id):
                raise self._invalid_response("attachments", "id")
            attachment_id = str(raw_id)
            download: str | None
            if self.cloud:
                # downloadLink may point to a retired browser download route.
                # The supported REST endpoint supplies the signed media redirect.
                download = (
                    f"{self.base}/rest/api/content/{quote(page_id, safe='')}/child/attachment/"
                    f"{quote(attachment_id, safe='')}/download?version={version}"
                )
            else:
                download = self._optional_text(
                    item.get("downloadLink") or links.get("download"), "attachments", "download"
                )
            if not download:
                continue
            size = item.get("fileSize")
            if size is None:
                size = extension.get("fileSize")
            result.append(
                Attachment(
                    id=attachment_id,
                    title=self._optional_text(item.get("title"), "attachments", "title")
                    or attachment_id,
                    version=version,
                    media_type=self._optional_text(
                        item.get("mediaType") or extension.get("mediaType"),
                        "attachments",
                        "mediaType",
                    )
                    or "application/octet-stream",
                    size=self._integer(size, "attachments", "fileSize", minimum=0)
                    if size is not None
                    else None,
                    updated_at=self._optional_text(
                        version_data.get("createdAt") or version_data.get("when"),
                        "attachments",
                        "version date",
                    ),
                    download_url=self._trusted_url(download),
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
                {
                    self._integer(item.get("number"), "versions", "number")
                    for item in self._paginate(
                        endpoint, params={"limit": 100}, category="versions"
                    )
                },
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
        with safe_http_logging():
            return self._download(attachment)

    def _download(self, attachment: Attachment) -> bytes:
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
            request_id, started = self._request_started(target, attempt, attachment=True)
            try:
                with client.stream("GET", target, headers={"Accept": "*/*"}) as response:
                    self._response_received(request_id, response, started)
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
                        logger.info(
                            "Confluence attachment redirect accepted: trace=%s hop=%d auth=%s",
                            self.trace_id,
                            redirects,
                            self.config.auth_mode if client is self.http else "none",
                        )
                        continue
                    if response.status_code in {429, 502, 503, 504} and attempt < self.retries:
                        self._retry_wait(
                            self._retry_delay(response, attempt), f"HTTP {response.status_code}"
                        )
                        attempt += 1
                        continue
                    if response.is_error:
                        raise ConfluenceError(
                            f"Attachment HTTP {response.status_code}",
                            status_code=response.status_code,
                        )
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                    if content_type in {"text/html", "application/xhtml+xml"} and (
                        attachment.media_type.lower().startswith("image/")
                        or attachment.title.lower().endswith(
                            (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp")
                        )
                    ):
                        raise ConfluenceError(
                            "Attachment download returned HTML instead of an image "
                            "[attachment_html_response]. Check Confluence/SSO download access; "
                            "the response was not sent to GigaCode.",
                            error_code="attachment_html_response",
                        )
                    chunks, size = [], 0
                    for chunk in response.iter_bytes(64 * 1024):
                        self._check_cancel()
                        size += len(chunk)
                        if size > self.config.max_attachment_bytes:
                            raise ConfluenceError("Attachment exceeds configured size limit")
                        chunks.append(chunk)
                    logger.info(
                        "Confluence attachment download completed: request=%s bytes=%d "
                        "elapsed=%.3fs",
                        request_id,
                        size,
                        monotonic() - started,
                    )
                    return b"".join(chunks)
            except httpx.HTTPError as exc:
                failure = self._network_failure(
                    exc, target, attempt, attachment=True, elapsed=monotonic() - started
                )
                if attempt >= self.retries:
                    raise failure from None
                self._retry_wait(min(2**attempt, 15), failure.error_code or "network")
                attempt += 1
