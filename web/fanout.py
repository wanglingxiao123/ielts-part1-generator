"""One Runtime invocation per material, merged into the single SSE stream the frontend reads.

## Why this module exists

Every material of a batch used to travel inside ONE ``invoke_agent_runtime`` call. That made the
platform's 15-minute synchronous limit a product limit: six materials at ~150-230s each barely fit,
so ``config/scenarios.yaml`` grew a ``max_batch: 6`` and the UI refused a seventh set. The product
owner's objection is correct and structural -- generating a material has no cross-material
dependency, so nothing required them to share a request:

    每套材料是一个独立请求（独立的 AgentCore invoke），不共享 session；并行执行，并发上限自己
    控制；前端通过 SSE 逐套接收；去掉「单批上限」的概念。

So the fan-out moves here. N children, N ``runtimeSessionId``s (a new id lands on a fresh microVM,
which is what makes them genuinely parallel rather than queued behind one warm instance), each
carrying exactly one material. The 900s wall now bounds ~200s of work instead of ~1400s.

## What the merge has to reconcile

Each child is a complete batch as far as the backend is concerned: it emits its own
``batch_started``, its own ``slot-1``, and its own ``batch_completed``. Relayed naively the browser
would see N batches starting, N materials all claiming ``slot-1``, and a batch that finishes N
times. Three rules fix that, and all three are load-bearing:

**One ``batch_started``, emitted by the web tier before any child answers.** The frontend needs the
total up front to lay out skeleton cards, and it must not wait for the first invoke's headers.
Children's own ``batch_started`` frames are swallowed.

**The web tier owns the slot id space.** Child-local ``slot-k`` is rewritten to the batch-wide
``slot-<n>`` this fan-out allotted it. Namespacing (``child-3/slot-1``) was the obvious alternative
and it is wrong: ``frontend/src/api/agentcore.ts`` pre-plans ``slot-1..slot-N`` in request order at
``createBatch`` time and matches wire events to those planned slots by exact id, so a namespaced id
would create a *second* card and leave the skeleton spinning forever. Rewriting to the planned ids
is what lets the frontend contract stay untouched -- and because the plan order here is the same
order ``backend/request.py`` used to expand (scenarios in order, then the custom one), the
per-scenario index the frontend derives is still right.

**One ``batch_completed``, when the last child finishes**, aggregating every child's counts. A
child that fails to invoke at all, or dies mid-stream, or answers ``batch_failed``, contributes
``material_failed`` for its own slot and nothing else: one child's failure must not end the batch
for the other N-1.

## Ordering, back-pressure and the threadpool

Each child's ``iter_lines`` read is blocking (botocore ``StreamingBody``), so each child runs on a
thread and pushes into an ``asyncio.Queue`` through ``call_soon_threadsafe``. The merged stream is
an *async* generator over that queue, which is a deliberate change from the old single-payload
relay: that one had to be a sync generator so ``iterate_in_threadpool`` would keep its blocking
read off the event loop, and here the blocking reads are already on their own threads. The
guarantee is the same and is now structural -- nothing blocking runs on the loop at all.

The threads come from a dedicated executor sized to the concurrency cap, NOT from anyio's default
threadpool. That pool has 40 tokens and is shared with every sync route handler in `web/app.py`
(``/healthz`` included, until it was made async). Six children holding tokens for four minutes
each, times a handful of concurrent users, is how a health check starts timing out -- and an
instance whose health check times out gets killed, taking every in-flight batch with it. A separate
executor makes that arithmetic impossible rather than merely unlikely.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Dict, List, Tuple

from .runtime_client import SSE_CONTENT_TYPE, iter_sse_payloads, new_session_id, read_json

__all__ = [
    "FANOUT_CONCURRENCY",
    "PER_MATERIAL_WALL_SECONDS",
    "ChildPlan",
    "FanOut",
    "plan_children",
]

# How many materials the web tier will have in flight at once.
#
# Not a product limit -- the user may ask for any number of sets -- but a throughput one: the model
# channel's TPM/RPM is undocumented, and 6 is the value the backend already ran its own slots at
# without seeing a 429. Raise it only with evidence; on 429s lower it, because a retry storm costs
# more wall time than a shorter queue does.
#
# The cost of holding a slot open is one blocked thread, not CPU: the web tier is 0.5 vCPU / 1 GB
# and every one of these threads is parked on a socket read.
FANOUT_CONCURRENCY = max(1, int(os.environ.get("WEB_FANOUT_CONCURRENCY", "6")))

# The platform's synchronous wall on ONE invocation, which now carries ONE material. Used only to
# report an honest `deadline_at`; the backend enforces its own budget inside each child.
PER_MATERIAL_WALL_SECONDS = float(os.environ.get("WEB_PER_MATERIAL_WALL", "900"))

# Marks a child's end on the merge queue. A unique object, so it can never collide with an event.
_CHILD_DONE = object()


class ChildPlan(object):
    """One child invocation: the payload to send and the batch-wide slot ids it may use.

    ``slot_ids`` is a list rather than a single id because a child is *allowed* to produce more
    than one material -- see `plan_children`. In the normal case it has exactly one entry.
    """

    __slots__ = ("index", "payload", "slot_ids", "scenario")

    def __init__(self, index: int, payload: Dict[str, Any], slot_ids: List[str],
                 scenario: str) -> None:
        self.index = index
        self.payload = payload
        self.slot_ids = slot_ids
        self.scenario = scenario


def plan_children(
    payload: Dict[str, Any], *, batch_id: str, default_count: int = 1
) -> Tuple[List[ChildPlan], List[str]]:
    """Expand one `generate` payload into one child per material. Returns (children, slot_ids).

    Mirrors `backend/request.py`'s ``_expand`` ordering exactly -- each requested scenario in turn,
    expanded by its count, then the custom scenario -- because that order is also the order the
    frontend pre-planned its skeleton cards in, and the two have to agree on which card is
    ``slot-3``.

    A scenario with no explicit count gets ``default_count``, which is 1 and NOT the catalogue's
    ``default_count: 2``. The difference matters only for a hand-written request -- the frontend
    always sends explicit counts -- and 1 is the safe direction: the web tier would have to fetch the
    catalogue to learn the real default, and a synchronous `list_scenarios` before every batch would
    add a round trip to the one path that must answer immediately. Under-generating is a request the
    user can repeat; over-generating spends their money on materials they did not ask for.

    Anything this function cannot expand -- an unknown scenario id, a bad count, a rejected custom
    scenario -- is left alone and sent to a child verbatim, so the backend's own validation
    produces the error message. Duplicating that validation here would give the user two different
    sentences for the same mistake, and the web tier has no catalogue to check ids against.
    """
    counts = payload.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    fallback = payload.get("count")

    def count_for(scenario_id: str) -> int:
        raw = counts.get(scenario_id, fallback if fallback is not None else default_count)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            # Not the web tier's error to report: pass one child through and let the backend say
            # "count for %r must be an integer" in its own words.
            return 1
        return max(1, value)

    requested = payload.get("scenarios")
    requested = requested if isinstance(requested, list) else []

    # Everything except the batch shape is forwarded untouched: `hard_limit_seconds`,
    # `concurrency`, and any field added later all belong to the child.
    base = {k: v for k, v in payload.items()
            if k not in ("scenarios", "counts", "count", "custom_scenario")}

    children: List[ChildPlan] = []
    slot_ids: List[str] = []

    def add(child_payload: Dict[str, Any], scenario: str, materials: int) -> None:
        allotted = [
            "slot-%d" % (len(slot_ids) + offset + 1) for offset in range(materials)
        ]
        slot_ids.extend(allotted)
        child_payload["batch_id"] = batch_id
        children.append(ChildPlan(len(children), child_payload, allotted, scenario))

    for entry in requested:
        if not isinstance(entry, str):
            # Same reasoning as a bad count: forward it and let the backend reject it by name.
            add(dict(base, scenarios=[entry]), str(entry), 1)
            continue
        for _ in range(count_for(entry)):
            add(dict(base, scenarios=[entry], counts={entry: 1}, count=1), entry, 1)

    custom = payload.get("custom_scenario")
    if custom:
        if isinstance(custom, dict):
            try:
                repeat = max(1, int(custom.get("count") or 1))
            except (TypeError, ValueError):
                repeat = 1
            one = dict(custom, count=1)
        else:
            repeat, one = 1, custom
        for _ in range(repeat):
            add(dict(base, scenarios=[], custom_scenario=one), "custom", 1)

    return children, slot_ids


class _Merge(object):
    """Aggregate state for one fanned-out batch. Mutated only from the event loop.

    **The counts are derived from the terminal events actually relayed, not from the children's own
    summaries.** That is the whole design of this class and it is not the obvious choice, so:

    Summing the children's `batch_completed` counts is one lost frame away from lying. A child whose
    connection closes after `material_completed` but before `batch_completed` has delivered a
    material the browser is rendering, while contributing 0 to the sum -- so `batch_completed` would
    say "0 succeeded" over a grid of cards, and the frontend would render `status: 'partial'` with
    `completed: 0`. Counting what was relayed makes the summary agree with the cards by
    construction: every slot that produced a terminal event is counted exactly once, and a slot that
    produced none is counted as a failure at the end, because the browser is otherwise left with a
    skeleton that never resolves.

    The children's summaries are still absorbed, for the fields that genuinely only they know:
    `degraded`, `refilled`, `stage_timings`, and the per-slot detail rows.
    """

    __slots__ = ("degraded", "refilled", "rows", "timings", "config", "outcomes")

    def __init__(self) -> None:
        self.degraded = 0
        self.refilled = 0
        # slot_id -> the child's own detail row for it, when one arrived.
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.timings: Dict[str, Dict[str, Any]] = {}
        self.config: Dict[str, Any] = {}
        # slot_id -> "ok" | "failed" | "skipped". One entry per slot that reached a terminal state;
        # a dict rather than counters so a duplicate terminal event for one slot cannot double-count.
        self.outcomes: Dict[str, str] = {}

    def record(self, slot_id: str, outcome: str) -> None:
        self.outcomes.setdefault(slot_id, outcome)

    def summarise(self, slot_ids: List[str], scenarios: Dict[str, str]) -> Dict[str, Any]:
        """The summary counts and per-slot rows, over every slot the batch PLANNED.

        Driven by the plan rather than by what arrived, so the summary has exactly one row per card
        the browser drew. A slot with no terminal event is `failed`: its child produced neither a
        material nor a stated failure, and reporting a clean batch over a card still spinning is the
        one outcome worse than saying that slot failed.
        """
        for slot_id in slot_ids:
            self.outcomes.setdefault(slot_id, "failed")
        outcomes = [self.outcomes[slot_id] for slot_id in slot_ids]
        return {
            "succeeded": outcomes.count("ok"),
            "failed": outcomes.count("failed"),
            "skipped": outcomes.count("skipped"),
            "slots": [self._row(slot_id, scenarios) for slot_id in slot_ids],
        }

    def _row(self, slot_id: str, scenarios: Dict[str, str]) -> Dict[str, Any]:
        """One summary row: the child's detail where it exists, the plan's knowledge where it does not.

        The child's row wins on every field it carries -- it knows the route, the timings and the real
        failure reason -- while `slot_id`, `scenario` and `ok` are overwritten from what was actually
        relayed, because those three are what the frontend joins on and the merge is authoritative
        about them.
        """
        outcome = self.outcomes[slot_id]
        row = dict(self.rows.get(slot_id) or {})
        row["slot_id"] = slot_id
        row["scenario"] = row.get("scenario") or scenarios.get(slot_id, "")
        row["ok"] = outcome == "ok"
        # `reason` is the child's own phrasing when it gave one; the outcome word is the fallback for
        # a slot that never spoke, where "failed" is all anyone can honestly say.
        row["reason"] = row.get("reason") or (None if outcome == "ok" else outcome)
        return row

    def absorb_completed(self, event: Dict[str, Any], rename) -> None:
        """Fold one child's `batch_completed` into the aggregate.

        Only the fields the child alone knows. `succeeded` / `failed` / `skipped` are deliberately
        NOT read from here -- see the class docstring.
        """
        for field in ("degraded", "refilled"):
            try:
                setattr(self, field, getattr(self, field) + int(event.get(field) or 0))
            except (TypeError, ValueError):
                pass
        for row in event.get("slots") or []:
            if isinstance(row, dict) and row.get("slot_id"):
                self.rows[rename(str(row["slot_id"]))] = dict(row)
        self._merge_timings(event.get("stage_timings") or {})

    def _merge_timings(self, incoming: Dict[str, Any]) -> None:
        """Combine per-stage aggregates without needing the raw samples.

        count/min/max compose exactly; the mean is recombined as a count-weighted average, which is
        the true mean of the union rather than a mean of means. Getting that wrong would quietly
        skew the one number `docs/timing.md` calibrates the estimate against.
        """
        for name, value in (incoming or {}).items():
            if not isinstance(value, dict):
                continue
            try:
                count = int(value.get("count") or 0)
                low = float(value.get("min"))
                high = float(value.get("max"))
                mean = float(value.get("mean"))
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            current = self.timings.get(name)
            if current is None:
                self.timings[name] = {"count": count, "min": low, "max": high, "mean": mean}
                continue
            total = current["count"] + count
            current["mean"] = round(
                (current["mean"] * current["count"] + mean * count) / total, 2
            )
            current["count"] = total
            current["min"] = min(current["min"], low)
            current["max"] = max(current["max"], high)


class FanOut(object):
    """Runs one batch as N independent invocations and yields one merged event stream.

    Construct per request. The executor is shared across requests (it is the concurrency cap) and
    is owned by the caller, so a batch that finishes does not tear down another batch's threads.
    """

    def __init__(self, runtime: Any, children: List[ChildPlan], slot_ids: List[str], *,
                 executor: ThreadPoolExecutor, concurrency: int = FANOUT_CONCURRENCY,
                 batch_id: str = "") -> None:
        self.runtime = runtime
        self.children = children
        self.slot_ids = slot_ids
        # The id `web/app.py` minted for this batch, and the id `web/batch_history.py` keys the
        # record on. Carried here for one reason: it has to reach the browser in `batch_started`.
        # See the `events()` docstring.
        self.batch_id = batch_id
        self.executor = executor
        self.concurrency = max(1, min(concurrency, len(children) or 1))
        self._bodies: Dict[int, Any] = {}
        self._bodies_lock = threading.Lock()
        self._stopped = threading.Event()

    # ── slot identity ────────────────────────────────────────────────────────

    def _rename(self, child: ChildPlan, extra: Dict[str, str], slot_id: str) -> str:
        """Child-local slot id -> the batch-wide id this fan-out allotted it.

        A child that emits more slot ids than it was allotted gets fresh ids appended rather than
        colliding with another child's. That cannot happen while every child carries one material,
        and silently mapping two materials onto one card is a worse failure than an extra card.
        """
        mapped = extra.get(slot_id)
        if mapped is not None:
            return mapped
        used = len(extra)
        if used < len(child.slot_ids):
            mapped = child.slot_ids[used]
        else:
            mapped = "slot-%d-%d" % (child.index + 1, used + 1)
        extra[slot_id] = mapped
        return mapped

    # ── the child worker (runs on a thread) ──────────────────────────────────

    def _pump(self, child: ChildPlan, push) -> None:
        """Invoke one child and push its raw events onto the merge queue. Never raises.

        Runs on the dedicated executor. Everything here is blocking on purpose: the boto3 call, and
        then ``iter_lines`` for as long as the material takes.
        """
        try:
            if self._stopped.is_set():
                push({"type": "__child_aborted__"})
                return
            # A fresh session id per child, and one that names the card it belongs to.
            #
            # Fresh is the load-bearing half: a reused id routes to the one warm microVM and
            # serialises the batch this module exists to parallelise. The slot prefix is the cheap
            # half -- `runtimeSessionId` is what AgentCore stamps on its log streams, so it is the
            # only field that lets an operator get from "the third card failed" to that child's
            # logs. `new_session_id` pads past the API's 33-character floor either way.
            content_type, body, _ = self.runtime.invoke(
                child.payload,
                session_id=new_session_id("ielts-%s" % child.slot_ids[0]),
            )
            with self._bodies_lock:
                self._bodies[child.index] = body
            try:
                if SSE_CONTENT_TYPE not in content_type:
                    # A unary answer to `generate` means the backend rejected the payload before it
                    # became a generator (app.py returns a dict for an unknown action, and an
                    # error body is JSON). Surface it rather than dropping it on the floor.
                    push({"type": "__child_error__",
                          "detail": json.dumps(read_json(body))[:400]})
                    return
                for text in iter_sse_payloads(body):
                    if self._stopped.is_set():
                        return
                    try:
                        event = json.loads(text)
                    except ValueError:
                        # A non-JSON line on an event-stream response: the Runtime printed
                        # something. Report it as this child's failure instead of discarding it.
                        push({"type": "__child_error__", "detail": text[:400]})
                        continue
                    if isinstance(event, dict):
                        push(event)
            finally:
                closer = getattr(body, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:  # noqa: BLE001 - close failures are not the batch's problem
                        pass
        except Exception as exc:  # noqa: BLE001 - isolation is the whole point of this method
            push({"type": "__child_error__",
                  "detail": "%s: %s" % (type(exc).__name__, str(exc)[:300])})
        finally:
            push(_CHILD_DONE)

    def close(self) -> None:
        """Stop the children. Called when the browser goes away, or on the way out.

        Closing the ``StreamingBody`` is what unblocks a thread parked in ``iter_lines``; the flag
        alone would only be noticed after the next line arrived, which for an abandoned batch could
        be minutes. The threads are not joined -- the response is already over, and waiting for a
        socket teardown would hold the loop for no benefit.
        """
        self._stopped.set()
        with self._bodies_lock:
            bodies = list(self._bodies.values())
            self._bodies.clear()
        for body in bodies:
            closer = getattr(body, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass

    # ── the merged stream ────────────────────────────────────────────────────

    async def events(self) -> AsyncIterator[Dict[str, Any]]:
        """Yield one coherent batch's worth of events: one start, the middles, one completion.

        ``batch_started`` carries ``batch_id``, and that field is not decoration. The web tier mints
        the id (`new_batch_id`), keys the S3 record on it (`web/batch_history.py`) and plans the
        children with it -- but until this field existed it never told the browser, so
        `frontend/src/api/agentcore.ts` minted its own `batch-<ms36>-<n>` and put THAT in the URL.
        The two id spaces never intersected, so after a reload the history panel asked
        `/api/batch-history/batch-ms713fnc-1` about a batch recorded as `web-1785386619156-1` and got
        「没有找到批次 ... 的历史记录」 for a batch sitting in S3. Same class of bug as the
        `placeholderId` one: the frontend inventing an identifier the backend never issued.
        """
        total = len(self.slot_ids)
        waves = (total + self.concurrency - 1) // self.concurrency if total else 0
        yield {
            "type": "batch_started",
            # The authoritative id. Emitted first among the fields because everything downstream --
            # the URL, the history lookup, the candidate group keys -- has to agree with it.
            "batch_id": self.batch_id,
            "total": total,
            # An upper bound the web tier can actually stand behind: each child gets its own 900s
            # wall, and at most `waves` of them run in series. The old value was one shared wall
            # for the whole batch, which is the constraint this change removed.
            "deadline_at": round(time.time() + PER_MATERIAL_WALL_SECONDS * max(waves, 1), 3),
            "config": {
                "fanout": "per_material_invoke",
                "children": len(self.children),
                "web_concurrency": self.concurrency,
                "per_material_wall_seconds": PER_MATERIAL_WALL_SECONDS,
            },
            "at": time.time(),
        }
        if total == 0:
            yield {"type": "batch_failed", "reason": "bad_request",
                   "detail": "no scenarios requested"}
            return

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        gate = threading.Semaphore(self.concurrency)
        merge = _Merge()
        renames: Dict[int, Dict[str, str]] = {}
        remaining = len(self.children)
        # Known from the plan, so the summary can name a scenario even for a slot whose child said
        # nothing at all -- the frontend groups its cards by scenario, and a blank one would be
        # ungroupable.
        scenario_of = {
            slot_id: child.scenario for child in self.children for slot_id in child.slot_ids
        }

        def make_push(child: ChildPlan):
            def push(item: Any) -> None:
                # The only thread->loop handoff in this module. `call_soon_threadsafe` rather than
                # `run_coroutine_threadsafe` because `put_nowait` on an unbounded queue cannot
                # block, so there is nothing to await and no way for a child to deadlock on a
                # browser that stopped reading.
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, (child, item))
                except RuntimeError:
                    # Loop already closed: the request is over and nobody is listening.
                    self._stopped.set()
            return push

        def run(child: ChildPlan) -> None:
            # The gate is held for the child's whole lifetime, which is what caps concurrency:
            # a slot is a material in flight, not a queued task.
            with gate:
                self._pump(child, make_push(child))

        futures = [self.executor.submit(run, child) for child in self.children]

        try:
            while remaining > 0:
                child, item = await queue.get()
                if item is _CHILD_DONE:
                    remaining -= 1
                    continue
                extra = renames.setdefault(child.index, {})

                def rename(slot_id: str, _child=child, _extra=extra) -> str:
                    return self._rename(_child, _extra, slot_id)

                for event in self._translate(child, item, merge, rename):
                    yield event

            yield dict(
                merge.summarise(self.slot_ids, scenario_of),
                type="batch_completed",
                degraded=merge.degraded,
                refilled=merge.refilled,
                stage_timings=merge.timings,
                at=time.time(),
            )
        finally:
            # Reached on normal completion, on a client disconnect (GeneratorExit) and on an
            # unexpected error. Children must not outlive the response either way: an orphaned
            # invocation keeps paying the model for output nobody will read.
            self.close()
            for future in futures:
                future.cancel()

    def _translate(self, child: ChildPlan, event: Dict[str, Any], merge: _Merge,
                   rename) -> List[Dict[str, Any]]:
        """One child event -> zero or more merged events. Where the reconciliation happens."""
        kind = str(event.get("type") or "")

        if kind == "batch_started":
            # Swallowed: the merged stream already announced itself, and a second `batch_started`
            # would reset the frontend's total to this child's 1. The child's `config` names the
            # model and region, which is worth keeping once for an operator.
            if not merge.config and isinstance(event.get("config"), dict):
                merge.config = dict(event["config"])
            return []

        if kind == "batch_completed":
            merge.absorb_completed(event, rename)
            return []

        if kind == "__child_aborted__":
            # The batch was abandoned before this child started. No card, no count: the response
            # is already gone.
            return []

        if kind in ("__child_error__", "batch_failed"):
            # The child never produced a usable stream (invoke refused, connection died, backend
            # rejected the payload). Report it per slot so the other children carry on, and record
            # it as failed so `batch_completed` cannot present a partial batch as a success.
            #
            # Skipped if the slot already reached a terminal state: a child that delivered its
            # material and *then* had its connection die must not have that material replaced by a
            # failure card. `record` is setdefault-based, but the frame would still be relayed.
            reason = str(event.get("reason") or "runtime_invoke_failed")
            detail = event.get("detail")
            out: List[Dict[str, Any]] = []
            for slot_id in child.slot_ids:
                if slot_id in merge.outcomes:
                    continue
                merge.record(slot_id, "failed")
                merge.rows[slot_id] = {"slot_id": slot_id, "scenario": child.scenario,
                                       "ok": False, "reason": reason}
                out.append({"type": "material_failed", "slot_id": slot_id,
                            "scenario": child.scenario, "ok": False, "reason": reason,
                            "detail": detail, "at": time.time()})
            return out

        if event.get("slot_id"):
            event = dict(event)
            slot_id = rename(str(event["slot_id"]))
            event["slot_id"] = slot_id
            # The summary counts come from here, not from the children's own summaries -- see
            # `_Merge`. `material_failed` with `skipped: true` is the time-budget case, which the
            # frontend distinguishes and the summary must not report as an outright failure.
            if kind == "material_completed":
                merge.record(slot_id, "ok")
            elif kind == "material_failed":
                merge.record(slot_id, "skipped" if event.get("skipped") else "failed")
        return [event]


def build_executor(concurrency: int = FANOUT_CONCURRENCY) -> ThreadPoolExecutor:
    """The dedicated pool the children run on.

    Sized to the concurrency cap and no larger: the semaphore already refuses to start a seventh
    material, so extra workers could only sit idle. Kept out of anyio's default threadpool for the
    reason in the module docstring -- a child parked for four minutes must not be able to consume a
    token that `/healthz` or a static file needs.
    """
    return ThreadPoolExecutor(
        max_workers=max(1, concurrency), thread_name_prefix="ielts-fanout"
    )
