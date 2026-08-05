# 发布台账

回退时唯一需要查的表。每次 `runtime.sh` 或 `service.sh` 之后，在这里补一行。

不记录就等于没有回退能力：ECR 镜像标签本身不携带 git commit，三天后没人能凭
`two-states-20260801` 反推出它是从哪个 commit 构建的。

## 当前生产版本

**`prod-20260805`** — Runtime version **17** + ECS taskdef **`ielts-part1-web:32`**

回退命令见 [`rollback.sh`](rollback.sh)：

```bash
bash deploy/rollback.sh --to prod-20260805 --dry-run   # 先看清，什么都不改
bash deploy/rollback.sh --to prod-20260805
```

`prod-20260801`（taskdef :31）是上一个锚点，两者代码相同，只差
`ALLOWED_EMAIL_DOMAINS`。除非需要临时放开域名限制，回退目标用 `prod-20260805`。

## 台账

新到旧。`git tag` 一列是可回退锚点；空表示该次发布没有单独打 tag。

| 日期 | 镜像标签 | git commit | git tag | Runtime ver | taskdef | 说明 |
|---|---|---|---|---|---|---|
| 2026-08-05 | `two-states-20260801`（未变） | `fa598d9` | `prod-20260805` | 17（未变） | **32** | 仅收紧 `ALLOWED_EMAIL_DOMAINS`：`*` → `amazon.com,example.com`，镜像未重建。同时作为回退演练素材，见下方演练记录 |
| 2026-08-01 | `two-states-20260801` | `380869a` | `prod-20260801` | 17 | 31 | 状态收到两个、撤回改为整批、候选保留期延到 30 天 |
| 2026-07-31 | `agent-autonomy` | `a3bf922`（推定） | — | 16 | ~30 | Agent 自主性重写：Python 只做调度，模型步骤改为调预定义 Agent |
| 2026-07-30 | `known-good-20260730`（backend）/ `dev`（frontend） | — | `backup/main-20260801-pre-agent-autonomy`（远端分支） | 15 | — | agent-autonomy 重写之前的最后一版 |

> 2026-07-31 及更早的行是从 ECR 推送时间与 git 提交时间对照**推定**的，不是当时记录的。
> `dev` 这个 frontend 标签是在 ECR 设为 IMMUTABLE 之前推的，曾被覆盖过，不能作为可信回退点。
> 从 `prod-20260801` 起，每行都在部署时写入。

## 怎么补一行

```bash
# 部署后，采集三个真实值（而不是凭记忆写）
aws bedrock-agentcore-control list-agent-runtime-endpoints \
  --agent-runtime-id ielts_part1_runtime-fA4wkq8nKf \
  --query 'runtimeEndpoints[?name==`DEFAULT`].liveVersion' --output text
aws ecs describe-services --cluster ielts-part1 --services ielts-part1-web \
  --query 'services[0].taskDefinition' --output text
git rev-parse --short HEAD
```

要让某一行成为可回退锚点，给它打一个带附注的 tag 并推上去：

```bash
git tag -a prod-<日期> <commit> -m "镜像/Runtime ver/taskdef ..."
git push origin prod-<日期>
```

## 回退演练记录

回退路径没跑过就等于不存在。演练记录在这里，包含实测耗时。

### 2026-08-05 — 首次演练

**素材**：把 `ALLOWED_EMAIL_DOMAINS` 从 `*` 收紧为 `amazon.com,example.com`。这项改动本身
是待办（README §5 要求公网部署必须限制域名），同时是理想的演练素材：它只改 taskdef 里的
环境变量，不重建镜像，唯一变量就是配置本身。

现有 23 个用户分布在 `amazon.com` 和 `example.com`；`email_domain_allowed` 只在
`web/auth.py` 的 `register()` 里调用，登录不查域名，所以收紧不影响任何已注册用户登录。

**步骤与实测耗时**

| # | 动作 | 结果 |
|---|---|---|
| 1 | 基线：`/healthz` | 200；`_batches/` 34、`_candidates/` 363 |
| 2 | 基线：用 `@drill-invalid.test` 注册 | **HTTP 200 通过** —— 直接证实 `*` 的问题 |
| 3 | 基于 :31 注册 taskdef **:32**（只改环境变量，镜像字节相同） | revision 32 |
| 4 | `update-service` → :32，等 `services-stable` | **165s** |
| 5 | 验证收紧：域名外注册 / `@example.com` 注册 | **403** `EMAIL_DOMAIN_NOT_ALLOWED` / 200 |
| 6 | `rollback.sh --to prod-20260801 --dry-run` | 正确识别漂移 32→31，Runtime 判为无需动 |
| 7 | `rollback.sh --to prod-20260801` | **184s**，CloudFront `/healthz` 200 |
| 8 | 验证回退真的生效：域名外注册 | **恢复 200** —— 配置确实回到 :31 |
| 9 | 旧 session cookie `/api/auth/me` | **200** —— SESSION_SECRET 跨回退稳定，用户不掉线 |
| 10 | URL 与数据核对 | 域名不变；`_batches/` 34、`_candidates/` 363 未变 |
| 11 | 前滚回 :32（本轮要保留的收紧） | **181s** |
| 12 | 清理演练留下的 3 个探针账号 | users.json 26 → 23，无 admin 被删 |

**结论**

- 回退路径**双向可证**：不只验证了「回退后健康」，还验证了「被回退的那项配置真的消失了」。
  只看 `/healthz` 200 无法区分「回退成功」和「什么都没发生」。
- Web 层单层回退约 **3 分钟**。README 之前写的 `service.sh <known-good-tag>` 回退法是假的：
  `service.sh` 无条件 `docker build && docker push`，而 repo 是 IMMUTABLE，推已存在的 tag 必然
  失败。演练当天本机 Docker daemon 根本没运行，`rollback.sh` 不受影响 —— 这正是不依赖构建的价值。
- CloudFront 域名、ALB、S3 全程未被触碰；已登录用户不掉线。
- 未覆盖：**Runtime 层**没有实际切换（v17 就是目标，切了等于没切）。
  `update-agent-runtime-endpoint --agent-runtime-version` 的参数已核实存在，
  但真实切换要等下一次后端发布产生 v18 之后才能演练。这是当前唯一未经实测的分支。
