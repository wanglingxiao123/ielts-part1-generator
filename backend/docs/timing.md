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
| `IELTS_CONCURRENCY` | 6 (code default) | no throttling observed at the 3 concurrent slots that were actually measured; the default was raised to 6 when one invocation stopped meaning one batch. Effectively dead in production: one slot per invocation means `BatchRequest` clamps it to 1. Still governs the CLI, where 3 remains the measured-safe value. |
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

## 平台时限实测（2026-08-06，独立探针 Runtime）

回答的问题：**一次 `InvokeAgentRuntime` 能挂多久不返回，同步和流式是否不同？**
此前仓库里有三种互相矛盾的说法，其中 `material/part1-question-stage-analysis.md` §7.1
断言「AgentCore 没有单次 invocation 15 分钟同步硬上限」。现在这是实测结论，不是推断。

两条 quota（`aws service-quotas list-service-quotas --service-code bedrock-agentcore`，
与官网 `bedrock-agentcore-limits.html` → Runtime → Invocation limits 一致，**两条都不可调**）：

| Quota ID | 值 | 适用路径 | 实测结论 |
|---|---:|---|---|
| `L-3ED45A13` | 15 min = 900s | 同步（`application/json`） | **成立**。900.5s 后容器被杀 |
| `L-C91AC63F` | 60 min = 3600s | 流式（SSE / WebSocket） | **成立**。1206s 正常收尾，未被 900s 切 |

测法：独立探针 Runtime（`ielts_part1_probe`，`backend/probe_app.py` +
`backend/probe.Dockerfile`，只 sleep 不调模型），客户端 `backend/scripts/probe_runtime_timing.py`
（`read_timeout=3600`、**重试关闭**、直连 `bedrock-agentcore` endpoint 不经 ALB/CloudFront）。
生产 Runtime 全程未被触碰，探针测完删除。

### 同步路径：900s 到了**杀容器，但不通知客户端**

这是本次最要紧的发现，也是三种说法都没说到的一点。1000s sleep 的探针，容器侧日志每 60s 一行：

```
probe_sync ENTERED, sleeping 1000.0s
probe_sync alive at  60.0s of 1000.0s
...                                      ← 60s 一行，节奏完整无缺口
probe_sync alive at 900.5s of 1000.0s    ← 最后一行
                                         ← 960.5s 那行再也没来，也没有 FINISHED
```

即容器在 **(900.5s, 960.5s]** 之间被杀死，与 900s 的 quota 吻合。**跑了两次，两次都断在
`alive at 900.5s`**（不同 session、不同时间、同一镜像），所以这不是偶发：

| | 最后一条容器日志 | 推断的杀死区间 |
|---|---|---|
| 第一次（10:05:51 进入） | `alive at 900.5s` | (900.5s, 960.5s] |
| 第二次（10:39:51 进入） | `alive at 900.5s` | (900.5s, 960.5s] |

区间上界是 60s 的日志间隔造成的分辨率，不是观测到的宽容度——真实的杀死时刻在 900s 之后不久。
而平台**没有为此写任何日志**
（没有 5xx、没有超时事件、没有终止记录），**也没有给客户端任何响应**。

这一点用两个不同的 `read_timeout` 各测了一遍，**客户端的等待时长完全由它自己的 read timeout
决定，与 900s 无关**：

| 客户端 `read_timeout` | 客户端实际墙钟 | 容器实际死亡时刻 | 白等了 |
|---:|---:|---:|---:|
| 3600s | **3600.517s** | ~900s | ~2700s |
| 3600s（复跑） | **3600.526s** | ~900s | ~2700s |
| 1500s | **1500.7s** | ~900s | ~600s |

三次的异常完全一致：

```
exception : botocore.exceptions.ReadTimeoutError      ← botocore 本地异常，不是服务端错误
message   : Read timeout on endpoint URL: "https://bedrock-agentcore.us-east-1.amazonaws.com
            /runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A907488872981%3Aruntime
            %2Fielts_part1_probe-hAXrU75Hx4/invocations"
RequestId : 无 —— ReadTimeoutError 不带 .response，无 RequestId 可对 CloudWatch
```

**结论：同步路径超限时，客户端唯一的止损是它自己的 `read_timeout`。** 平台不回 504、不回任何
错误码、不断开连接。所以同步路径上 `read_timeout` 设多大，一次超限就白等多久——设成 3600s
就是 3600s 的静默等待，而后端早在 900s 就死了。这是 §7.2 讨论提高 `READ_TIMEOUT_SECONDS`
时必须并进去的一条：**同步路径的 read timeout 不该超过 900s**，因为超过的部分只会变成
「后端已死但前端还在等」的时间，没有任何一秒是有用的。

对照组（同一路径、同一镜像、120s sleep）证明这不是探针或路径本身坏了：

| | 对照组（120s） |
|---|---|
| `ended_by` | `returned` |
| `contentType` | `application/json`（确认走的是非 SSE 分支） |
| 客户端墙钟 | 126.102s |
| 容器自报 `slept_seconds` | 120.016s |
| 容器日志 | `ENTERED` → 2 条 alive → `FINISHED` → SDK `completed successfully (120.017s)` |

两个时钟一致，说明「900s 那次容器日志断在 900.5s」是被杀，不是没被调度到——
这个区分需要容器侧的进度日志才能做，第一次跑因为只在 handler 返回时才有日志，
数据无法区分「被杀」和「从未派发」，所以补了 `PROBE_PROGRESS_SECONDS` 才定案。

### 流式路径：跨过 900s，跑到 1206s 正常收尾

15s 心跳、计划 1200s 的 SSE 探针：

| | 值 |
|---|---|
| `contentType` | `text/event-stream; charset=utf-8` |
| 首字节 | 5.971s |
| 总墙钟 | **1205.98s** |
| data 帧 | 82，**含收尾帧** `{"type":"probe_completed","heartbeats":80,"elapsed_seconds":1200.015}` |
| 最大心跳间隔 | 30.015s |

收尾帧是关键：没有它，「跨过 900s 正常跑完」和「900s 被切、客户端只收到那之前的心跳」
在客户端看起来都是「心跳停了」。收到 `probe_completed` 才排除了后者。

最大间隔 30.0s（15s 的两倍，出现 4 次）说明**有心跳被合并投递**。这不影响本次结论——
30s 仍远低于 ALB 120s / CloudFront 60s 的 idle 门槛——但意味着 15s 的心跳设置不能假定
「客户端每 15s 必收到一次」。若日后把心跳间隔调到 60s 附近，实际间隔可能翻倍到 120s
并撞上中间层 idle 超时。

### 本轮不改任何数值

`READ_TIMEOUT_SECONDS`、`IELTS_HARD_LIMIT`、`P95_PER_MATERIAL`、`REVISION_COST` 一个都没动。
生产走 SSE，因此受 3600s 约束而非 900s——但真正的取值依赖题目阶段存在后重测 P95，
现在改只是换一组猜测。另：`material/part1-question-stage-analysis.md` §7.3 列的约 40 处
B 类叙述错误（把 900s 说成「平台同步上限」）本轮**未逐处修改**——改动面 §7.3 已列全，
成本在逐处措辞而非判断，留给真正调整数值的那个任务一起改，免得改两遍。

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
