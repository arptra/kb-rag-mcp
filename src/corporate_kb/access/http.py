"""Certificate enrollment and separate access-administrator HTTP endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_http_request
from starlette.requests import Request
from starlette.responses import JSONResponse

from corporate_kb.access.models import AccessDenied, AccessRateLimited
from corporate_kb.access.store import AccessStore
from corporate_kb.access.tls import certificate_from_scope
from corporate_kb.config import Settings

logger = logging.getLogger(__name__)
USER_COOKIE = "__Host-kb-user"
ADMIN_COOKIE = "__Host-kb-access-admin"
MCP_CONFIG_COOKIE = "__Host-kb-mcp-config"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_MAX_BODY = 16_384
Handler = Callable[[Request], Awaitable[JSONResponse]]


def _response(payload: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


def bearer_token(request: Request) -> str | None:
    scheme, separator, token = request.headers.get("authorization", "").partition(" ")
    if separator and scheme.lower() == "bearer" and token and len(token) <= 4096:
        return token
    return None


def _same_origin(request: Request, *, required: bool = False) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return not required
    return origin == str(request.base_url).rstrip("/")


def _csrf_token(session: str) -> str:
    return hmac.new(session.encode(), b"kb-access-admin-csrf-v1", hashlib.sha256).hexdigest()


async def _body(request: Request) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise ValueError("Content-Type must be application/json")
    if not _same_origin(request):
        raise AccessDenied("Cross-origin requests are not allowed")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > _MAX_BODY:
            raise ValueError("Request body is too large")
    try:
        parsed = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Request body must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Request body must be a JSON object")
    return parsed


def _string(body: dict[str, Any], key: str, *, default: str | None = None) -> str:
    value = body.get(key, default)
    if not isinstance(value, str) or len(value) > 1024:
        raise ValueError(f"{key} must be a string of at most 1024 characters")
    return value


def _pagination(request: Request) -> dict[str, int]:
    try:
        limit = int(request.query_params.get("limit", "100"))
        offset = int(request.query_params.get("offset", "0"))
    except ValueError as exc:
        raise ValueError("limit and offset must be integers") from exc
    if not 1 <= limit <= 200 or not 0 <= offset <= 1_000_000:
        raise ValueError("Invalid pagination range")
    return {"limit": limit, "offset": offset}


def _address(request: Request) -> str:
    # Never trust client-supplied forwarding headers for audit/rate limiting.
    return request.client.host if request.client else "unknown"


def _guard(handler: Handler) -> Handler:
    @wraps(handler)
    async def wrapped(request: Request) -> JSONResponse:
        if request.url.scheme != "https":
            return _response({"error": "Access control requires HTTPS"}, 403)
        try:
            return await handler(request)
        except AccessRateLimited:
            response = _response({"error": "Too many attempts; try again later"}, 429)
            response.headers["Retry-After"] = "900"
            return response
        except AccessDenied as exc:
            return _response({"error": str(exc)}, 403)
        except KeyError:
            return _response({"error": "Not found"}, 404)
        except ValueError as exc:
            return _response({"error": str(exc)}, 400)
        except Exception:
            logger.exception("Access-control operation failed")
            return _response({"error": "Access-control operation failed"}, 500)

    return wrapped


class RegistryTokenVerifier(TokenVerifier):
    """Resolve live registry state for every MCP HTTP request, including sessions."""

    def __init__(self, store: AccessStore) -> None:
        super().__init__(required_scopes=["kb:read"])
        self.store = store

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            request = get_http_request()
        except RuntimeError:
            return None
        if request.url.scheme != "https":
            return None
        certificate = certificate_from_scope(request.scope)
        user = await asyncio.to_thread(
            self.store.verify_user_token,
            token,
            fingerprint=certificate.fingerprint if certificate else None,
        )
        if user is None:
            return None
        return AccessToken(
            token=token,
            client_id=f"certificate:{user['id']}",
            subject=user["id"],
            scopes=["kb:read"],
        )


class AccessControl:
    """HTTP adapter around a replaceable persistent access registry."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = AccessStore(
            settings.access_db_path,
            token_ttl_seconds=settings.access_token_ttl_seconds,
            admin_session_ttl_seconds=settings.access_admin_session_ttl_seconds,
        )
        password = settings.access_bootstrap_admin_password
        self.store.bootstrap_admin(
            settings.access_bootstrap_admin_username,
            password.get_secret_value() if password is not None else None,
        )

    def user(self, request: Request) -> dict[str, Any] | None:
        if request.url.scheme != "https":
            return None
        if "authorization" in request.headers:
            token = bearer_token(request)
        else:
            token = request.cookies.get(USER_COOKIE)
            if request.method not in _SAFE_METHODS and not _same_origin(request, required=True):
                return None
        if not token:
            return None
        certificate = certificate_from_scope(request.scope)
        return self.store.verify_user_token(
            token,
            fingerprint=certificate.fingerprint if certificate else None,
        )

    def administrator(self, request: Request) -> dict[str, Any] | None:
        token = request.cookies.get(ADMIN_COOKIE, "")
        if request.url.scheme != "https" or not token or len(token) > 4096:
            return None
        if request.method not in _SAFE_METHODS:
            supplied = request.headers.get("x-csrf-token", "")
            if (
                not _same_origin(request)
                or not supplied.isascii()
                or not hmac.compare_digest(supplied, _csrf_token(token))
            ):
                return None
        return self.store.verify_admin_session(token)

    def register(self, server: FastMCP) -> None:
        def route(path: str, methods: list[str]) -> Callable[[Handler], Handler]:
            def decorate(handler: Handler) -> Handler:
                guarded = _guard(handler)
                server.custom_route(path, methods=methods, include_in_schema=False)(guarded)
                return guarded

            return decorate

        def admin_route(path: str, methods: list[str]) -> Callable[[Handler], Handler]:
            def decorate(handler: Handler) -> Handler:
                @wraps(handler)
                async def authorized(request: Request) -> JSONResponse:
                    admin = await asyncio.to_thread(self.administrator, request)
                    if admin is None:
                        return _response({"error": "Administrator login required"}, 401)
                    request.state.access_admin = admin
                    return await handler(request)

                return route(path, methods)(authorized)

            return decorate

        @route("/auth/status", ["GET"])
        async def status(request: Request) -> JSONResponse:
            user = await asyncio.to_thread(self.user, request)
            return _response(
                {
                    "enabled": True,
                    "authenticated": user is not None,
                    "certificate_present": certificate_from_scope(request.scope) is not None,
                    "user": user,
                }
            )

        async def enroll(request: Request, *, browser: bool) -> JSONResponse:
            await _body(request)
            certificate = certificate_from_scope(request.scope)
            if certificate is None:
                raise AccessDenied("A trusted personal client certificate is required")
            existing = request.cookies.get(USER_COOKIE) if browser else bearer_token(request)
            issued = await asyncio.to_thread(
                self.store.enroll,
                certificate,
                existing_token=existing,
                address=_address(request),
            )
            if browser:
                response = _response({"user": issued.user, "expires_at": issued.expires_at})
                response.set_cookie(
                    USER_COOKIE,
                    issued.token,
                    path="/",
                    secure=True,
                    httponly=True,
                    samesite="strict",
                    max_age=max(0, issued.expires_at - int(time.time())),
                )
                return response
            return _response(
                {
                    "access_token": issued.token,
                    "token_type": "Bearer",
                    "expires_at": issued.expires_at,
                    "mcp_path": self.settings.mcp_http_path,
                    "user": issued.user,
                }
            )

        @route("/auth/token", ["POST"])
        async def issue_token(request: Request) -> JSONResponse:
            return await enroll(request, browser=False)

        @route("/auth/browser-session", ["POST"])
        async def browser_session(request: Request) -> JSONResponse:
            return await enroll(request, browser=True)

        @route("/auth/mcp-config", ["POST"])
        async def mcp_config(request: Request) -> JSONResponse:
            """Explicit browser export with its own token, never a dashboard session leak."""
            await _body(request)
            if not _same_origin(request, required=True):
                raise AccessDenied("Same-origin request required")
            certificate = certificate_from_scope(request.scope)
            if certificate is None:
                raise AccessDenied("A trusted personal client certificate is required")
            issued = await asyncio.to_thread(
                self.store.enroll,
                certificate,
                existing_token=request.cookies.get(MCP_CONFIG_COOKIE),
                address=_address(request),
            )
            # Origin was checked above, and the direct TLS server ignores forwarded
            # headers. The config must point to the same service the browser visited.
            mcp_url = str(request.base_url).rstrip("/") + self.settings.mcp_http_path
            response = _response(
                {
                    "config": {
                        "mcpServers": {
                            "corporate-kb": {
                                "httpUrl": mcp_url,
                                "headers": {"Authorization": f"Bearer {issued.token}"},
                            }
                        }
                    },
                    "expires_at": issued.expires_at,
                    "user": issued.user,
                }
            )
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            # Reopening the page may reuse this export token after a fresh mTLS
            # check. Browser logout only revokes USER_COOKIE, not a downloaded MCP
            # configuration. Token/user revocation still takes effect in the DB.
            response.set_cookie(
                MCP_CONFIG_COOKIE,
                issued.token,
                path="/",
                secure=True,
                httponly=True,
                samesite="strict",
                max_age=max(0, issued.expires_at - int(time.time())),
            )
            return response

        @route("/auth/logout", ["POST"])
        async def logout_user(request: Request) -> JSONResponse:
            await _body(request)
            if not _same_origin(request, required=True):
                raise AccessDenied("Same-origin request required")
            await asyncio.to_thread(self.store.logout_user, request.cookies.get(USER_COOKIE, ""))
            response = _response({"ok": True})
            response.delete_cookie(
                USER_COOKIE, path="/", secure=True, httponly=True, samesite="strict"
            )
            return response

        @route("/access/api/login", ["POST"])
        async def login(request: Request) -> JSONResponse:
            body = await _body(request)
            try:
                issued = await asyncio.to_thread(
                    self.store.login_admin,
                    _string(body, "username"),
                    _string(body, "password"),
                    address=_address(request),
                )
            except AccessRateLimited:
                raise
            except AccessDenied:
                return _response({"error": "Invalid username or password"}, 401)
            response = _response({"admin": issued.admin, "csrf_token": _csrf_token(issued.token)})
            response.set_cookie(
                ADMIN_COOKIE,
                issued.token,
                path="/",
                secure=True,
                httponly=True,
                samesite="strict",
                max_age=max(0, issued.expires_at - int(time.time())),
            )
            return response

        @admin_route("/access/api/session", ["GET"])
        async def session(request: Request) -> JSONResponse:
            return _response(
                {
                    "admin": request.state.access_admin,
                    "csrf_token": _csrf_token(request.cookies[ADMIN_COOKIE]),
                }
            )

        @admin_route("/access/api/logout", ["POST"])
        async def logout_admin(request: Request) -> JSONResponse:
            await _body(request)
            await asyncio.to_thread(self.store.logout_admin, request.cookies[ADMIN_COOKIE])
            response = _response({"ok": True})
            response.delete_cookie(
                ADMIN_COOKIE, path="/", secure=True, httponly=True, samesite="strict"
            )
            return response

        @admin_route("/access/api/users", ["GET"])
        async def users(request: Request) -> JSONResponse:
            return _response(await asyncio.to_thread(self.store.list_users, **_pagination(request)))

        @admin_route("/access/api/tokens", ["GET"])
        async def tokens(request: Request) -> JSONResponse:
            return _response(
                await asyncio.to_thread(
                    self.store.list_tokens,
                    user_id=request.query_params.get("user_id") or None,
                    **_pagination(request),
                )
            )

        @admin_route("/access/api/admins", ["GET"])
        async def administrators(_request: Request) -> JSONResponse:
            return _response(await asyncio.to_thread(self.store.list_admins))

        @admin_route("/access/api/events", ["GET"])
        async def events(request: Request) -> JSONResponse:
            return _response(
                await asyncio.to_thread(self.store.list_events, **_pagination(request))
            )

        @admin_route("/access/api/users/{user_id}/revoke", ["POST"])
        async def revoke_user(request: Request) -> JSONResponse:
            body = await _body(request)
            await asyncio.to_thread(
                self.store.revoke_user,
                request.path_params["user_id"],
                actor=request.state.access_admin["username"],
                reason=_string(body, "reason", default=""),
                admin_session=request.cookies[ADMIN_COOKIE],
            )
            return _response({"ok": True})

        @admin_route("/access/api/users/{user_id}/restore", ["POST"])
        async def restore_user(request: Request) -> JSONResponse:
            await _body(request)
            await asyncio.to_thread(
                self.store.restore_user,
                request.path_params["user_id"],
                actor=request.state.access_admin["username"],
                admin_session=request.cookies[ADMIN_COOKIE],
            )
            return _response({"ok": True})

        @admin_route("/access/api/tokens/{token_id}/revoke", ["POST"])
        async def revoke_token(request: Request) -> JSONResponse:
            body = await _body(request)
            await asyncio.to_thread(
                self.store.revoke_token,
                request.path_params["token_id"],
                actor=request.state.access_admin["username"],
                reason=_string(body, "reason", default=""),
                admin_session=request.cookies[ADMIN_COOKIE],
            )
            return _response({"ok": True})

        @admin_route("/access/api/admins", ["POST"])
        async def add_admin(request: Request) -> JSONResponse:
            body = await _body(request)
            admin = await asyncio.to_thread(
                self.store.add_admin,
                _string(body, "username"),
                _string(body, "password"),
                actor=request.state.access_admin["username"],
                admin_session=request.cookies[ADMIN_COOKIE],
            )
            return _response({"admin": admin}, 201)

        @admin_route("/access/api/admins/{admin_id}/deactivate", ["POST"])
        async def deactivate_admin(request: Request) -> JSONResponse:
            await _body(request)
            await asyncio.to_thread(
                self.store.deactivate_admin,
                request.path_params["admin_id"],
                actor=request.state.access_admin["username"],
                admin_session=request.cookies[ADMIN_COOKIE],
            )
            return _response({"ok": True})


def register_disabled_status(server: FastMCP) -> None:
    @server.custom_route("/auth/status", methods=["GET"], include_in_schema=False)
    async def disabled_status(_request: Request) -> JSONResponse:
        return _response(
            {
                "enabled": False,
                "authenticated": False,
                "certificate_present": False,
                "user": None,
            }
        )
