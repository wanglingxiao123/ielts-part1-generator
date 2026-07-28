"""FastAPI web tier: login, the SigV4 proxy to AgentCore, and the frontend's static build.

Route map:

    GET  /healthz                 liveness, unauthenticated
    POST /api/auth/register       gated by ALLOWED_EMAIL_DOMAINS; first account becomes admin
    POST /api/auth/login          sets the session cookie
    POST /api/auth/logout         clears it
    GET  /api/auth/me             current user
    POST /api/invocations         -> invoke_agent_runtime (JSON or relayed SSE)
    GET  /login                   a server-rendered form, because the SPA has no local login UI
    GET  /*                       the frontend build, with SPA fallback to index.html

Two implementation choices are load-bearing:

**The `/api/*` gate is a raw ASGI middleware, not `BaseHTTPMiddleware`.** BaseHTTPMiddleware
re-wraps the response, which is exactly the layer where a streaming body accidentally becomes a
buffered one. This middleware only reads the request scope and either short-circuits with a 401 or
calls through untouched, so the SSE path has nothing between it and the server.

**The SSE relay yields from a sync generator.** `StreamingResponse` runs a sync iterator through
`iterate_in_threadpool`, so each blocking `iter_lines` read happens off the event loop and each
line reaches the browser as it arrives. Collecting the lines into a list first would pass every
functional test and still break the only thing the frontend actually needs from this endpoint --
progress that shows up during an eight-minute batch rather than after it.
"""

from __future__ import annotations

import json
import os
import time
from http.cookies import SimpleCookie
from typing import Any, Dict, Iterator, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)

from .auth import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    AuthError,
    AuthService,
    InvalidSession,
    build_auth,
)
from .runtime_client import (
    SSE_CONTENT_TYPE,
    AgentCoreRuntimeClient,
    RuntimeNotConfigured,
    iter_sse_payloads,
    new_session_id,
    read_json,
)

HERE = os.path.dirname(os.path.abspath(__file__))

# Only these /api paths are reachable without a session. Everything else under /api is 401.
# An allowlist rather than a denylist: a new endpoint added later is closed by default.
PUBLIC_API_PATHS = frozenset({
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
})

# Set on the scope by the middleware, read by the handlers. Not `scope["state"]`, which Starlette
# only populates further in than this middleware runs.
USER_SCOPE_KEY = "ielts.user"

# A warm microVM is reused while the same runtimeSessionId is used, and the id also resets the
# idle timer. Rotating after this long keeps any single streaming connection far inside the
# platform's 60-minute cap (a 6-material batch measures ~8 minutes).
SESSION_REUSE_SECONDS = 45 * 60


def _cookie(scope_headers: Any, name: str) -> Optional[str]:
    for key, value in scope_headers:
        if key.lower() != b"cookie":
            continue
        jar = SimpleCookie()
        try:
            jar.load(value.decode("latin-1"))
        except Exception:  # noqa: BLE001 - a malformed Cookie header is simply "no cookie"
            continue
        if name in jar:
            return jar[name].value
    return None


def _error_body(code: str, message: str, **detail: Any) -> Dict[str, Any]:
    """The frontend's error shape (frontend/src/api/agentcore.ts reads `error.code`)."""
    body: Dict[str, Any] = {"code": code, "message": message}
    if detail:
        body["detail"] = detail
    return {"error": body}


class ApiAuthMiddleware:
    """401s every `/api/*` request without a valid session cookie, except the allowlist."""

    def __init__(self, app: Any, auth: AuthService) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith("/api/") and path not in PUBLIC_API_PATHS:
            token = _cookie(scope.get("headers") or [], SESSION_COOKIE)
            try:
                scope[USER_SCOPE_KEY] = self.auth.identify(token)
            except InvalidSession as exc:
                response = JSONResponse(
                    _error_body("UNAUTHENTICATED", str(exc.message)), status_code=401,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class WebTier:
    """Holds the app plus the pieces tests need to substitute.

    Assembled in a class rather than at module scope so a test can build an app with a memory
    store and a stub runtime without touching the environment, and so importing this module never
    constructs an AWS client.
    """

    def __init__(self, auth: AuthService, runtime: AgentCoreRuntimeClient,
                 static_dir: str, *, cookie_secure: bool = False) -> None:
        self.auth = auth
        self.runtime = runtime
        self.static_dir = static_dir
        self.cookie_secure = cookie_secure
        # email -> (runtimeSessionId, minted_at). Per-user so two reviewers do not share a
        # microVM, and so one user's long batch does not reset another's idle timer.
        self._sessions: Dict[str, Tuple[str, float]] = {}
        self.app = self._build()

    # ── runtime session ids ──────────────────────────────────────────────────

    def session_for(self, email: str, *, now: Optional[float] = None) -> str:
        moment = time.time() if now is None else now
        existing = self._sessions.get(email)
        if existing and moment - existing[1] < SESSION_REUSE_SECONDS:
            return existing[0]
        minted = new_session_id()
        self._sessions[email] = (minted, moment)
        return minted

    # ── app assembly ─────────────────────────────────────────────────────────

    def _build(self) -> FastAPI:
        app = FastAPI(title="IELTS Part 1 web tier", docs_url=None, redoc_url=None)
        app.add_middleware(ApiAuthMiddleware, auth=self.auth)

        @app.get("/healthz")
        def healthz() -> Dict[str, Any]:
            # Deliberately does not call AWS: a health check that depends on a downstream service
            # turns a Runtime hiccup into a killed web task.
            return {
                "status": "ok",
                "runtime_configured": self.runtime.configured,
                "static": os.path.isfile(os.path.join(self.static_dir, "index.html")),
                "user_store": type(self.auth.store).__name__,
            }

        @app.post("/api/auth/register")
        async def register(request: Request) -> JSONResponse:
            body = _as_dict(await _json_body(request))
            try:
                user = self.auth.register(str(body.get("email") or ""),
                                          str(body.get("password") or ""))
            except AuthError as exc:
                return JSONResponse(_error_body(exc.code, exc.message), status_code=exc.status)
            # Registering logs you in: a separate login round trip after a successful register
            # only exists to be forgotten.
            return self._with_session(JSONResponse({"user": user}), user["email"])

        @app.post("/api/auth/login")
        async def login(request: Request) -> JSONResponse:
            body = _as_dict(await _json_body(request))
            try:
                user = self.auth.login(str(body.get("email") or ""),
                                       str(body.get("password") or ""))
            except AuthError as exc:
                return JSONResponse(_error_body(exc.code, exc.message), status_code=exc.status)
            return self._with_session(JSONResponse({"user": user}), user["email"])

        @app.post("/api/auth/logout")
        def logout() -> JSONResponse:
            # The token stays cryptographically valid until it expires -- there is no server-side
            # session table to revoke against. Stated here so nobody mistakes this for revocation.
            response = JSONResponse({"ok": True})
            response.delete_cookie(SESSION_COOKIE, path="/")
            return response

        @app.get("/api/auth/me")
        def me(request: Request) -> JSONResponse:
            try:
                user = self.auth.identify(request.cookies.get(SESSION_COOKIE))
            except InvalidSession as exc:
                return JSONResponse(_error_body("UNAUTHENTICATED", exc.message), status_code=401)
            return JSONResponse({"user": user})

        @app.post("/api/invocations")
        async def invocations(request: Request) -> Any:
            return await self._invocations(request)

        @app.get("/login")
        def login_page() -> HTMLResponse:
            return HTMLResponse(_login_html())

        @app.get("/{full_path:path}")
        def spa(full_path: str, request: Request) -> Any:
            return self._serve_static(full_path, request)

        return app

    # ── helpers ──────────────────────────────────────────────────────────────

    def _with_session(self, response: JSONResponse, email: str) -> JSONResponse:
        response.set_cookie(
            SESSION_COOKIE,
            self.auth.issue_token(email),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            # False by default because the deployment is plain HTTP on a task IP: a Secure cookie
            # would simply never be stored, and login would appear to succeed then fail.
            secure=self.cookie_secure,
            path="/",
        )
        return response

    async def _invocations(self, request: Request) -> Any:
        """Proxy one payload to the Runtime. JSON in, JSON or relayed SSE out."""
        user = request.scope.get(USER_SCOPE_KEY) or {}
        payload = await _json_body(request)
        if not isinstance(payload, dict):
            return JSONResponse(_error_body("bad_request", "payload must be a JSON object"),
                                status_code=400)
        session_id = self.session_for(str(user.get("email") or "anonymous"))

        from starlette.concurrency import run_in_threadpool

        try:
            # In a threadpool because boto3 is synchronous. The call returns once the Runtime's
            # response headers arrive, so this awaits the connection, not the batch.
            content_type, body, _ = await run_in_threadpool(
                self.runtime.invoke, payload, session_id=session_id
            )
        except RuntimeNotConfigured as exc:
            return JSONResponse(_error_body("RUNTIME_NOT_CONFIGURED", str(exc)), status_code=503)
        except Exception as exc:  # noqa: BLE001 - surfaced to the browser in its own error shape
            return JSONResponse(
                _error_body("RUNTIME_INVOKE_FAILED",
                            "%s: %s" % (type(exc).__name__, str(exc)[:300])),
                status_code=502,
            )

        if SSE_CONTENT_TYPE in content_type:
            return StreamingResponse(
                _relay(body),
                media_type=SSE_CONTENT_TYPE,
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "X-Accel-Buffering": "no",  # harmless here, and correct if a proxy appears
                    "Connection": "keep-alive",
                },
            )
        return JSONResponse(read_json(body))

    def _serve_static(self, full_path: str, request: Request) -> Any:
        """The frontend build, with SPA fallback.

        Unknown paths return index.html so client-side routes like `/compare/x` survive a reload.
        An unauthenticated document request is redirected to `/login` instead: the SPA has no UI
        for these accounts, so handing it to an anonymous visitor would render a shell that 401s
        on its first API call.
        """
        index = os.path.join(self.static_dir, "index.html")
        candidate = os.path.normpath(os.path.join(self.static_dir, full_path or "index.html"))
        root = os.path.abspath(self.static_dir)
        inside = os.path.abspath(candidate).startswith(root + os.sep) or \
            os.path.abspath(candidate) == root
        if inside and os.path.isfile(candidate) and full_path not in ("", "index.html"):
            # Assets are served without a session: they are the same bytes for everyone, and
            # gating them would only break the login page's own styling.
            return FileResponse(candidate)
        try:
            self.auth.identify(request.cookies.get(SESSION_COOKIE))
        except InvalidSession:
            return RedirectResponse("/login", status_code=302)
        if not os.path.isfile(index):
            return JSONResponse(
                _error_body("STATIC_MISSING",
                            "frontend build not found at %s; run `npm run build` and copy dist/"
                            % self.static_dir),
                status_code=404,
            )
        return FileResponse(index)


def _relay(body: Any) -> Iterator[bytes]:
    """Re-frame the Runtime's `data:` lines for the browser, one at a time.

    Sync generator on purpose -- see the module docstring. Each `yield` becomes one ASGI
    `http.response.body` message with `more_body=True`, which is the observable property the
    tests assert.
    """
    try:
        for payload in iter_sse_payloads(body):
            yield ("data: %s\n\n" % payload).encode("utf-8")
    except Exception as exc:  # noqa: BLE001 - mid-stream failure must still reach the client
        # The frontend treats a stream that ends without `batch_completed` as a lost connection;
        # a final `batch_failed` frame lets it say what actually happened instead.
        broken = {"type": "batch_failed", "reason": "stream_error",
                  "detail": "%s: %s" % (type(exc).__name__, str(exc)[:300])}
        yield ("data: %s\n\n" % json.dumps(broken)).encode("utf-8")
    finally:
        closer = getattr(body, "close", None)
        if callable(closer):
            closer()


async def _json_body(request: Request) -> Any:
    """The parsed body, or `{}` if it will not parse.

    Returns the parsed value *as-is* rather than coercing to a dict: `/api/invocations` needs to
    tell "a JSON array" from "an empty object" so it can reject the former. An earlier version
    flattened both to `{}`, which silently made that check unreachable.
    """
    try:
        return await request.json()
    except Exception:  # noqa: BLE001 - an unparseable body is an empty one, handled by callers
        return {}


def _as_dict(body: Any) -> Dict[str, Any]:
    return body if isinstance(body, dict) else {}


def _login_html() -> str:
    """A standalone login form.

    Server-rendered rather than added to the SPA: the SPA's auth module is Cognito-shaped and
    still on `devBypass`, and bending it to cookie auth is a frontend task, not a deployment
    blocker. This page speaks only to `/api/auth/*`.
    """
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · IELTS Part 1</title>
<style>
 body{font-family:system-ui,-apple-system,"PingFang SC",sans-serif;background:#f4f5f7;margin:0;
      display:flex;min-height:100vh;align-items:center;justify-content:center}
 form{background:#fff;padding:32px;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.12);
      width:320px;display:flex;flex-direction:column;gap:12px}
 h1{font-size:18px;margin:0 0 8px}
 input{padding:9px 10px;border:1px solid #cbd2d9;border-radius:6px;font-size:14px}
 button{padding:9px;border:0;border-radius:6px;background:#0b6bcb;color:#fff;font-size:14px;
        cursor:pointer}
 button.alt{background:#e7ebf0;color:#243b53}
 .msg{font-size:13px;min-height:18px;color:#b42318}
</style></head><body>
<form id="f">
  <h1>IELTS Part 1 材料生成</h1>
  <input id="email" type="email" placeholder="邮箱" autocomplete="username" required>
  <input id="password" type="password" placeholder="密码（至少 8 位）"
         autocomplete="current-password" required>
  <button type="submit">登录</button>
  <button type="button" class="alt" id="reg">注册</button>
  <div class="msg" id="msg"></div>
</form>
<script>
const msg = document.getElementById('msg');
async function go(path) {
  msg.style.color = '#b42318'; msg.textContent = '';
  const res = await fetch(path, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({email: email.value, password: password.value}),
  });
  const body = await res.json().catch(() => ({}));
  if (res.ok) { location.href = '/'; return; }
  msg.textContent = (body.error && body.error.message) || ('请求失败 ' + res.status);
}
document.getElementById('f').addEventListener('submit', (e) => {
  e.preventDefault(); go('/api/auth/login');
});
document.getElementById('reg').addEventListener('click', () => go('/api/auth/register'));
</script></body></html>
"""


def build_tier(env: Optional[Dict[str, str]] = None) -> WebTier:
    """Assemble from the environment. Raises `SigningKeyUnavailable` on a bad config."""
    env = os.environ if env is None else env
    return WebTier(
        build_auth(env),
        AgentCoreRuntimeClient(),
        (env.get("WEB_STATIC_DIR") or os.path.join(HERE, "static")).strip(),
        cookie_secure=(env.get("SESSION_COOKIE_SECURE") or "").lower() in {"1", "true", "yes"},
    )


def create_app() -> FastAPI:
    return build_tier().app


# Module-level app for `uvicorn web.app:app`. Built at import so a misconfiguration (no
# SESSION_SECRET with a shared store) crashes the container at start rather than at first login.
app = create_app()


if __name__ == "__main__":  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "80")))
