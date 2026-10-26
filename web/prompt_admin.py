"""Admin-only prompt configuration and isolated, durable trial results."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import threading
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse
from prompt_config.store import Conflict
from .runtime_client import read_json


def install(app, tier, user_scope_key):
    def denied(request):
        if not (request.scope.get(user_scope_key) or {}).get("is_admin"):
            return JSONResponse({"error": {"code": "FORBIDDEN", "message": "仅管理员可管理提示词。"}}, 403)

    def state():
        result = tier.prompts.state()
        root = Path(__file__).resolve().parents[1] / "skills"
        # Allowlisted Markdown only. Never accept a filesystem path from a request.
        result["skills"] = [{"path": str(p.relative_to(root)), "text": p.read_text()}
                            for p in sorted(root.rglob("*.md"))]
        return result

    def error(exc):
        return JSONResponse({"error": {"code": "PROMPT_CONFLICT" if isinstance(exc, Conflict)
                                      else "PROMPT_INVALID", "message": str(exc)}},
                            409 if isinstance(exc, Conflict) else 400)

    @app.get("/api/admin/prompts")
    async def get_prompts(request: Request):
        if response := denied(request):
            return response
        return await asyncio.to_thread(state)

    @app.post("/api/admin/prompts/{operation}")
    async def change_prompts(operation: str, request: Request):
        if response := denied(request):
            return response
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求必须为对象。")
            actor = request.scope[user_scope_key]["email"]
            if operation == "draft":
                await asyncio.to_thread(tier.prompts.save_draft, body.get("values"), actor,
                                        body.get("expected_etag"))
            elif operation == "publish":
                await asyncio.to_thread(tier.prompts.publish, actor, body.get("expected_etag"),
                                        draft_etag=body.get("draft_etag"))
            elif operation == "rollback":
                if not isinstance(body.get("release_id"), str):
                    raise ValueError("请选择需要回退的版本。")
                await asyncio.to_thread(tier.prompts.publish, actor, body.get("expected_etag"),
                                        release_id=body["release_id"])
            elif operation == "test":
                if not tier.runtime.configured:
                    raise ValueError("尚未配置模型运行服务。")
                return await asyncio.to_thread(start_trial, tier, body, actor)
            else:
                return JSONResponse({"error": {"message": "未知操作。"}}, 404)
            return await asyncio.to_thread(state)
        except (ValueError, TypeError) as exc:
            return error(exc)

    @app.get("/api/admin/prompts/test/{test_id}")
    async def get_test(test_id: str, request: Request):
        if response := denied(request):
            return response
        if len(test_id) != 32 or any(c not in "0123456789abcdef" for c in test_id):
            return JSONResponse({"error": {"message": "试运行不存在。"}}, 404)
        record, _ = await asyncio.to_thread(tier.prompts.storage.read, "tests/" + test_id + ".json")
        if record is None:
            return JSONResponse({"error": {"message": "试运行不存在。"}}, 404)
        if record["status"] == "running" and time.time() - record["created_at"] > 1200:
            record = dict(record, status="failed", error="试运行已超时或服务重启，请重新测试。")
        return record


def start_trial(tier, body, actor):
    from prompt_config.catalogue import defaults
    prompt_id, sample = body.get("prompt_id"), body.get("input")
    if prompt_id not in defaults() or not isinstance(sample, str) or not 1 <= len(sample) <= 32000:
        raise ValueError("请选择提示词并输入不超过 32000 字符的测试样例。")
    draft, etag = tier.prompts.storage.read("draft.json")
    if draft is None or etag != body.get("draft_etag"):
        raise Conflict("草稿已改变，请保存并重新检查。")
    # One bounded trial at a time prevents accidental expensive duplicate-click fan-out.
    lease, lease_etag = tier.prompts.storage.read("trial-lease.json")
    if lease and lease.get("expires", 0) > time.time():
        raise Conflict("已有试运行正在执行，请等待完成后重试。")
    test_id = uuid.uuid4().hex
    lease_etag = tier.prompts.storage.write("trial-lease.json",
                                           {"test_id": test_id, "expires": time.time() + 1200}, lease_etag)
    try:
        baseline = tier.prompts.resolve()
        baseline_source = baseline["release_id"]
        # Even bundled baseline is persisted, so Web and Runtime image mismatch cannot swap it.
        baseline = tier.prompts.snapshot(baseline["values"], actor, trial=True)
        candidate = tier.prompts.snapshot(draft["values"], actor, trial=True)
        record = dict(test_id=test_id, status="running", actor=actor, created_at=time.time(),
                      prompt_id=prompt_id, model_id=body.get("model_id") or "",
                      baseline_release=baseline_source, draft_release=candidate["release_id"],
                      baseline_digest=baseline["digest"], draft_digest=candidate["digest"],
                      draft_etag=etag)
        key = "tests/" + test_id + ".json"
        token = tier.prompts.storage.write(key, record)
    except Exception:
        tier.prompts.storage.write("trial-lease.json", {"expires": 0}, lease_etag)
        raise

    def run():
        try:
            for label, bundle in (("baseline", baseline), ("draft", candidate)):
                payload = {"action": "prompt_trial", "prompt_id": prompt_id, "input": sample,
                           "model_id": record["model_id"],
                           "_prompt_release": {k: bundle[k] for k in ("release_id", "digest")}}
                _, response, _ = tier.runtime.invoke(payload, session_id="prompt-trial-" + uuid.uuid4().hex)
                try:
                    result = read_json(response)
                finally:
                    if hasattr(response, "close"):
                        response.close()
                if not isinstance(result, dict) or not isinstance(result.get("output"), str):
                    raise ValueError("试运行未返回有效结果。")
                record[label] = result["output"]
                record["model_id"] = result.get("model_id", record["model_id"])
            record["status"] = "completed"
        except Exception:
            logging.getLogger(__name__).exception("Prompt trial failed id=%s", test_id)
            record.update(status="failed", error="模型试运行失败，请查看后台日志；当前发布版本未改变。")
        finally:
            record["finished_at"] = time.time()
            try:
                tier.prompts.storage.write(key, record, token)
            finally:
                try:
                    tier.prompts.storage.write("trial-lease.json", {"expires": 0}, lease_etag)
                except Conflict:
                    pass
    threading.Thread(target=run, daemon=True, name="prompt-trial-" + test_id[:8]).start()
    return {"test_id": test_id}
