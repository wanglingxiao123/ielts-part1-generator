"""Admin-only prompt configuration and isolated, durable trial results."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import Request
from fastapi.responses import JSONResponse
from prompt_config.store import Conflict, encode
from .runtime_client import read_json


class TrialBusy(Conflict):
    code = "TRIAL_BUSY"


class TrialStarting(ValueError):
    code = "TRIAL_STARTING"
    status = 503


class TrialRequestConflict(Conflict):
    code = "TRIAL_REQUEST_CONFLICT"


def install(app, tier, user_scope_key):
    from .prompt_scenarios import install as install_scenarios
    install_scenarios(app, tier, user_scope_key)
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
        return JSONResponse({"error": {"code": getattr(exc, "code",
                            "PROMPT_CONFLICT" if isinstance(exc, Conflict) else "PROMPT_INVALID"),
                            "message": str(exc)}},
                            getattr(exc, "status", 409 if isinstance(exc, Conflict) else 400))

    @app.get("/api/admin/prompts")
    async def get_prompts(request: Request):
        if response := denied(request):
            return response
        return await asyncio.to_thread(state)

    @app.get("/api/admin/prompts/tests")
    async def get_tests(request: Request, offset: int = 0):
        if response := denied(request):
            return response
        if offset < 0:
            return error(ValueError("无效的历史记录页。"))
        return await asyncio.to_thread(trial_history, tier, offset)

    @app.get("/api/admin/prompts/releases/{release_id}")
    async def get_release(release_id: str, request: Request):
        if response := denied(request):
            return response
        try:
            return await asyncio.to_thread(tier.prompts.release_detail, release_id)
        except ValueError as exc:
            return error(exc)

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
            elif operation == "restore-defaults":
                default_id = tier.prompts.bundled()["release_id"]
                if body.get("release_id") != default_id:
                    raise Conflict("内置默认版本已改变，请刷新后重新确认。")
                if body.get("sync_draft") is True:
                    if "draft_etag" not in body:
                        raise ValueError("请刷新并确认需要恢复的共享草稿。")
                    notice = await asyncio.to_thread(
                        tier.prompts.restore_defaults, actor, body.get("expected_etag"),
                        default_id, body["draft_etag"])
                    return {**await asyncio.to_thread(state), "operation_notice": notice}
                await asyncio.to_thread(tier.prompts.publish, actor, body.get("expected_etag"),
                                        release_id=default_id)
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
        return await asyncio.to_thread(trial_detail, tier, record)


def trial_status(record):
    if record["status"] == "running" and time.time() - record["created_at"] > 1200:
        return dict(record, status="failed", error="试运行已超时或服务重启，请重新测试。")
    return record


def trial_detail(tier, record):
    record = trial_status(dict(record))
    # Legacy records retain their real provenance; never substitute today's defaults.
    for side, ref in (("baseline", record.get("baseline_snapshot_release") or record.get("baseline_release")),
                      ("draft", record.get("draft_release"))):
        field = side + "_prompt"
        if field not in record and ref:
            bundle, _ = tier.prompts.storage.read("releases/" + ref + ".json")
            if bundle and record.get("prompt_id") in bundle.get("values", {}):
                record[field] = bundle["values"][record["prompt_id"]]
    return record


def trial_history(tier, offset=0):
    keys = tier.prompts.storage.list_keys("tests/")
    def summary(key):
        record, _ = tier.prompts.storage.read(key)
        if record is None:
            return None
        record = trial_status(record)
        fields = ("test_id", "prompt_id", "model_id", "created_at", "status",
                  "actor", "baseline_release", "draft_release")
        return {**{field: record.get(field) for field in fields}, "has_input": "input" in record}
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = [row for row in pool.map(summary, keys) if row]
    records.sort(key=lambda row: (row["created_at"], row["test_id"]), reverse=True)
    return {"items": records[offset:offset + 20],
            "next_offset": offset + 20 if offset + 20 < len(records) else None}


def start_trial(tier, body, actor):
    from prompt_config.catalogue import defaults
    request_id = body.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id)
    ):
        raise ValueError("缺少有效的请求编号。")
    # Legacy clients without an ID still work during a rolling deployment.
    test_id = (hashlib.sha256((actor + "\0" + request_id).encode()).hexdigest()[:32]
               if request_id is not None else uuid.uuid4().hex)
    fingerprint = hashlib.sha256(encode(body)).hexdigest()
    key = "tests/" + test_id + ".json"
    store = tier.prompts.storage

    def existing_result():
        existing, _ = store.read(key)
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                raise TrialRequestConflict("同一请求编号对应不同测试内容，请重新发起测试。")
            return {"test_id": test_id, "reused": True}

    if found := existing_result():
        return found
    prompt_id, sample = body.get("prompt_id"), body.get("input")
    if prompt_id not in defaults() or not isinstance(sample, str) or not 1 <= len(sample) <= 32000:
        raise ValueError("请选择提示词并输入不超过 32000 字符的测试样例。")
    draft, etag = store.read("draft.json")
    if draft is None or etag != body.get("draft_etag"):
        raise Conflict("草稿已改变，请保存并重新检查。")
    # One bounded trial at a time prevents accidental expensive duplicate-click fan-out.
    lease, lease_etag = store.read("trial-lease.json")
    if lease and lease.get("expires", 0) > time.time():
        if found := existing_result():
            return found
        if lease.get("test_id") == test_id:
            raise TrialStarting("正在启动这次测试，请稍后重试确认同一请求。")
        raise TrialBusy("已有试运行正在执行，请等待完成后重试。")
    try:
        lease_etag = store.write("trial-lease.json",
                                {"test_id": test_id, "expires": time.time() + 1200}, lease_etag)
    except Conflict:
        if found := existing_result():
            return found
        raise TrialStarting("测试正在排队确认，请稍后重试同一请求。")

    def release_lease():
        try:
            store.write("trial-lease.json", {"expires": 0}, lease_etag)
        except Conflict:
            pass

    try:
        # The owner may have completed since the initial read and released its lease.
        if found := existing_result():
            release_lease()
            return found
        baseline = tier.prompts.resolve()
        baseline_source = baseline["release_id"]
        # Even bundled baseline is persisted, so Web and Runtime image mismatch cannot swap it.
        baseline = tier.prompts.snapshot(baseline["values"], actor, trial=True)
        candidate = tier.prompts.snapshot(draft["values"], actor, trial=True)
        record = dict(test_id=test_id, fingerprint=fingerprint, status="running",
                      actor=actor, created_at=time.time(),
                      prompt_id=prompt_id, model_id=body.get("model_id") or "",
                      baseline_release=baseline_source, draft_release=candidate["release_id"],
                      baseline_digest=baseline["digest"], draft_digest=candidate["digest"],
                      baseline_snapshot_release=baseline["release_id"],
                      input=sample, baseline_prompt=baseline["values"][prompt_id],
                      draft_prompt=candidate["values"][prompt_id], draft_etag=etag)
        token = store.write(key, record)
    except Exception:
        release_lease()
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
                store.write(key, record, token)
            finally:
                release_lease()
    try:
        threading.Thread(target=run, daemon=True, name="prompt-trial-" + test_id[:8]).start()
    except Exception:
        logging.getLogger(__name__).exception("Prompt trial worker start failed id=%s", test_id)
        record.update(status="failed", finished_at=time.time(),
                      error="试运行未能启动，请发起新的测试。")
        try:
            store.write(key, record, token)
        finally:
            release_lease()
    return {"test_id": test_id, "reused": False}
