# 发布台账

回退时唯一需要查的表。每次 `runtime.sh` 或 `service.sh` 之后，在这里补一行。

不记录就等于没有回退能力：ECR 镜像标签本身不携带 git commit，三天后没人能凭
`two-states-20260801` 反推出它是从哪个 commit 构建的。

## 当前生产版本

**`anchor-fix-20260808`（`12f3c30`）**，承载在 Runtime version **21** + ECS taskdef
**`ielts-part1-web:35`**。代码在 `feat/listening-full-test`，**未合并 main**。

上一版是 `gate-fix-20260808`（Runtime 20 / taskdef 34），回滚基线记在
[`.deploy-baseline-20260808b.md`](.deploy-baseline-20260808b.md)；再往前一格是
`two-states-20260801`（Runtime 19 / taskdef 32），记在
[`.deploy-baseline-20260808.md`](.deploy-baseline-20260808.md)。

**版本号推断不出线上代码是哪一版**（19 装的是 17 的镜像，见下方第二次演练：DEFAULT 端点无法被
`update-agent-runtime-endpoint` 重新指向，只能用 `update-agent-runtime` 重推镜像，而那必然产生
新版本号）。判定线上代码只能查 `containerUri`：

```bash
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id ielts_part1_runtime-fA4wkq8nKf --agent-runtime-version 21 \
  --query 'agentRuntimeArtifact.containerConfiguration.containerUri' --output text
# → 907488872981.dkr.ecr.us-east-1.amazonaws.com/ielts-part1-backend:anchor-fix-20260808
```

回退命令见 [`rollback.sh`](rollback.sh)。**Runtime 分支已于 2026-08-07 修好**：改用
`update-agent-runtime` 重放目标版本记录的整份配置（不再调对 DEFAULT 端点无效的
`update-agent-runtime-endpoint`），顺序改成先 Runtime 后 web，且 web 步失败会把 Runtime 补偿回去。

```bash
bash deploy/rollback.sh --to <tag> --dry-run   # 先看清，什么都不改；顺带预检两层镜像是否还在 ECR
bash deploy/rollback.sh --to <tag>
bash deploy/rollback.sh --runtime-image two-states-20260801   # 直接按镜像标签回退，绕开版本号
```

`--runtime-version 17` 现在读作「回到 v17 记录的那个镜像和配置」，**不是**「让 v17 重新 live」——
后者 AWS 不允许。回退结果由 `containerUri` 和 ECS taskdef 判定，脚本不再用版本号推断代码版本。

## 台账

新到旧。`git tag` 一列是可回退锚点；空表示该次发布没有单独打 tag。

| 日期 | 镜像标签 | git commit | git tag | Runtime ver | taskdef | 说明 |
|---|---|---|---|---|---|---|
| 2026-08-08（晚） | `anchor-fix-20260808`（backend + frontend 同 tag） | `12f3c30` | — | **21** | **35** | quote 落在相邻 ±1 turn 且能唯一逐字定位时归一化为 agreement 并留痕；引用邻句仍硬阻断；生成器与 validator 双侧要求 quote 与 `turn_index` 一致；agreement 不足若只是已列 adjacency 的算术则不重复加 blocker。**顺带把 taskdef 34 静默变回 `*` 的 `ALLOWED_EMAIL_DOMAINS` 收回 `amazon.com,example.com`**。前端无改动但同轮重建，保持两层标签一致。验收范围仅健康检查 + 接口版本，一套真实 `generate_sets` 由人从前端手动发起 |
| 2026-08-08 | `gate-fix-20260808`（backend + frontend 同 tag） | `062668c` | — | 20 | 34 | 交付门不再因单个 MINOR 就拒；固化 `answer_category` 边界；修 `rollback.sh`。**此次部署没在环境里带 `ALLOWED_EMAIL_DOMAINS`，`service.sh` 的 `${ALLOWED_EMAIL_DOMAINS:-*}` 默认值把公网注册静默放开了**，次轮修回 |
| 2026-08-07（晚） | `two-states-20260801`（回退，镜像未重建） | `fa598d9` 的代码 | — | **19**（= 17 的镜像） | **32** | **回退 `prod-20260807`**：验收 6 条里 2 条硬失败，5 次真实 invocation 一套都没交付。见下方第二次演练记录 |
| 2026-08-07 | `question-sets-20260807`（backend + frontend 同 tag） | `c9b1709` | `prod-20260807` | 18（已下线） | 33（已下线） | 前端改发 `generate_sets`（题目自此真的会生成，此前 `_questions/` 一直为空）；出题的 13 个环节名与断点状态进入进度显示。两个产物同轮上线——线上旧镜像的 `web/app.py` 里 `material-questions` 出现 0 次，只部一半会让新页签调不存在的接口。**当日回退** |
| 2026-08-05 | `two-states-20260801`（未变） | `fa598d9` | `prod-20260805` | 17（未变） | **32** | 仅收紧 `ALLOWED_EMAIL_DOMAINS`：`*` → `amazon.com,example.com`，镜像未重建。同时作为回退演练素材，见下方演练记录 |
| 2026-08-01 | `two-states-20260801` | `380869a` | `prod-20260801` | 17 | 31 | 状态收到两个、撤回改为整批、候选保留期延到 30 天 |
| 2026-07-31 | `agent-autonomy` | `a3bf922`（推定） | — | 16 | ~30 | Agent 自主性重写：Python 只做调度，模型步骤改为调预定义 Agent |
| 2026-07-30 | `known-good-20260730`（backend）/ `dev`（frontend） | — | `backup/main-20260801-pre-agent-autonomy`（远端分支） | 15 | — | agent-autonomy 重写之前的最后一版 |

> 2026-07-31 及更早的行是从 ECR 推送时间与 git 提交时间对照**推定**的，不是当时记录的。
> `dev` 这个 frontend 标签是在 ECR 设为 IMMUTABLE 之前推的，曾被覆盖过，不能作为可信回退点。
> 从 `prod-20260801` 起，每行都在部署时写入。
>
> **`service.sh` 里 `ALLOWED_EMAIL_DOMAINS` 是 `"${ALLOWED_EMAIL_DOMAINS:-*}"`**：环境里不带这个
> 变量，部署就会把公网注册静默放开（2026-08-08 的 taskdef 34 就是这么丢的）。每次跑
> `service.sh` 都要显式带上 `ALLOWED_EMAIL_DOMAINS=amazon.com,example.com`；同理，按 taskdef
> 回退时要先确认目标 revision 的这个值，`--taskdef 34` 会把 `*` 一起带回来。

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
  → **2026-08-07 演练了，这个分支是坏的。见下。**

### 2026-08-07 — 第二次演练（真回退，不是演习）

`prod-20260807` 验收失败后按预案回退 18/33 → 17/32。**Runtime 层第一次真正切换**，也就是
上一次留下的那个唯一未测分支。

| # | 动作 | 结果 |
|---|---|---|
| 1 | `rollback.sh --to prod-20260805 --dry-run` | 正确识别 18→17、33→32 |
| 2 | `rollback.sh --to prod-20260805` | web 层 **stable**；Runtime 层 **失败**，见下 |
| 3 | 手工 `update-agent-runtime` 重推 `two-states-20260801` | 产生 version **19**，READY |
| 4 | 核对 v19 的 `containerUri` | `ielts-part1-backend:two-states-20260801` —— 确是旧镜像 |
| 5 | 核对线上 bundle（不是只看 `/healthz`） | `generate_sets`/`resumable_slots`/`questions_started`/断点文案 **各 0 次** |
| 6 | `/healthz`、旧 session cookie | 200 / 0.85s；`/api/auth/me` **200**，用户不掉线 |
| 7 | S3 核对 | `_batches/` 285、`_candidates/` 363、`_slots/` 13 未被删改 |

**`rollback.sh` 的 Runtime 分支当时不能用**（已于同日修复，见下）。旧代码第 163 行调
`update-agent-runtime-endpoint --agent-runtime-version`，AWS 直接拒绝：

```
ConflictException: Default endpoints are managed through agent updates.
Please use the update agent operation.
```

上一次演练只核实了「这个参数存在」，没核实「对 DEFAULT 端点可用」——参数存在不等于这条路走得通，
这正是未跑过的路径等于不存在的原因。而且它是**先 web 后 Runtime**，于是失败点把生产留在
「web 已回退、Runtime 未回退」的半回退态，比两端任何一端都糟。修的时候要一起改这两件事：
Runtime 分支换成 `update-agent-runtime`（代价是版本号只增不减，见「当前生产版本」一节），
且顺序要能容忍中途失败。

第 161 行的注释写着这样做是为了「版本列表保持真实历史，不积累 v19 = 旧 v17 的条目」——
那个目标现在达不到了：唯一可用的回退手段必然产生新版本号，所以版本号不再能反推代码版本。

**修复内容（同日完成）。** 四处，都对着上面这次失败的具体形状：

1. **Runtime 分支换成 `update-agent-runtime`**，请求体由 `get-agent-runtime <目标版本>` 的整份配置
   回放生成，而不是在脚本里重列 flag。手工回退那次是抄五个 flag 过去的，任何没想到要抄的字段都会
   被静默重置成默认值；回放整份配置则连以后 API 新增的字段也一起带回去。
2. **顺序改成先 Runtime 后 web。** Runtime 是会失败的那一步，让它先跑：失败时什么都还没动，脚本直接
   退出并明说生产未变。web 步若失败，`compensate` 用开跑前录好的配置把 Runtime 推回原样——那份配置
   在动手之前就写进临时目录了，故障当时不必再赌一次 API 调用能成功。
3. **动手前预检两层。** Runtime 目标镜像和 ECS taskdef 的镜像都要求还在 ECR（被生命周期策略清掉的
   镜像照样能读出版本记录、照样接受 update，然后拉不到镜像），taskdef 还要求是 `ACTIVE`——
   `DEREGISTERED` 的 revision 读得出来也起不了任务，而 `update-service` 会先接受再几分钟后失败。
4. **结果以 `containerUri` + ECS taskdef 判定**，不看 Runtime 版本号；不一致就以 MISMATCH 退出非零。
   `/healthz` 仍然打印，但明确标注它分不清「回退成功」和「什么都没发生」。

演练当天的六条失败路径都用 `--dry-run` 实测过（不存在的版本号、不在 ECR 的镜像标签、不存在的
taskdef、两个后端选项同时给、什么都不给），每条都在改动任何东西之前拒绝退出。
