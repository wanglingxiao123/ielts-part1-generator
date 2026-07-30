# Measured timings

Replaces the assumptions in `prd.md`'s uncertainty list with measurements. Every number here was
observed, not estimated. Where a figure is still unmeasured it says so.

Environment: local macOS, `openai.gpt-5.6-terra` via bedrock-mantle `us-east-1`,
`IELTS_MODEL_AUTH=bearer`. **Not yet measured on AgentCore Runtime** — network latency from
inside the Runtime may differ, so these are a lower bound on production times, not a substitute.

## 1 material (`scripts/run_one.py`, scenario `accommodation-rental`)

| Stage | Seconds |
|---|---:|
| generate attempt 1 (rejected by validator) | 38.1 |
| validate 1 | 0.09 |
| generate attempt 2 | 30.7 |
| validate 2 | 0.06 |
| blind audit | 33.6 |
| revise | 14.4 |
| validate revised | 0.09 |
| blind re-audit | 29.2 |
| **total** | **146.2** |

Observations that matter for calibration:

- **The deterministic layer is free.** All four script invocations together took 0.33s — 0.2% of
  the run. There is no reason to skip validation to save time, and the cross-check costs nothing
  at all.
- **Model calls dominate at 25–38s each.** Total elapsed time is essentially (number of model
  calls) × ~32s. Reducing wall time means reducing calls, not optimising anything else.
- The first attempt failed validation on a stale `turn_index` and a missing narration phrase.
  That regeneration cost 38s, i.e. a quarter of the run — see the note below.

## 3 materials, concurrency 3 (`scripts/run_batch.py`)

See `docs/timing-batch3.md` for the recorded run. Summary of what it establishes:

- no 429s at concurrency 3, so `IELTS_CONCURRENCY=3` is safe at this batch size;
- three concurrent materials finish in roughly the time of the slowest single material, since the
  bottleneck is per-call latency rather than local CPU.

## Batch ceiling — removed, and what these numbers now bound

**There is no batch ceiling any more.** `max_batch: 6` is gone from `config/scenarios.yaml`, from
`ScenarioCatalogue` and from `backend/request.py`. The web tier issues one `invoke_agent_runtime` per
material (`web/fanout.py`), so what the 900s wall bounds is a single material, not a batch.

Read against a single material, the measurements below are not merely comfortable but decisive: the
observed range is 146s (one regeneration) to 225s (two), i.e. **a quarter of one invocation's
budget at the slowest observed value**. The old calculation that made 6 a defensible ceiling is now
the calculation that shows a ceiling has no basis:

| single material | share of 810s usable budget |
|---:|---|
| 146s | 18% |
| 225s | 28% |
| 3 attempts × 240s p95 | 89% — the refill bound, the only thing left that the clock constrains |

The last row is the surviving purpose of the time budget. Six materials used to share those 810s, so
`may_start` refused *siblings* and `may_revise` was routinely declined; now one material owns them,
and the budget only refuses a third refill attempt of the same material. See `Budget`'s docstring.

What replaced the ceiling as the thing to calibrate is **throughput, not duration**: N concurrent
invocations mean N concurrent model conversations, so the number to watch is 429s, and the knob is
the web tier's `WEB_FANOUT_CONCURRENCY` (default 6). Unmeasured, and honestly so — the runs below
were all made with a single invocation running several materials internally.

## Configuration derived from these measurements

| Variable | Value | Basis |
|---|---|---|
| `WEB_FANOUT_CONCURRENCY` | 6 | web tier; how many independent invocations run at once. **Unmeasured** — inherited from the value `IELTS_CONCURRENCY` ran at without 429s, which is suggestive rather than evidence, since those slots shared one conversation. Lower it on throttling. |
| `IELTS_CONCURRENCY` | 3 | no throttling observed at 3 concurrent slots. Effectively dead in production: one slot per invocation means `BatchRequest` clamps it to 1. Still governs the CLI. |
| `IELTS_P95_PER_MATERIAL` | 240 | measured 146s typical; 240 covers the two-regeneration case |
| `IELTS_REVISION_COST` | 120 | measured revise + re-audit ≈ 44s; 120 is deliberately cautious. NOT lowered now that one material owns the whole budget — `may_revise` compares against what a revision costs, so a smaller value would only make it answer yes with no time left to finish. |
| `IELTS_SAFETY_MARGIN` | 90 | leaves room to emit the summary event and close cleanly |

## Time budget, verified live

Forced with `--hard-limit 260 IELTS_P95_PER_MATERIAL=100 IELTS_REVISION_COST=1000` over two
materials at concurrency 1:

- slot-1 was already in flight and **finished** rather than being cut off, skipping only the
  optional revision: `degraded: true`, `degraded_reason: "time_budget"`,
  `selected_version: "initial"`, `route: "pending"` (its verdict was PASS, and degrading does not
  change the standard);
- slot-2 had not started and was reported `skipped_time_budget`;
- the summary read `succeeded=1 failed=0 skipped=1 degraded=1` — no partial result presented as
  success.

That is the design.md §9 behaviour observed end to end, not simulated.

## `/ping` under load, verified locally

12 samples at 4s intervals while a live generation was in flight through the real
`BedrockAgentCoreApp` server: every response between **1.2ms and 8.7ms**, far inside the 1s
budget. No blocking call reaches the shared event loop — subprocesses go through
`create_subprocess_exec` and the catalogue is loaded via `asyncio.to_thread`.

## Still to measure on Runtime

- End-to-end timing from inside AgentCore Runtime (ARM64, 2 vCPU / 8GB).
- A 6-material batch inside the 15-minute limit.
- `/ping` during a 6-material Runtime batch. Use `scripts/check_ping.sh`.
- ARM64 image build and size. **Not verified**: Docker Hub is unreachable from the development
  machine (`registry-1.docker.io` EOF on manifest fetch), so `python:3.12-slim` cannot be pulled.
  The Dockerfile and `deploy.sh` size gate are written but unexercised.
- TPM/RPM ceilings on the mantle channel. Unknown at planning time and still unknown; no 429
  was seen at concurrency 3, which bounds it from below but does not locate it. On 429s, lower
  concurrency rather than adding retries — retries increase total elapsed time and make the
  15-minute wall more likely, not less.

## 容器内实测（2026-07-28，ARM64 镜像）

镜像：`ielts-backend:dev`，`arm64/linux`，**77 MB**（AgentCore 上限 2048 MB）。
基础镜像改用 `public.ecr.aws/docker/library/python:3.12-slim`——本机 Docker Hub
不可达（`registry-1.docker.io` EOF），ECR Public 可达且对 Bedrock 部署更合适。

HTTP 契约实测：

- `GET /ping` → `{"status":"Healthy",...}`，**7–25 ms**
- **生成进行中并发探测 `/ping` 仍为 14–22 ms**，证实 entrypoint 未阻塞事件循环
  （这是 AgentCore 判定实例健康的关键，design.md §7 的硬约束）
- `POST /invocations` 非流式分支（`action=list_scenarios`）正常，读取镜像内配置
- `POST /invocations` SSE 分支跑通完整 Loop

单套完整 Loop（`booking-hotel`，容器内，含 2 次重生成 + 1 次基础设施重试）：

| 阶段 | 秒 |
|---|---:|
| generate_1 | 84.8 |
| generate_2 | 32.7 |
| generate_3 | 38.5 |
| audit | 29.4 |
| revise | 10.9 |
| re_audit | 28.2 |
| validate ×4 | 0.5 |
| **total** | **224.9** |

结果：`PASS 100/100`，0 findings，498 词 / 31 轮 / 16-15 均衡，
`selected_version: revised`，cross_check **10/10 匹配、0 unrecoverable、0 unintended**。

### 批量上限复核 —— 上限已删除

实测区间：146s（1 次重生成）→ 225s（2 次重生成）。这两个数现在对着的是**单次 invoke**，
因为 web 层每套材料发一次独立请求：

| 单套耗时 | 对该次 invoke 的 810s 可用预算 |
|---:|---|
| 146s | 18% |
| 225s | 28% |

原先「6 套两波 292–450s，仍在 900s 内」的算法曾是 `max_batch: 6` 的实测依据；
同一批数据在每套一次 invoke 的架构下说的是另一件事——**上限没有平台依据了**，所以它被删掉。

重生成仍是耗时的主要方差来源（一次约 +33～85s），因此降低重生成率仍然是压缩 P95 最有效的
手段。变化的是并发的意义：它不再决定「一批能否跑完」，只决定「一批要跑几波」，以及会不会
把模型配额打爆。

### 重生成原因（本次样本）

1. `infra_retry`：模型返回了裸 turn 对象而非 material/blueprint 包装 —— SDK 层重试已覆盖
2. 对话 352 词，低于硬限 450
3. 结尾缺「转 Part 2」指引；开场缺「四部分」说明；旁白 151 词，低于 160

第 3 类是真实的模型疏漏，不是校验器过严：我用 31 份归档真题复核，
`RECORDINGS_RE` 与 `PARTS_RE` 对 **7/7** 份 full-mode 开场白全部命中（含 "four sections" 变体）。
