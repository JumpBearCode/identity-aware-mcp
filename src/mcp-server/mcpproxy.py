"""Plan A — thin resource-stripping proxy for RFC 8707 clients.

Some MCP clients (Claude Code, opencode) follow RFC 8707 and always attach a
`resource=` parameter to the Entra `/authorize` + `/token` calls. Entra v2 rejects
that with `AADSTS9010010`. VS Code does not, so `/mcp` (client talks to Entra
directly) keeps working — we leave it untouched.

This module adds a SECOND endpoint, `/mcpproxy`, that sits one hop in front of
Entra purely to delete the `resource` parameter:

    client --scope+resource--> /mcpproxy/{authorize,token}  (we strip resource)
                          --scope-only--> Entra             (issues a real token)

The proxy is (almost) stateless: it mints no tokens, stores nothing, and holds no
secret. PKCE and `state` round-trip end-to-end between the client and Entra; the
Entra callback goes straight back to the client's own loopback redirect_uri. The
client therefore ends up holding a REAL Entra token, so `/mcpproxy`'s MCP endpoint
is verified by the exact same `AzureJWTVerifier` as `/mcp` — `oid`, OBO and the
AD-group tool gating are all unchanged.

The only "AS" behaviour we advertise is discovery metadata (protected-resource +
authorization-server) that points the client at our two forwarding routes and
omits `registration_endpoint`, so a spec-compliant client uses its statically
configured `client_id` (49af5fc1) and never attempts DCR.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

import httpx
from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

logger = logging.getLogger("dataops-mcp.mcpproxy")

# Reserved OIDC scopes we always ensure are present on the upstream authorize
# request: `offline_access` so Entra returns a refresh_token, `openid`/`profile`
# so the id_token / user claims come back. They are allowed alongside a single
# resource's scope in one Entra v2 request.
_RESERVED_SCOPES = ("offline_access", "openid", "profile")


def _ensure_scopes(scope: str | None, api_scope: str) -> str:
    """Return a scope string guaranteed to contain our API scope + the reserved
    OIDC scopes, preserving whatever the client already asked for."""
    parts = scope.split() if scope else []
    for needed in (api_scope, *_RESERVED_SCOPES):
        if needed not in parts:
            parts.append(needed)
    return " ".join(parts)


def find_streamable_asgi_app(app, mcp_path: str):
    """Locate the inner StreamableHTTP ASGI app behind the `/mcp` route so we can
    re-serve it under `/mcpproxy` with a different WWW-Authenticate target.

    The `/mcp` route's endpoint is a RequireAuthMiddleware wrapping the shared
    StreamableHTTPASGIApp (whose session_manager is set once by the app lifespan).
    Serving that same object under a second path shares one session manager, so no
    lifespan changes are needed. Sessions are keyed by the `mcp-session-id` header,
    not the URL path, so two paths coexist safely.
    """
    for route in app.router.routes:
        if isinstance(route, Route) and route.path == mcp_path:
            endpoint = getattr(route, "app", None) or getattr(route, "endpoint", None)
            if isinstance(endpoint, RequireAuthMiddleware):
                return endpoint.app
            return endpoint
    raise RuntimeError(f"could not find MCP streamable route at {mcp_path!r}")


def install_proxy_endpoint(
    app,
    *,
    mcp_path: str,
    base_url: str,
    tenant_id: str,
    mcp_app_id: str,
    required_scopes: list[str],
    proxy_path: str = "/mcpproxy",
) -> None:
    """Add the `/mcpproxy` endpoint + its OAuth discovery/forwarding routes to an
    already-built FastMCP Starlette app. Mutates `app.router.routes` in place.

    `/mcp` and all its existing routes are left completely untouched.
    """
    base = base_url.rstrip("/")
    resource = f"{base}{proxy_path}"          # e.g. https://host/mcpproxy
    issuer = resource                          # our AS issuer == the resource URL
    authorize_ep = f"{resource}/authorize"
    token_ep = f"{resource}/token"
    api_scope = f"api://{mcp_app_id}/user_impersonation"

    upstream_authorize = (
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize"
    )
    upstream_token = (
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    )

    prm_path = "/.well-known/oauth-protected-resource" + proxy_path
    prm_url = f"{base}{prm_path}"

    # --- ① Protected-resource metadata (RFC 9728): point the client at OUR AS ---
    async def protected_resource_metadata(_request: Request) -> Response:
        return JSONResponse(
            {
                "resource": resource,
                "authorization_servers": [issuer],
                "scopes_supported": [api_scope],
                "bearer_methods_supported": ["header"],
            }
        )

    # --- ② Authorization-server metadata (RFC 8414): NO registration_endpoint,
    #        so a compliant client uses its static client_id and never does DCR. --
    async def authorization_server_metadata(_request: Request) -> Response:
        return JSONResponse(
            {
                "issuer": issuer,
                "authorization_endpoint": authorize_ep,
                "token_endpoint": token_ep,
                "response_types_supported": ["code"],
                "response_modes_supported": ["query", "fragment"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": [api_scope, *_RESERVED_SCOPES],
            }
        )

    # --- ③ authorize: strip `resource`, 302 to Entra. redirect_uri is the client's
    #        own loopback and is passed through untouched, so Entra's callback goes
    #        straight back to the client — we never see the code (stateless). ------
    async def authorize(request: Request) -> Response:
        params = dict(request.query_params)
        params.pop("resource", None)  # ★ the whole point: drop RFC 8707 resource
        params["scope"] = _ensure_scopes(params.get("scope"), api_scope)
        # Only ever redirect to the fixed Entra endpoint (no open-redirect surface).
        return RedirectResponse(
            f"{upstream_authorize}?{urlencode(params)}", status_code=302
        )

    # --- ④ token: strip `resource`, forward to Entra, relay the response verbatim.
    #        Handles both authorization_code and refresh_token grants. ------------
    async def token(request: Request) -> Response:
        form = dict(await request.form())
        form.pop("resource", None)  # ★ drop RFC 8707 resource here too
        async with httpx.AsyncClient(timeout=30.0) as client:
            upstream = await client.post(
                upstream_token,
                data=form,
                headers={"Accept": "application/json"},
            )
        # Relay status + body untouched. Never log the body — it carries tokens.
        media_type = upstream.headers.get("content-type", "application/json")
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=media_type,
        )

    # --- ⑤ the /mcpproxy MCP endpoint itself: same streamable app, same verifier,
    #        only the 401 WWW-Authenticate points at our proxy metadata. ----------
    streamable_app = find_streamable_asgi_app(app, mcp_path)
    proxy_mcp_endpoint = RequireAuthMiddleware(
        streamable_app,
        required_scopes,
        prm_url,  # WWW-Authenticate: resource_metadata="…/oauth-protected-resource/mcpproxy"
    )

    new_routes = [
        Route(prm_path, protected_resource_metadata, methods=["GET"]),
        # Register the AS metadata at both discovery locations clients try for an
        # issuer that has a path component: RFC 8414 path-insertion and the
        # path-append variant used by newer MCP clients.
        Route(
            "/.well-known/oauth-authorization-server" + proxy_path,
            authorization_server_metadata,
            methods=["GET"],
        ),
        Route(
            proxy_path + "/.well-known/oauth-authorization-server",
            authorization_server_metadata,
            methods=["GET"],
        ),
        Route(proxy_path + "/authorize", authorize, methods=["GET"]),
        Route(proxy_path + "/token", token, methods=["POST"]),
        Route(
            proxy_path,
            proxy_mcp_endpoint,
            methods=["GET", "POST", "DELETE"],
        ),
    ]
    # Insert ahead of any catch-alls; plain appends work too since paths are exact.
    app.router.routes[:0] = new_routes
    logger.info(
        "mcpproxy enabled: resource=%s authorize=%s token=%s",
        resource,
        authorize_ep,
        token_ep,
    )
