"""FastAPI web tier: login, the SigV4 proxy to AgentCore, and the frontend's static build.

Route map:

    GET  /healthz                 liveness, unauthenticated
    POST /api/auth/register       gated by ALLOWED_EMAIL_DOMAINS; first account becomes admin
    POST /api/auth/login          sets the session cookie
    POST /api/auth/logout         clears it
    GET  /api/auth/me             current user
    POST /api/invocations         -> invoke_agent_runtime (JSON, relayed SSE, or a fanned-out batch)
    GET  /api/batch-history                  batch history, newest first (web/batch_history.py)
    GET  /api/batch-history/{id}             one historical batch, with its materials' artifacts
    GET  /api/batch-history-material/{id}    one material by id alone, for the reader page
    GET  /api/material-questions/{id}        the delivered question set, for the 题目预览 tab
    GET/POST/DELETE /api/material-comments/* personal material comments
    POST /api/batch-history/{id}/submit      records the 已提交 status
    POST /api/batch-history/{id}/withdraw    undoes it, wholly or per material
    GET  /login                   a server-rendered form, because the SPA has no local login UI
    GET  /*                       the frontend build, with SPA fallback to index.html

Three implementation choices are load-bearing:

**The `/api/*` gate is a raw ASGI middleware, not `BaseHTTPMiddleware`.** BaseHTTPMiddleware
re-wraps the response, which is exactly the layer where a streaming body accidentally becomes a
buffered one. This middleware only reads the request scope and either short-circuits with a 401 or
calls through untouched, so the SSE path has nothing between it and the server.

**Neither SSE path lets a blocking read touch the event loop, and each does it differently.**

* `action: generate` and `action: generate_sets` are fanned out: one `invoke_agent_runtime` per
  material, merged into one event stream (`web/fanout.py`). Each child's blocking `iter_lines` sits on
  a dedicated executor thread and hands events to the loop through `call_soon_threadsafe`, so the
  merged generator is `async` and never blocks. This is what removed the batch ceiling -- the
  platform's 15-minute wall now bounds one ~200s material instead of a whole batch.
* Every other action still goes through `_relay`, a *sync* generator. `StreamingResponse` runs a
  sync iterator through `iterate_in_threadpool`, so each blocking read happens off the loop and each
  line reaches the browser as it arrives. Collecting the lines into a list first would pass every
  functional test and still break the only thing the frontend needs from this endpoint -- progress
  that shows up during a batch rather than after it.

**The fan-out's threads are its own, not anyio's.** anyio's default threadpool has 40 tokens and is
shared with every sync route handler here. A material held for four minutes must not be able to
consume a token `/healthz` needs, because AgentCore kills a task whose health check times out --
which would take every in-flight batch with it. Hence a `ThreadPoolExecutor` sized to the
concurrency cap, and hence `/healthz` being `async def`: it now answers on the loop itself and
cannot queue behind anything.

**The batch-history routes live here and not on the Runtime.** A batch is a unit only the web tier
knows about -- the Runtime is invoked once per material and never sees the group -- so the web tier
is the only component that can record one. `web/batch_history.py` says what the record is and how
its three statuses are derived; this module is only the routes and the wiring that feeds the
recorder from the fanned-out stream.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from http.cookies import SimpleCookie
from typing import Any, AsyncIterator, Dict, Iterator, Optional, Tuple

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
from .batch_history import BatchHistory, BatchRecorder, new_batch_id
from .comment_store import CommentError, CommentService
from .fanout import (
    FANOUT_CONCURRENCY,
    HEARTBEAT,
    HEARTBEAT_SECONDS,
    FanOut,
    build_executor,
    plan_children,
)
from .runtime_client import (
    SSE_CONTENT_TYPE,
    AgentCoreRuntimeClient,
    RuntimeNotConfigured,
    iter_sse_payloads,
    new_session_id,
    read_json,
)
from .question_versions import (
    QuestionVersionError,
    QuestionVersionService,
    build_question_version_service,
)
from .slot_state import SlotStateReader, build_reader

LOG = logging.getLogger(__name__)

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


def _infra_error_body(code: str, message: str, exc: BaseException) -> Dict[str, Any]:
    """An operator's diagnosis in the LOG, a plain sentence in `message`.

    The client saw 「历史记录读取失败 ModuleNotFoundError: No module named 'audio_storage'」 on a
    fresh page. Two things were wrong with that and only one of them was the missing module: a
    Python exception class name is not a sentence anybody outside this repository can act on, and
    putting it in `message` guarantees it reaches the DOM, because `message` is what the frontend
    renders. So the exception goes to the task log (where it is actually useful, with a traceback)
    and `detail.cause` carries the short form for a support conversation -- the frontend renders
    `message` and nothing else.

    Not merely a wording change: `message` is the only field with a rendering contract, so this is
    the one place that can enforce it for every route at once.
    """
    LOG.exception("%s: %s", code, message)
    return _error_body(code, message, cause="%s: %s" % (type(exc).__name__, str(exc)[:300]))


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
                 static_dir: str, *, cookie_secure: bool = False,
                 fanout_concurrency: int = FANOUT_CONCURRENCY,
                 history: Optional[BatchHistory] = None,
                 slot_state: Optional[SlotStateReader] = None,
                 comments: Optional[CommentService] = None,
                 question_versions: Optional[QuestionVersionService] = None) -> None:
        self.auth = auth
        self.runtime = runtime
        self.static_dir = static_dir
        self.cookie_secure = cookie_secure
        # The `_slots/` + `_questions/` reader, for the 题目预览 tab. Built once here rather than per
        # request: `build_reader` constructs an S3 client, and a tab that polls while a set is being
        # generated would otherwise build one per poll. Injectable for the same reason `history` is.
        self.slot_state = slot_state if slot_state is not None else build_reader()
        # Batch history. Injectable so a test can hand in an in-memory store; the default builds
        # itself lazily on first use, so a tier that never sees a batch constructs no AWS client.
        self.history = history if history is not None else BatchHistory()
        self.comments = comments if comments is not None else CommentService()
        self.question_versions = (
            question_versions if question_versions is not None
            else build_question_version_service()
        )
        # email -> (runtimeSessionId, minted_at). Per-user so two reviewers do not share a
        # microVM, and so one user's long batch does not reset another's idle timer.
        #
        # Used by the UNARY actions only. A fanned-out `generate` mints a fresh id per child
        # (fanout.py), which is the point: a shared id would route every child to the one warm
        # microVM and serialise the batch it exists to parallelise.
        self._sessions: Dict[str, Tuple[str, float]] = {}
        self.fanout_concurrency = max(1, fanout_concurrency)
        # One executor for the whole process, built lazily so a tier that never sees a `generate`
        # (the unary tests, a bare `/healthz` probe) spawns no threads at all. It is also the
        # concurrency cap's second line of defence: `max_workers` cannot be exceeded even if a
        # future caller forgets the semaphore.
        self._executor: Optional[Any] = None
        self._executor_lock = threading.Lock()
        self._batch_counter = 0
        self.app = self._build()

    def executor(self) -> Any:
        if self._executor is None:
            with self._executor_lock:
                if self._executor is None:
                    self._executor = build_executor(self.fanout_concurrency)
        return self._executor

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
        async def healthz() -> Dict[str, Any]:
            # Deliberately does not call AWS: a health check that depends on a downstream service
            # turns a Runtime hiccup into a killed web task.
            #
            # `async def` and not `def`: a sync handler is dispatched into anyio's 40-token
            # threadpool, and the whole reason the fan-out has its own executor is that a health
            # check must never queue behind long-lived work. Answering on the loop closes the
            # remaining path by which it could.
            return {
                "status": "ok",
                "runtime_configured": self.runtime.configured,
                "static": os.path.isfile(os.path.join(self.static_dir, "index.html")),
                "user_store": type(self.auth.store).__name__,
                "fanout_concurrency": self.fanout_concurrency,
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

        # ── batch history ────────────────────────────────────────────────────
        #
        # Plain routes rather than more `/api/invocations` actions: these read the web tier's own
        # store and never call the Runtime, so routing them through the proxy would mean signing a
        # SigV4 request to ask ourselves a question. Registered before the SPA catch-all, which
        # would otherwise swallow every GET under this prefix.
        #
        # `/api/batch-history` and NOT `/api/batches`, which the frontend's adapter already owns for
        # the §8 contract's session-scoped batch routes (`frontend/src/api/agentcore.ts`). Those two
        # answer different questions -- "what is in this page session" versus "what was ever
        # generated" -- and sharing a path would make which one you got depend on where the request
        # was issued from.

        @app.get("/api/batch-history")
        async def list_batches(request: Request) -> JSONResponse:
            user = request.scope.get(USER_SCOPE_KEY) or {}
            from starlette.concurrency import run_in_threadpool

            # In a threadpool: `load_all_indexes` is a LIST plus one GET per batch, all blocking
            # boto3. On the loop it would stall `/healthz` for as long as S3 took.
            try:
                batches = await run_in_threadpool(
                    self.history.list_batches, owner=str(user.get("email") or "") or None
                )
            except Exception as exc:  # noqa: BLE001 - surfaced in the frontend's error shape
                return JSONResponse(
                    _infra_error_body(
                        "BATCH_HISTORY_UNAVAILABLE",
                        "历史记录暂时读取不到，请稍后重试。", exc),
                    status_code=502,
                )
            # An EMPTY history is a successful answer, not a failure: `[]` on first use means
            # "nothing generated yet", which the panel renders as 暂无历史批次. Stated here because
            # the route has no other way to distinguish the two -- and a 200 with `[]` is the only
            # shape that lets the frontend distinguish them either.
            return JSONResponse({"batches": batches, "next_cursor": None})

        @app.get("/api/batch-history/{batch_id}")
        async def get_batch(batch_id: str) -> JSONResponse:
            from starlette.concurrency import run_in_threadpool

            try:
                found = await run_in_threadpool(self.history.get_batch, batch_id)
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    _infra_error_body(
                        "BATCH_HISTORY_UNAVAILABLE",
                        "这个批次暂时读取不到，请稍后重试。", exc),
                    status_code=502,
                )
            if found is None:
                return JSONResponse(
                    _error_body("BATCH_NOT_FOUND",
                                "没有找到批次 %s 的历史记录" % batch_id, batch_id=batch_id),
                    status_code=404,
                )
            return JSONResponse(found)

        @app.get("/api/batch-history-material/{material_id}")
        async def get_history_material(material_id: str) -> JSONResponse:
            """One material's artifacts by id alone, for the reader page of a historical batch.

            A separate route rather than a query on `/api/batch-history/{id}`: the reader-page URL is
            `/materials/{id}` and names no batch, so the batch id is genuinely not available to the
            caller. Without this, 阅读全文 on a batch from last week is a link to "材料不存在".
            """
            from starlette.concurrency import run_in_threadpool

            try:
                found = await run_in_threadpool(self.history.get_material, material_id)
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    _infra_error_body(
                        "BATCH_HISTORY_UNAVAILABLE",
                        "这份材料暂时读取不到，请稍后重试。", exc),
                    status_code=502,
                )
            if found is None:
                return JSONResponse(
                    _error_body("MATERIAL_NOT_FOUND",
                                "历史记录里没有材料 %s" % material_id, material_id=material_id),
                    status_code=404,
                )
            return JSONResponse(found)

        @app.get("/api/material-questions/{material_id}")
        async def get_material_questions(material_id: str, batch_id: str = "") -> JSONResponse:
            """The delivered question set for one material, for the 题目预览 tab.

            **200 with `questions: null` rather than a 404 when there is no set.** "No questions yet"
            is the normal state for most of a material's life -- the question stage runs after the
            material and can be paused by the clock -- and a 404 makes the browser's error path the
            usual path, which is how a routine empty state ends up drawn as a failure. A 404 here
            would also be ambiguous in the one way that matters: it cannot distinguish "no such
            material" from "this material has no questions", and the tab must say different things.

            Read-only, and served from `_questions/` directly rather than from the batch record. The
            Runtime writes that prefix when a set clears every gate, so what is there is deliverable
            by construction; going through the batch record instead would mean the web tier had to
            re-derive deliverability from stage events it only partially saw.

            **`batch_id` is optional, and what it buys is the reason there are no questions.** With it
            the slot row is found too, so the tab can say 生成被时钟停在断点、这一位已用尽、整批系统故障
            rather than only 暂无题目. Optional because the reader page's URL carries no batch id in
            every path (a material can be opened straight from the in-session cache), and a missing
            explanation must not cost the questions themselves.
            """
            from starlette.concurrency import run_in_threadpool

            if not self.slot_state.available:
                # No bucket. Local dev, and the honest answer is "cannot know", not "none": drawing
                # 暂无题目 here would tell a developer their generated set was lost.
                return JSONResponse(
                    _error_body("QUESTIONS_UNAVAILABLE",
                                "题目存储未配置，读不到题目。", material_id=material_id),
                    status_code=503,
                )
            try:
                found = await run_in_threadpool(self.slot_state.load_questions, material_id)
            except Exception as exc:  # noqa: BLE001 - the reader swallows its own; this is a backstop
                return JSONResponse(
                    _infra_error_body("QUESTIONS_UNAVAILABLE",
                                      "题目暂时读取不到，请稍后重试。", exc),
                    status_code=502,
                )
            # `package` is the deliverable set inside `QuestionResult.as_dict()`; a stored failure has
            # no such key (slot_store.save_questions' docstring). Unwrapped here so the browser is
            # handed the schema's own shape and needs no knowledge of the result envelope.
            package = found.get("package") if isinstance(found, dict) else None
            body: Dict[str, Any] = {
                "material_id": material_id,
                "questions": package if isinstance(package, dict) else None,
                "slot": None,
                "request_status": None,
            }
            # Only when the questions are absent. A delivered set needs no explanation, and the LIST
            # this costs is not worth paying on the path that already has what it came for.
            if body["questions"] is None and batch_id:
                try:
                    state = await run_in_threadpool(
                        self.slot_state.find_slot, batch_id, material_id)
                except Exception:  # noqa: BLE001 - an explanation is optional; the 200 is not
                    LOG.warning("slot lookup failed for %s/%s", batch_id, material_id,
                                exc_info=True)
                    state = None
                if state:
                    body["slot"] = state.get("slot")
                    body["request_status"] = state.get("request_status")
            return JSONResponse(body)

        # ── personal material comments ─────────────────────────────────────

        @app.get("/api/material-comments/{material_id}")
        async def list_material_comments(material_id: str) -> JSONResponse:
            from starlette.concurrency import run_in_threadpool

            try:
                document = await run_in_threadpool(self.comments.list, material_id)
            except CommentError as exc:
                return JSONResponse(_error_body(exc.code, exc.message), status_code=exc.status)
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    _infra_error_body(
                        "COMMENTS_UNAVAILABLE", "评论暂时读取不到，请稍后重试。", exc),
                    status_code=502,
                )
            return JSONResponse(document)

        @app.post("/api/material-comments/{material_id}")
        async def create_material_comment(material_id: str, request: Request) -> JSONResponse:
            from starlette.concurrency import run_in_threadpool

            body = await _json_body(request)
            try:
                if (
                    isinstance(body, dict)
                    and isinstance(body.get("anchor"), dict)
                    and body["anchor"].get("type") == "question"
                    and self.question_versions is not None
                ):
                    versions = await run_in_threadpool(
                        self.question_versions.list, material_id)
                    requested_version = str(body.get("version_id") or "original")
                    if requested_version != versions.get("active_version_id"):
                        raise CommentError(
                            "COMMENT_VERSION_NOT_ACTIVE",
                            "只能在当前采用的题目版本上添加批注。",
                            409,
                        )
                document = await run_in_threadpool(self.comments.create, material_id, body)
            except CommentError as exc:
                return JSONResponse(_error_body(exc.code, exc.message), status_code=exc.status)
            except QuestionVersionError as exc:
                return JSONResponse(_error_body(exc.code, exc.message), status_code=exc.status)
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    _infra_error_body(
                        "COMMENT_SAVE_FAILED", "评论没有保存成功，请稍后重试。", exc),
                    status_code=502,
                )
            return JSONResponse(document, status_code=201)

        @app.delete("/api/material-comments/{material_id}/{comment_id}")
        async def delete_material_comment(material_id: str, comment_id: str) -> JSONResponse:
            from starlette.concurrency import run_in_threadpool

            try:
                document = await run_in_threadpool(
                    self.comments.delete, material_id, comment_id)
            except CommentError as exc:
                return JSONResponse(_error_body(exc.code, exc.message), status_code=exc.status)
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    _infra_error_body(
                        "COMMENT_DELETE_FAILED", "评论没有删除成功，请稍后重试。", exc),
                    status_code=502,
                )
            return JSONResponse(document)

        @app.get("/api/material-question-versions/{material_id}")
        async def list_question_versions(material_id: str) -> JSONResponse:
            from starlette.concurrency import run_in_threadpool

            if self.question_versions is None:
                return JSONResponse(
                    _error_body("QUESTION_VERSIONS_UNAVAILABLE", "题目版本存储未配置。"),
                    status_code=503,
                )
            try:
                document = await run_in_threadpool(self.question_versions.list, material_id)
                await run_in_threadpool(
                    _reconcile_question_comments, self.comments, material_id, document)
            except QuestionVersionError as exc:
                return JSONResponse(_error_body(exc.code, exc.message), status_code=exc.status)
            except Exception as exc:
                return JSONResponse(_infra_error_body(
                    "QUESTION_VERSIONS_UNAVAILABLE", "题目版本暂时读取不到。", exc),
                    status_code=502)
            return JSONResponse(document)

        @app.post("/api/material-question-versions/{material_id}/{version_id}/adopt")
        async def adopt_question_version(
            material_id: str, version_id: str, request: Request
        ) -> JSONResponse:
            from starlette.concurrency import run_in_threadpool

            if self.question_versions is None:
                return JSONResponse(
                    _error_body("QUESTION_VERSIONS_UNAVAILABLE", "题目版本存储未配置。"),
                    status_code=503)
            user = request.scope.get(USER_SCOPE_KEY) or {}
            try:
                document = await run_in_threadpool(
                    self.question_versions.adopt, material_id, version_id,
                    str(user.get("email") or "reviewer"))
            except QuestionVersionError as exc:
                return JSONResponse(_error_body(exc.code, exc.message), status_code=exc.status)
            except Exception as exc:
                return JSONResponse(_infra_error_body(
                    "QUESTION_VERSION_ADOPT_FAILED", "题目版本没有采用成功。", exc),
                    status_code=502)
            return JSONResponse(document)

        @app.post("/api/material-question-revisions/{material_id}")
        async def revise_material_questions(material_id: str, request: Request) -> Any:
            """Snapshot question comments, then relay one Runtime revision as SSE."""
            from starlette.concurrency import run_in_threadpool

            if self.question_versions is None:
                return JSONResponse(
                    _error_body("QUESTION_VERSIONS_UNAVAILABLE", "题目版本存储未配置。"),
                    status_code=503)
            body = _as_dict(await _json_body(request))
            base_version_id = str(body.get("base_version_id") or "")
            if not base_version_id:
                return JSONResponse(
                    _error_body("bad_request", "base_version_id is required"), status_code=400)
            user = request.scope.get(USER_SCOPE_KEY) or {}
            actor = str(user.get("email") or "reviewer")
            try:
                comments_doc = await run_in_threadpool(self.comments.list, material_id)
                requested = body.get("comment_ids")
                requested_ids = (
                    {str(value) for value in requested} if isinstance(requested, list) else None)
                comments = [
                    row for row in comments_doc.get("comments", [])
                    if isinstance(row, dict)
                    and (row.get("anchor") or {}).get("type") == "question"
                    and row.get("version_id") == base_version_id
                    and row.get("status") == "open"
                    and (requested_ids is None or str(row.get("id")) in requested_ids)
                ]
                if requested_ids is not None and {
                    str(row.get("id")) for row in comments
                } != requested_ids:
                    raise QuestionVersionError(
                        "QUESTION_COMMENT_NOT_FOUND",
                        "部分待修改批注已被删除，请刷新后重新提交。", 409)
                if not comments:
                    raise QuestionVersionError(
                        "NO_QUESTION_COMMENTS", "请先添加至少一条题目批注。")
                versions = await run_in_threadpool(self.question_versions.list, material_id)
                if versions.get("active_version_id") != base_version_id:
                    raise QuestionVersionError(
                        "BASE_VERSION_NOT_ACTIVE", "只能基于当前采用版本提交修改。", 409)
                base = await run_in_threadpool(
                    self.question_versions.load, material_id, base_version_id)
                material_record = await run_in_threadpool(
                    self.history.get_material, material_id)
                if not isinstance(material_record, dict):
                    raise QuestionVersionError(
                        "MATERIAL_NOT_FOUND", "没有找到这套题目的材料。", 404)
                material = material_record.get("material")
                blueprint = material_record.get("blueprint")
                if not isinstance(material, dict) or not isinstance(blueprint, dict):
                    raise QuestionVersionError(
                        "MATERIAL_ARTIFACTS_MISSING", "材料或信息点蓝图不完整。", 409)
                revision = await run_in_threadpool(
                    self.question_versions.reserve,
                    material_id, base_version_id, comments, actor)
            except QuestionVersionError as exc:
                return JSONResponse(_error_body(exc.code, exc.message), status_code=exc.status)
            except Exception as exc:
                return JSONResponse(_infra_error_body(
                    "QUESTION_REVISION_PREPARE_FAILED", "题目修改任务没有创建成功。", exc),
                    status_code=502)

            payload = {
                "action": "revise_questions_from_comments",
                "material_id": material_id,
                "request_id": revision["request_id"],
                "base_version_id": base_version_id,
                "material": material,
                "blueprint": blueprint,
                "package": base["package"],
                "base_version": base,
                "comments": comments,
                "actor": actor,
            }
            session_id = self.session_for(actor)
            try:
                content_type, runtime_body, _ = await run_in_threadpool(
                    self.runtime.invoke, payload, session_id=session_id)
            except Exception as exc:
                await run_in_threadpool(
                    self.question_versions.fail_request, material_id, revision,
                    "%s: %s" % (type(exc).__name__, str(exc)[:300]))
                return JSONResponse(_infra_error_body(
                    "QUESTION_REVISION_INVOKE_FAILED", "题目修改服务暂时没有响应。", exc),
                    status_code=502)
            if SSE_CONTENT_TYPE not in content_type:
                await run_in_threadpool(
                    self.question_versions.fail_request, material_id, revision,
                    "Runtime returned a non-streaming response")
                return JSONResponse(
                    _error_body("QUESTION_REVISION_BAD_RESPONSE",
                                "题目修改服务返回了错误格式。"), status_code=502)
            return StreamingResponse(
                _relay_question_revision(
                    runtime_body, self.question_versions, material_id, revision),
                media_type=SSE_CONTENT_TYPE,
                headers=_SSE_HEADERS,
            )

        @app.post("/api/batch-history/{batch_id}/submit")
        async def submit_batch(batch_id: str, request: Request) -> JSONResponse:
            """Record the 已提交 status. The transition the backend did not have.

            Deliberately not `action: select`: submitting for review is a reviewer stating their
            picks, while `select` claims the candidate group, discards the siblings and pays Polly.
            See web/batch_history.py's docstring.
            """
            user = request.scope.get(USER_SCOPE_KEY) or {}
            body = _as_dict(await _json_body(request))
            raw = body.get("material_ids")
            if not isinstance(raw, list):
                return JSONResponse(
                    _error_body("bad_request", "material_ids must be a list"), status_code=400,
                )
            from starlette.concurrency import run_in_threadpool

            try:
                view = await run_in_threadpool(
                    self.history.submit, batch_id, [str(m) for m in raw],
                    actor=str(user.get("email") or "reviewer"),
                )
            except KeyError:
                return JSONResponse(
                    _error_body("BATCH_NOT_FOUND",
                                "没有找到批次 %s 的历史记录" % batch_id, batch_id=batch_id),
                    status_code=404,
                )
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    _infra_error_body(
                        "BATCH_SUBMIT_FAILED",
                        "提交状态没有记录成功，请稍后重试。", exc),
                    status_code=502,
                )
            return JSONResponse(view)

        @app.post("/api/batch-history/{batch_id}/withdraw")
        async def withdraw_batch(batch_id: str, request: Request) -> JSONResponse:
            """Undo a submission. The counterpart `submit` shipped without.

            `material_ids` withdraws those picks and leaves the batch submitted with a shorter list;
            omitting it withdraws the batch. Without this route the queue page's 撤回 button only
            emptied the browser's local list, and the history panel went on showing 已提交 with no
            way to clear it.
            """
            body = _as_dict(await _json_body(request))
            raw = body.get("material_ids")
            if raw is not None and not isinstance(raw, list):
                return JSONResponse(
                    _error_body("bad_request", "material_ids must be a list when present"),
                    status_code=400,
                )
            from starlette.concurrency import run_in_threadpool

            try:
                view = await run_in_threadpool(
                    self.history.withdraw, batch_id,
                    None if raw is None else [str(m) for m in raw],
                )
            except KeyError:
                return JSONResponse(
                    _error_body("BATCH_NOT_FOUND",
                                "没有找到批次 %s 的历史记录" % batch_id, batch_id=batch_id),
                    status_code=404,
                )
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    _infra_error_body(
                        "BATCH_WITHDRAW_FAILED",
                        "撤回状态没有记录成功，请稍后重试。", exc),
                    status_code=502,
                )
            return JSONResponse(view)

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
        """Proxy one payload to the Runtime. JSON in, JSON or SSE out.

        `generate` and `generate_sets` are the fanned-out cases: N invocations, one per material,
        merged into one stream. Everything else is a single call relayed as-is.

        `generate_sets` is fanned out for exactly the reason `generate` is -- one invocation per
        material, so the 900s wall applies to one material rather than to the whole batch -- and it has
        to be, not merely may be: the alternative is one invocation carrying N sets, and N × ~200s of
        generation plus the question stages does not fit under one wall. What differs is only what each
        child promises about its own material, which is the backend's business (`backend/app.py`), and
        the per-child request ids `plan_children` mints so the children's slot records do not collide.
        """
        user = request.scope.get(USER_SCOPE_KEY) or {}
        payload = await _json_body(request)
        if not isinstance(payload, dict):
            return JSONResponse(_error_body("bad_request", "payload must be a JSON object"),
                                status_code=400)

        if str(payload.get("action") or "generate") in ("generate", "generate_sets"):
            if not self.runtime.configured:
                # A per-batch precondition, not a per-child failure: with no Runtime ARN every
                # child would fail identically, and N cards reading "RuntimeNotConfigured" tells
                # the operator far less than one 503 naming the missing variable.
                return JSONResponse(
                    _error_body("RUNTIME_NOT_CONFIGURED",
                                "AGENT_RUNTIME_ARN is not set; the web tier has no Runtime to "
                                "call"),
                    status_code=503,
                )
            return self._fanned_out_generate(payload, owner=str(user.get("email") or ""))

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
                _infra_error_body(
                    "RUNTIME_INVOKE_FAILED",
                    "后端服务暂时没有响应，请稍后重试。", exc),
                status_code=502,
            )

        if SSE_CONTENT_TYPE in content_type:
            return StreamingResponse(_relay(body), media_type=SSE_CONTENT_TYPE,
                                     headers=_SSE_HEADERS)
        return JSONResponse(read_json(body))

    def _fanned_out_generate(self, payload: Dict[str, Any], *,
                             owner: str = "") -> StreamingResponse:
        """One invocation per material, merged into the single stream the frontend already reads.

        Returns immediately with headers: the children are started by the generator, so the browser
        can render its skeleton grid and the `batch_started` frame lands before the first invoke's
        response headers do. Nothing is awaited here, which is also why there is no error branch --
        a child that cannot be invoked at all becomes a `material_failed` frame inside the stream
        rather than a 502 that would lose the N-1 children that were fine.

        The batch id is per-request and namespaces two things: the backend's candidate groups (two
        materials for the same scenario in one submission must compete for one user choice, and two
        submissions must not) and now the batch record too, which is why `new_batch_id` keeps the
        same format rather than minting something prettier.

        The recorder is attached here and fed by `_frames`. It is what makes a batch survive the
        request that created it -- see web/batch_history.py.
        """
        self._batch_counter += 1
        batch_id = new_batch_id(self._batch_counter)
        children, slot_ids = plan_children(payload, batch_id=batch_id)
        # `batch_id` reaches the browser through `batch_started`. Without it the frontend minted its
        # own id, put that in `/batches/:batchId`, and the history panel then asked about a batch id
        # nothing had ever recorded -- see FanOut.events().
        fan = FanOut(self.runtime, children, slot_ids, executor=self.executor(),
                     concurrency=self.fanout_concurrency, batch_id=batch_id)
        recorder = self.history.recorder(
            batch_id, owner=owner, requested_total=len(slot_ids),
            # The plan's own view of the batch shape, so the history panel can render a scenario tag
            # and a set count for a batch that produced nothing at all. Derived from `children`
            # rather than from the payload because `plan_children` has already resolved counts and
            # the custom scenario into one material per child.
            scenarios=_scenario_shape(children),
            # 自定义场景的用户原文。历史面板要显示它，而它在别处都不存在：材料自带的 `scenario`
            # 是模型扩写的完整英文句，场景目录里也没有自定义场景的条目。
            custom_label=fan.custom_label(),
        )
        return StreamingResponse(_frames(fan, recorder), media_type=SSE_CONTENT_TYPE,
                                 headers=_SSE_HEADERS)

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


# Shared by both SSE paths. `no-transform` and `X-Accel-Buffering: no` are what stop an
# intermediary re-buffering the stream; no Content-Length, because the length is not knowable.
_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",  # harmless here, and correct if a proxy appears
    "Connection": "keep-alive",
}


def _scenario_shape(children: Any) -> list:
    """`[{scenario_key, count}]` in plan order, for the history panel's scenario tags.

    Collapsed to one row per scenario with a count, because that is what the panel renders
    ("🏨 酒店预订 × 2"). Order is the plan's order, which is the order the user picked the scenarios
    in -- `plan_children` mirrors `backend/request.py`'s expansion, and the frontend's cards are laid
    out the same way, so the panel's tags and the cards cannot disagree about which scenario came
    first.
    """
    order: list = []
    counts: Dict[str, int] = {}
    for child in children:
        key = str(getattr(child, "scenario", "") or "")
        if key not in counts:
            order.append(key)
            counts[key] = 0
        counts[key] += len(getattr(child, "slot_ids", ()) or ())
    return [{"scenario_key": key, "count": counts[key]} for key in order]


async def _frames(fan: FanOut, recorder: Optional[BatchRecorder] = None) -> AsyncIterator[bytes]:
    """Frame the merged event stream for the browser, one event per ASGI body message.

    An `async` generator, unlike `_relay`: the blocking reads already live on the fan-out's own
    threads, so there is nothing here for `iterate_in_threadpool` to protect the loop from. Each
    `yield` is still one `http.response.body` with `more_body=True`, which is the observable
    property the tests assert and the only thing progressive delivery means at this layer.

    The recorder is fed here, and its every method is non-blocking on purpose: it hands snapshots to
    its own worker thread. Writing S3 inline would put a multi-hundred-millisecond PUT between two
    material cards, which is the one thing this generator exists not to do.
    """
    try:
        if recorder is not None:
            # Before the first frame, so a batch that dies immediately still leaves a record saying
            # it was asked for.
            recorder.start()
        async for event in fan.events():
            if event is HEARTBEAT:
                # An SSE **comment**, not an event: it must not reach a reducer, mint a `seq`, or be
                # recorded. Its only job is to put bytes on the wire so no intermediary calls a
                # silent-but-healthy batch a dead connection (see fanout.HEARTBEAT).
                yield b": hb\n\n"
                continue
            if recorder is not None:
                recorder.on_event(event)
            yield ("data: %s\n\n" % json.dumps(event, ensure_ascii=False)).encode("utf-8")
    except Exception as exc:  # noqa: BLE001 - mid-stream failure must still reach the client
        # Same contract as `_relay`: the frontend treats a stream that ends without a terminal
        # event as a lost connection, so name the cause instead of going silent. Reachable only if
        # the merge itself breaks -- a child's failure is already a `material_failed` frame.
        broken = {"type": "batch_failed", "reason": "stream_error",
                  "detail": "%s: %s" % (type(exc).__name__, str(exc)[:300])}
        yield ("data: %s\n\n" % json.dumps(broken)).encode("utf-8")
    finally:
        fan.close()
        if recorder is not None:
            # Also runs on a client disconnect (GeneratorExit), which is the case that matters: an
            # abandoned batch keeps whatever materials it already delivered.
            recorder.close()


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


def _relay_question_revision(
    body: Any,
    service: QuestionVersionService,
    material_id: str,
    revision: Dict[str, Any],
) -> Iterator[bytes]:
    """Relay one revision, verifying completed versions before exposing success."""
    terminal = False
    try:
        for payload in _iter_revision_payloads(body):
            if payload is HEARTBEAT:
                yield b": hb\n\n"
                continue
            try:
                decoded = json.loads(payload)
            except ValueError:
                decoded = {}
            event_type = decoded.get("type") if isinstance(decoded, dict) else None
            if event_type == "question_revision_completed":
                version_id = decoded.get("version_id")
                try:
                    if not isinstance(version_id, str):
                        raise QuestionVersionError(
                            "QUESTION_VERSION_NOT_FOUND", "Runtime 没有返回题目版本 ID。", 502)
                    service.load(material_id, version_id)
                except Exception:
                    message = "修改结果没有写入题目版本存储，当前版本未改变。"
                    service.fail_request(material_id, revision, message)
                    failed = {
                        "type": "question_revision_failed",
                        "request_id": revision.get("request_id"),
                        "message": message,
                    }
                    yield (
                        "data: %s\n\n" % json.dumps(failed, ensure_ascii=False)
                    ).encode("utf-8")
                    return
            if event_type in {
                "question_revision_completed",
                "question_revision_no_change",
                "question_revision_needs_replan",
                "question_revision_needs_material",
                "question_revision_failed",
            }:
                terminal = True
            yield ("data: %s\n\n" % payload).encode("utf-8")
        if not terminal:
            failed = {
                "type": "question_revision_failed",
                "request_id": revision.get("request_id"),
                "message": "题目修改连接提前结束，当前版本未改变。",
            }
            yield ("data: %s\n\n" % json.dumps(failed, ensure_ascii=False)).encode("utf-8")
    except Exception as exc:  # noqa: BLE001 - the browser is owed a revision-shaped terminal event
        failed = {
            "type": "question_revision_failed",
            "request_id": revision.get("request_id"),
            "message": "题目修改连接中断，当前版本未改变。",
        }
        yield ("data: %s\n\n" % json.dumps(failed, ensure_ascii=False)).encode("utf-8")
        LOG.warning("question revision stream failed", exc_info=exc)
    finally:
        closer = getattr(body, "close", None)
        if callable(closer):
            closer()
        # A broken Web-to-Runtime stream does not prove the Runtime stopped. Keep the durable
        # running marker so a retry cannot launch a second paid Agent while the first may still
        # finish and commit its immutable version. Runtime owns the terminal transition.


def _iter_revision_payloads(body: Any) -> Iterator[Any]:
    """Read a blocking Runtime body on a worker while yielding keepalive sentinels."""
    events: "queue.Queue[Tuple[str, Any]]" = queue.Queue()

    def read() -> None:
        try:
            for payload in iter_sse_payloads(body):
                events.put(("payload", payload))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the response thread
            events.put(("error", exc))
        finally:
            events.put(("end", None))

    worker = threading.Thread(
        target=read,
        name="question-revision-sse",
        daemon=True,
    )
    worker.start()
    try:
        while True:
            try:
                kind, value = events.get(timeout=HEARTBEAT_SECONDS)
            except queue.Empty:
                yield HEARTBEAT
                continue
            if kind == "payload":
                yield value
            elif kind == "error":
                raise value
            else:
                return
    finally:
        closer = getattr(body, "close", None)
        if callable(closer):
            closer()
        worker.join(timeout=1.0)


def _reconcile_question_comments(
    comments: CommentService,
    material_id: str,
    versions: Dict[str, Any],
) -> None:
    """Apply a durable terminal revision to its exact comment snapshot."""
    revision = versions.get("revision_request")
    if not isinstance(revision, dict):
        return
    status = revision.get("status")
    source = revision.get("source_comments")
    terminal_outcomes = {
        "completed": "resolved",
        "no_change": "no_change",
        "replan_questions": "needs_replan",
        "needs_material_revision": "needs_material",
    }
    if status not in terminal_outcomes or not isinstance(source, list):
        return
    comment_ids = [
        str(row.get("id"))
        for row in source
        if isinstance(row, dict) and str(row.get("id") or "")
    ]
    if not comment_ids:
        return
    dispositions = revision.get("comment_outcomes")
    if not isinstance(dispositions, list):
        legacy_reasons = {
            str(row.get("comment_id") or ""): row
            for row in revision.get("reasons") or []
            if isinstance(row, dict) and str(row.get("comment_id") or "")
        }
        dispositions = [
            {
                **legacy_reasons.get(comment_id, {}),
                "comment_id": comment_id,
                "outcome": {
                    "completed": "question_only",
                    "no_change": "no_change",
                    "replan_questions": "replan_questions",
                    "needs_material_revision": "revise_material",
                }[status],
            }
            for comment_id in comment_ids
        ]
    settlement_status = {
        "question_only": "resolved",
        "no_change": "no_change",
        "replan_questions": "needs_replan",
        "revise_material": "needs_material",
    }
    grouped: Dict[str, list[Dict[str, Any]]] = {}
    for row in dispositions:
        if not isinstance(row, dict):
            continue
        outcome = str(row.get("outcome") or "")
        # A higher-layer route did not execute question-only changes.
        if outcome == "question_only" and status != "completed":
            continue
        if outcome in settlement_status:
            grouped.setdefault(outcome, []).append(row)
    for outcome, rows in grouped.items():
        comments.settle_revision(
            material_id,
            comment_ids=[str(row.get("comment_id") or "") for row in rows],
            base_version_id=str(revision.get("base_version_id") or "original"),
            request_id=str(revision.get("request_id") or ""),
            outcome=settlement_status[outcome],
            resolved_by_version_id=(
                str(revision.get("version_id"))
                if outcome == "question_only" and status == "completed"
                else None
            ),
            reasons=rows,
        )


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
        fanout_concurrency=_int_env(env, "WEB_FANOUT_CONCURRENCY", FANOUT_CONCURRENCY),
    )


def _int_env(env: Dict[str, str], name: str, default: int) -> int:
    """A positive integer from the environment, or the default.

    Lenient rather than fatal: a typo'd concurrency must not stop the container from booting, since
    the consequence of the default is a slower batch and the consequence of a crash-loop is no
    service at all.
    """
    try:
        return max(1, int(str(env.get(name) or default)))
    except (TypeError, ValueError):
        return default


def create_app() -> FastAPI:
    return build_tier().app


# Module-level app for `uvicorn web.app:app`. Built at import so a misconfiguration (no
# SESSION_SECRET with a shared store) crashes the container at start rather than at first login.
app = create_app()


if __name__ == "__main__":  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "80")))
