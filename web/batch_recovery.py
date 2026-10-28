"""User-triggered recovery of missing seats, backed by checkpoint IDs and a CAS lease."""
from __future__ import annotations

import hashlib
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor

from prompt_config.store import Conflict, encode
from .batch_history import derive, canonical_scenario_key


def retryable_question_fault(row):
    # Mirrors Runtime's transition allowlist; parity is tested against SlotRecord.
    failure = row.get("last_failure") or {}
    return bool(row.get("state") == "exhausted" and row.get("system_fault")
                and not row.get("replaced_by") and row.get("material_id")
                and failure.get("reason") == "question_stage_crashed"
                and str(failure.get("detail") or "").startswith("ModelCallError:"))


class RecoveryBusy(ValueError):
    pass


class BatchRecovery:
    def __init__(self, tier):
        self.tier = tier

    def _record(self, batch_id, owner):
        if not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", batch_id):
            raise ValueError("无效的批次编号。")
        record = self.tier.history.store.load_index(batch_id)
        if record is None:
            raise KeyError(batch_id)
        if record.get("owner") and record["owner"] != owner:
            raise PermissionError(batch_id)
        return record

    def _lease_key(self, batch_id):
        return "batch-recovery/" + hashlib.sha256(batch_id.encode()).hexdigest() + ".json"

    def plan(self, batch_id, owner):
        record = self._record(batch_id, owner)
        view = derive(record)
        result = {"batch_id": batch_id, "delivered": view["arrived"],
                  "requested": view["requested_total"], "targets": [], "status": "available"}
        if view["read_only"]:
            return dict(result, status="readonly")
        present = {(canonical_scenario_key(row["scenario_key"]), row["index"]) for row in view["materials"]}
        missing, number = [], 0
        mappings = record.get("request_ids_by_slot") or {}
        refill = record.get("refill_execution") or {}
        legacy_targets = {row["slot_id"] for row in refill.get("targets", [])}
        for scenario in record.get("scenarios") or []:
            for index in range(int(scenario["count"])):
                number += 1
                slot = "slot-%d" % number
                if (scenario["scenario_key"], index) in present:
                    continue
                request_id = mappings.get(slot) or (
                    "%s-%s" % (refill["execution_id"], slot) if slot in legacy_targets
                    else "%s-%s" % (batch_id, slot))
                if not re.fullmatch(re.escape(batch_id) + r"-[A-Za-z0-9_-]+", request_id):
                    raise ValueError("批次断点关联异常，请联系维护人员核对。")
                missing.append(dict(scenario_key=scenario["scenario_key"], index=index,
                                    slot_id=slot, request_id=request_id))
        if not missing:
            return dict(result, status="complete")
        lease, _ = self.tier.prompts.storage.read(self._lease_key(batch_id))
        manager = self.tier.executions
        if (manager.is_running("batch:" + batch_id)
                or manager.is_running("batch-refill:" + str(refill.get("execution_id", "")))
                or (lease and lease.get("expires", 0) > time.time())):
            return dict(result, status="running")
        # A quiet initial fan-out may not have written another index yet. Do not
        # use the shorter UI stale threshold to launch overlapping paid work.
        deadline = record.get("execution_deadline_at") or (
            float(record.get("created_at") or 0)
            + 3900 * max(1, math.ceil(view["requested_total"] / self.tier.fanout_concurrency)))
        if record.get("state") == "running" and time.time() < deadline:
            return dict(result, status="running")

        def inspect(target):
            doc = self.tier.slot_state.load_request(target["request_id"])
            if doc is None:
                return dict(target, action="restart")
            status = doc.get("status")
            rows = [row for row in doc["slots"] if not row.get("replaced_by")]
            if rows and any(retryable_question_fault(row) for row in rows) and all(
                    not row.get("system_fault") or retryable_question_fault(row) for row in rows):
                return dict(target, action="retry", model_id=doc.get("model_id") or record.get("model_id") or "")
            if status == "system_failure" or any(row.get("system_fault") for row in rows):
                return dict(target, action="blocked", reason="该故障不属于可直接重试的模型调用失败，请联系维护人员核对；已交付材料不受影响。")
            if status == "running" and time.time() - float(doc.get("updated_at") or 0) < 3900:
                return dict(target, action="wait")
            if status not in ("running", "incomplete", "succeeded"):
                return dict(target, action="blocked", reason="断点状态无法确认，请联系维护人员。")
            action = "resume" if any(row.get("state") != "exhausted" for row in rows) else "restart"
            return dict(target, action=action, model_id=doc.get("model_id") or record.get("model_id") or "")

        with ThreadPoolExecutor(max_workers=min(8, len(missing))) as pool:
            targets = list(pool.map(inspect, missing))
        return dict(result, targets=targets,
                    status="running" if any(t["action"] == "wait" for t in targets) else "available")

    def start_legacy(self, batch_id, owner, seats):
        record = self._record(batch_id, owner)
        current = record.get("refill_execution") or {}
        lease, _ = self.tier.prompts.storage.read(self._lease_key(batch_id))
        active = (self.tier.executions.is_running("batch-refill:" + str(current.get("execution_id", "")))
                  or (lease and lease.get("expires", 0) > time.time()))
        if current.get("status") == "running" and active:
            for acknowledgement in (record.get("recovery_requests") or {}).values():
                response = acknowledgement["response"]
                if response["execution_id"] == current.get("execution_id"):
                    return dict(response, reused=True)
        import uuid
        return self.start(batch_id, owner, uuid.uuid4().hex, seats)

    def start(self, batch_id, owner, request_id, seats=None):
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("缺少有效请求编号，请刷新重试。")
        record = self._record(batch_id, owner)
        fingerprint = hashlib.sha256(encode({"owner": owner, "seats": seats})).hexdigest()
        previous = (record.get("recovery_requests") or {}).get(request_id)
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise ValueError("同一请求编号对应不同设置。")
            return dict(previous["response"], reused=True)
        plan = self.plan(batch_id, owner)
        if plan["status"] == "running":
            raise RecoveryBusy("这一批仍在运行或启动，请刷新进度；无需重复提交。")
        if plan["status"] != "available":
            raise ValueError("这一批已补齐或不可修改，请刷新查看。")
        targets = [t for t in plan["targets"] if t["action"] in ("resume", "restart", "retry")]
        if seats is not None:
            requested = {(row.get("scenario_key"), row.get("index")) for row in seats}
            valid = {(t["scenario_key"], t["index"]) for t in plan["targets"]}
            if not requested or not requested <= valid:
                raise ValueError("请选择属于这一批的未完成版位。")
            targets = [t for t in targets if (t["scenario_key"], t["index"]) in requested]
        if not targets:
            raise ValueError("当前没有可恢复的版位，请查看故障提示。")
        storage = self.tier.prompts.storage
        lease_key = self._lease_key(batch_id)
        lease, token = storage.read(lease_key)
        if lease and lease.get("expires", 0) > time.time():
            raise RecoveryBusy("恢复任务正在启动，请稍后确认同一请求。")
        wall = 3900 * max(1, math.ceil(len(targets) / self.tier.fanout_concurrency))
        try:
            token = storage.write(lease_key, {"request_id": request_id, "expires": time.time() + wall}, token)
        except Conflict:
            raise RecoveryBusy("恢复任务正在启动，请稍后确认同一请求。")

        def release():
            try:
                storage.write(lease_key, {"expires": 0}, token)
            except Conflict:
                pass

        execution_id = ""
        try:
            # Recheck under the cross-process reservation lease before touching history.
            record = self._record(batch_id, owner)
            previous = (record.get("recovery_requests") or {}).get(request_id)
            if previous:
                release()
                return dict(previous["response"], reused=True)
            if derive(record)["read_only"]:
                raise ValueError("这一批已提交或已过保留期，不能恢复。")
            present = {(canonical_scenario_key(r["scenario_key"]), r["index"]) for r in derive(record)["materials"]}
            if any((t["scenario_key"], t["index"]) in present for t in targets):
                raise ValueError("版位已更新，请刷新后重试。")
            current_refill = record.get("refill_execution") or {}
            if (self.tier.executions.is_running("batch:" + batch_id)
                    or self.tier.executions.is_running("batch-refill:" + str(current_refill.get("execution_id", "")))):
                raise RecoveryBusy("这一批仍在运行，请刷新进度。")
            # Stale/failed reservations may be retried only after the durable lease
            # and runtime checks above establish that their producers have stopped.
            with self.tier.history._lock:
                latest = self.tier.history.store.load_index(batch_id)
                if (latest.get("refill_execution") or {}).get("status") == "running":
                    latest["refill_execution"]["status"] = "interrupted"
                    self.tier.history.store.save_index(batch_id, latest)
            reservation = self.tier.history.reserve_refill(batch_id, targets, owner=owner)
            execution_id = reservation["execution_id"]
            prepared = [dict(t, request_id=t["request_id"] if t["action"] in ("resume", "retry")
                             else "%s-%s" % (execution_id, t["slot_id"])) for t in targets]
            for target in prepared:
                if target["action"] == "retry":
                    target["retry_token"] = request_id
            with self.tier.history._lock:
                latest = self.tier.history.store.load_index(batch_id)
                latest.setdefault("request_ids_by_slot", {}).update(
                    {t["slot_id"]: t["request_id"] for t in prepared})
                latest["execution_deadline_at"] = time.time() + wall
                self.tier.history.store.save_index(batch_id, latest)
            response = {"batch_id": batch_id, "execution_id": execution_id,
                        "status": "running", "reused": False}
            # Persist acknowledgement before launching paid work. A lost HTTP response
            # must be replayable even when the producer has already finished.
            with self.tier.history._lock:
                latest = self.tier.history.store.load_index(batch_id)
                latest.setdefault("recovery_requests", {})[request_id] = {
                    "fingerprint": fingerprint, "response": response}
                self.tier.history.store.save_index(batch_id, latest)
            self.tier._start_batch_refill(
                batch_id, execution_id, prepared, custom_label=reservation.get("custom_label", ""),
                model_id=reservation.get("model_id", ""), on_finished=release)
            return response
        except Exception:
            if execution_id and self.tier.executions.is_running("batch-refill:" + execution_id):
                # Do not free a live producer if recording its acknowledgement fails.
                raise
            try:
                if execution_id:
                    self.tier.history.fail_refill(batch_id, execution_id, "恢复任务未能启动，请重试。")
                    with self.tier.history._lock:
                        latest = self.tier.history.store.load_index(batch_id)
                        acknowledgement = (latest.get("recovery_requests") or {}).get(request_id)
                        if acknowledgement:
                            acknowledgement["response"]["status"] = "failed"
                            self.tier.history.store.save_index(batch_id, latest)
            finally:
                release()
            raise
