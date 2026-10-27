"""Administrator-owned full pipeline comparisons; no adoption or review submission."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import Request
from fastapi.responses import JSONResponse

from prompt_config.store import Conflict, encode
from .runtime_client import iter_sse_payloads, new_session_id, read_json

LOG = logging.getLogger(__name__)
ROOT = "scenario-runs/"
WALL = 3900
SIDES = ("baseline", "candidate")


def unary(tier, action):
    _, stream, _ = tier.runtime.invoke({"action": action}, session_id=new_session_id())
    try:
        result = read_json(stream)
    finally:
        stream.close()
    if not isinstance(result, dict) or "error" in result:
        raise ValueError("场景或模型目录读取失败，请稍后重试。")
    return result


def side_key(run_id, side):
    return f"{ROOT}{run_id}/{side}.json"


def projected(row):
    if row["status"] in ("queued", "running") and time.time() > row["deadline_at"]:
        return dict(row, status="interrupted", error="执行超时或服务中断，已保存的结果仍可查看。")
    return row


def detail(tier, run_id):
    record, _ = tier.prompts.storage.read(f"{ROOT}{run_id}.json")
    if record is None:
        return None
    result = dict(record, sides={})
    for side in SIDES:
        row, _ = tier.prompts.storage.read(side_key(run_id, side))
        result["sides"][side] = projected(row) if row else {
            "status": "interrupted", "error": "执行记录不完整，未自动重启。"}
    states = [row["status"] for row in result["sides"].values()]
    result["status"] = ("running" if any(x in ("queued", "running") for x in states)
                        else "completed" if all(x == "succeeded" for x in states)
                        else "finished_with_issues")
    for row in result["sides"].values():
        row["attempt"] = row.get("attempt", 0)
        row["retry_action"] = None
        if result["status"] != "running" and row["status"] != "succeeded":
            slots = (row.get("summary") or {}).get("slots") or []
            row["retry_action"] = ("resume" if any(
                slot.get("resumable") and not slot.get("system_fault") for slot in slots
            ) else "regenerate")
            if row["retry_action"] == "resume" and any(
                (slot.get("last_failure") or {}).get("reason") == "time_budget"
                for slot in slots
            ):
                row["error"] = "本次执行时间已用完，已保存检查点，可以继续生成。"
    return result


def history(tier, offset):
    keys = [key for key in tier.prompts.storage.list_keys(ROOT)
            if re.fullmatch(r"scenario-runs/[a-f0-9]{32}\.json", key)]
    def load(key):
        record, _ = tier.prompts.storage.read(key)
        return record
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = [row for row in pool.map(load, keys) if row]
    rows.sort(key=lambda row: (row["created_at"], row["run_id"]), reverse=True)
    items = []
    for row in rows[offset:offset + 20]:
        run = detail(tier, row["run_id"])
        items.append({k: run[k] for k in
                      ("run_id", "created_at", "actor", "scenario", "model_id", "status")})
    return {"items": items, "next_offset": offset + 20 if len(rows) > offset + 20 else None}


def version(tier, selection, body):
    if selection == "active":
        state = tier.prompts.state()
        if state["active_etag"] != body.get("active_etag"):
            raise Conflict("生效版本已改变，请刷新后重新选择。")
        return state["active"]
    if selection == "draft":
        draft, etag = tier.prompts.storage.read("draft.json")
        if draft is None or etag != body.get("draft_etag"):
            raise Conflict("草稿已改变，请先保存并刷新。")
        return {"release_id": "draft", "values": draft["values"]}
    # Only publish history and the current bundled defaults are selectable.
    state = tier.prompts.state()
    allowed = {row["release_id"] for row in state["history"]}
    allowed.add(state["bundled"]["release_id"])
    if selection not in allowed:
        raise ValueError("请选择发布历史中的版本或内置默认版本。")
    return tier.prompts.resolve(selection)


def start(tier, body, actor):
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ValueError("缺少有效的请求编号。")
    run_id = hashlib.sha256((actor + "\0" + request_id).encode()).hexdigest()[:32]
    fingerprint = hashlib.sha256(encode(body)).hexdigest()
    store = tier.prompts.storage
    existing, _ = store.read(f"{ROOT}{run_id}.json")
    if existing:
        if existing["fingerprint"] != fingerprint:
            raise Conflict("相同请求编号对应不同设置，请重新发起。")
        return {"run_id": run_id, "reused": True}
    if not tier.runtime.configured:
        raise ValueError("尚未配置模型运行服务。")
    base = version(tier, body.get("baseline", "active"), body)
    candidate = version(tier, body.get("candidate", "draft"), body)
    if base["values"] == candidate["values"]:
        raise ValueError("两侧提示词完全相同，请选择不同版本或修改草稿。")
    catalogue = unary(tier, "list_scenarios").get("scenarios", {})
    scenarios = [item for category in catalogue.get("categories", [])
                 for item in category.get("scenarios", [])]
    scenario = next((item for item in scenarios if item.get("id") == body.get("scenario_id")), None)
    models = unary(tier, "list_models").get("models", [])
    if scenario is None:
        raise ValueError("请选择有效的目录场景。")
    if not any(item.get("id") == body.get("model_id") for item in models):
        raise ValueError("请选择当前可用的模型。")
    lease, etag = store.read("scenario-lease.json")
    if lease and lease.get("expires", 0) > time.time():
        # A concurrent identical request may have completed setup since the first read.
        existing, _ = store.read(f"{ROOT}{run_id}.json")
        if existing and existing["fingerprint"] == fingerprint:
            return {"run_id": run_id, "reused": True}
        raise Conflict("已有完整场景对比正在执行或启动，请从历史记录查看进度。")
    lease_etag = store.write("scenario-lease.json",
                            {"run_id": run_id, "expires": time.time() + WALL}, etag)
    try:
        # Recheck after acquiring the durable lease, including a completed duplicate.
        existing, _ = store.read(f"{ROOT}{run_id}.json")
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise Conflict("相同请求编号对应不同设置。")
            store.write("scenario-lease.json", {"expires": 0}, lease_etag)
            return {"run_id": run_id, "reused": True}
        sources = dict(baseline=base, candidate=candidate)
        snapshots = {side: tier.prompts.snapshot(bundle["values"], actor, scenario_trial=True)
                     for side, bundle in sources.items()}
        now = time.time()
        record = dict(run_id=run_id, fingerprint=fingerprint, actor=actor, created_at=now,
                      scenario={"id": scenario["id"], "title": scenario["title_zh"]},
                      model_id=body["model_id"], versions={
                          side: {"source_release": sources[side]["release_id"],
                                 "release_id": snapshots[side]["release_id"],
                                 "digest": snapshots[side]["digest"]}
                          for side in SIDES})
        from prompt_config.catalogue import entries
        record["prompt_changes"] = [
            {"id": item["id"], "name": item["name"],
             "baseline": base["values"][item["id"]], "candidate": candidate["values"][item["id"]]}
            for item in entries() if base["values"][item["id"]] != candidate["values"][item["id"]]]
        for side in SIDES:
            store.write(side_key(run_id, side), {
                "status": "queued", "created_at": now, "deadline_at": now + WALL,
                "events": [], "counts": {}, "stage": "queued", "elapsed_seconds": 0})
        store.write(f"{ROOT}{run_id}.json", record)
        for side in SIDES:
            tier.executions.start(f"prompt-scenario:{run_id}:{side}",
                                  lambda publish, side=side: run_side(tier, record, side, lease_etag))
    except Exception:
        # A partial setup must never leave a queued side or hold the lease indefinitely.
        for side in SIDES:
            row, token = store.read(side_key(run_id, side))
            if row and row["status"] == "queued":
                store.write(side_key(run_id, side), dict(row, status="failed",
                            error="对比启动失败，请重新发起。"), token)
        store.write("scenario-lease.json", {"expires": 0}, lease_etag)
        raise
    return {"run_id": run_id, "reused": False}


def retry_side(tier, run_id, side, attempt):
    """Reserve only the failed side; the observed attempt is the idempotency key."""
    if side not in SIDES or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("无效的对比记录或侧别。")
    if type(attempt) is not int or attempt < 0:
        raise ValueError("缺少有效的尝试编号，请刷新记录。")
    store = tier.prompts.storage
    record = detail(tier, run_id)
    if record is None:
        raise ValueError("对比记录不存在。")
    row = record["sides"][side]
    if row["attempt"] > attempt:
        return {"run_id": run_id, "reused": True}
    if row["attempt"] != attempt or not row["retry_action"]:
        raise Conflict("该侧已完成或仍在执行，请刷新查看。")
    lease, token = store.read("scenario-lease.json")
    if lease and lease.get("expires", 0) > time.time():
        raise Conflict("已有场景对比正在执行，请等待完成。")
    lease_token = store.write("scenario-lease.json",
                             {"run_id": run_id, "expires": time.time() + WALL}, token)
    reserved = False
    try:
        old, old_token = store.read(side_key(run_id, side))
        if old is None or old.get("attempt", 0) != attempt:
            raise Conflict("尝试记录已改变，请刷新。")
        archive_key = f"{ROOT}{run_id}/{side}-attempt-{attempt}.json"
        archived, _ = store.read(archive_key)
        if archived is None:
            store.write(archive_key, old)
        next_attempt = attempt + 1
        batch = old.get("batch_id", f"prompt-compare-{run_id}-{side}")
        resume = row["retry_action"] == "resume"
        now = time.time()
        updated = {
            "status": "queued", "stage": "queued", "created_at": now,
            "deadline_at": now + WALL, "attempt": next_attempt,
            "batch_id": batch if resume else f"prompt-compare-{run_id}-{side}-{next_attempt}",
            "events": [], "counts": {}, "elapsed_seconds": 0,
            "previous_attempts": (old.get("previous_attempts") or []) + [{
                "attempt": attempt, "status": row["status"], "error": old.get("error"),
                "elapsed_seconds": old.get("elapsed_seconds"), "action": row["retry_action"],
            }],
        }
        if resume and old.get("material_result"):
            updated["material_result"] = old["material_result"]
        store.write(side_key(run_id, side), updated, old_token)
        reserved = True
        tier.executions.start(f"prompt-scenario:{run_id}:{side}:{next_attempt}",
                              lambda publish: run_side(tier, record, side, lease_token))
    except Exception:
        if reserved:
            current, etag = store.read(side_key(run_id, side))
            if current and current["status"] == "queued":
                store.write(side_key(run_id, side), dict(
                    current, status="failed", error="启动失败，可重新尝试。"), etag)
        store.write("scenario-lease.json", {"expires": 0}, lease_token)
        raise
    return {"run_id": run_id, "reused": False}


def run_side(tier, record, side, lease_etag):
    store = tier.prompts.storage
    run_id = record["run_id"]
    key = side_key(run_id, side)
    row, token = store.read(key)
    started = time.time()
    row.update(status="running", started_at=started, stage="starting")

    def save():
        nonlocal token
        row["elapsed_seconds"] = round(time.time() - started, 1)
        row["updated_at"] = time.time()
        token = store.write(key, row, token)

    stream = None
    try:
        save()
        ref = record["versions"][side]
        group_id = f"prompt-compare-{run_id}-{side}"
        batch_id = row.get("batch_id", group_id)
        row["batch_id"] = batch_id
        content_type, stream, _ = tier.runtime.invoke({
            "action": "generate_sets", "scenarios": [record["scenario"]["id"]], "count": 1,
            "batch_id": batch_id, "group_id": group_id, "model_id": record["model_id"],
            "_prompt_release": {k: ref[k] for k in ("release_id", "digest")},
        }, session_id=new_session_id("prompt-scenario"))
        if "text/event-stream" not in content_type:
            raise ValueError("完整生成未返回事件流。")
        terminal = False
        for text in iter_sse_payloads(stream):
            event = json.loads(text)
            if event is None:
                # Runtime serializes the generator's None keepalive as JSON null.
                # It is transport liveness, never a failed or completed model step.
                save()
                continue
            if not isinstance(event, dict):
                raise ValueError("生成事件格式不正确。")
            provenance = event.get("prompt_release")
            if provenance and (provenance.get("release_id") != ref["release_id"]
                               or provenance.get("digest") != ref["digest"]):
                raise ValueError("生成使用的提示词版本不匹配。")
            kind = event.get("type")
            if kind == "stage":
                row["stage"] = event.get("stage", "")
                counts = row.setdefault("counts", {})
                counts[row["stage"]] = counts.get(row["stage"], 0) + 1
                row["events"] = (row["events"] + [event])[-500:]
            elif kind == "material_completed":
                row["material_result"] = event
            elif kind in ("material_failed", "batch_failed"):
                row["events"] = (row["events"] + [event])[-500:]
                row["error"] = event.get("detail") or event.get("reason") or "生成失败"
                if kind == "batch_failed":
                    row["status"] = "failed"
                    terminal = True
            elif kind == "request_completed":
                row["summary"] = event
                row["status"] = (event.get("status") if event.get("status") in
                                 ("succeeded", "incomplete", "system_failure") else "failed")
                # Success requires a complete visible material and a question package.
                sets = event.get("sets") or []
                has_package = any(isinstance(item.get("questions"), dict)
                                  and item["questions"].get("ok") is True
                                  and isinstance(item["questions"].get("package"), dict)
                                  for item in sets if isinstance(item, dict))
                if row["status"] == "succeeded" and (
                    not row.get("material_result") or not has_package
                ):
                    row["status"] = "interrupted"
                    row["error"] = "流程报告完成，但结果未完整传回；未将其显示为成功。"
                if row["status"] != "succeeded":
                    row["error"] = event.get("detail") or row.get("error") or "未交付完整材料与题目，请查看过程记录。"
                else:
                    row.pop("error", None)
                terminal = True
            save()
            if terminal:
                break
        if not terminal:
            row.update(status="interrupted", error="生成连接结束，但未收到完成记录。")
    except Exception as exc:
        LOG.exception("scenario comparison failed run=%s side=%s", run_id, side)
        row.update(status="failed", error=f"生成或记录结果失败（{type(exc).__name__}），请查看后台日志。")
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                LOG.warning("comparison stream close failed run=%s side=%s", run_id, side)
        row["finished_at"] = time.time()
        try:
            save()
        finally:
            # The last terminal side releases the shared lease. CAS handles racing finishers.
            other, _ = store.read(side_key(run_id, "candidate" if side == "baseline" else "baseline"))
            if other and projected(other)["status"] not in ("queued", "running"):
                try:
                    store.write("scenario-lease.json", {"expires": 0}, lease_etag)
                except Conflict:
                    pass


def install(app, tier, user_scope_key):
    def denied(request):
        if not (request.scope.get(user_scope_key) or {}).get("is_admin"):
            return JSONResponse({"error": {"message": "仅管理员可运行场景对比。"}}, 403)

    @app.post("/api/admin/prompt-scenarios")
    async def create(request: Request):
        if response := denied(request):
            return response
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求必须为对象。")
            return await asyncio.to_thread(start, tier, body, request.scope[user_scope_key]["email"])
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": {"message": str(exc)}},
                                409 if isinstance(exc, Conflict) else 400)

    @app.post("/api/admin/prompt-scenarios/{run_id}/{side}/retry")
    async def retry(run_id: str, side: str, request: Request):
        if response := denied(request):
            return response
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求必须为对象。")
            return await asyncio.to_thread(retry_side, tier, run_id, side, body.get("attempt"))
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": {"message": str(exc)}},
                                409 if isinstance(exc, Conflict) else 400)

    @app.get("/api/admin/prompt-scenarios")
    async def listing(request: Request, offset: int = 0):
        if response := denied(request):
            return response
        if offset < 0:
            return JSONResponse({"error": {"message": "无效页码。"}}, 400)
        return await asyncio.to_thread(history, tier, offset)

    @app.get("/api/admin/prompt-scenarios/{run_id}")
    async def get_run(run_id: str, request: Request):
        if response := denied(request):
            return response
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            return JSONResponse({"error": {"message": "对比记录不存在。"}}, 404)
        record = await asyncio.to_thread(detail, tier, run_id)
        if record is None:
            return JSONResponse({"error": {"message": "对比记录不存在。"}}, 404)
        return record
