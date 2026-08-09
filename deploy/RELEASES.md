# 发布台账

回退时唯一需要查的表。每次 `runtime.sh` 或 `service.sh` 之后，在这里补一行。

不记录就等于没有回退能力：ECR 镜像标签本身不携带 git commit，三天后没人能凭
`two-states-20260801` 反推出它是从哪个 commit 构建的。

## 当前生产版本

**`comments-20260809`（`f3c8bc9`）**，承载在 Runtime version **30** +
ECS taskdef **`ielts-part1-web:45`**。代码在 `feat/listening-full-test`，**未合并 main**。

⚠️ **两层不同版：** 这一轮只重建了 frontend 镜像（web 层），backend 镜像和 Runtime 停在
`carrier-parens-20260809` / version **30**，因为改动全在 web 层。按 tag 回退时要注意
`rollback.sh --to comments-20260809` 找不到 backend 侧的同名镜像——web 层回退用
`--taskdef 45`，Runtime 层用 `--runtime-image carrier-parens-20260809`。

2026-08-08/09 的八次发布（Runtime 24-30 及本轮 web-only）最初从带未提交改动的工作树构建；
这些改动现已归档在 `f3c8bc9`。该 commit 可以复原代码内容，线上实际镜像与 task definition 仍以
本表记录的 `containerUri`、Runtime version 和 taskdef 为准。

上一版是 `carrier-parens-20260809`（Runtime 30 / taskdef 44，括号与空 carrier 规则修复）。
再往前是 `stream-budget-20260808`（Runtime 29 / taskdef 43，预算改按流式上限算）。往前依次是
`row-header-20260808`（28 / 42）、`rubric-gate-20260808`（27 / 41）、
`note-sections-20260808`（26 / 40）、`carrier-style-20260808`（25 / 39）、
`page-shape-20260808`（24 / 38）、`anchor-fix-20260808`（21 / 35，回滚基线
[`.deploy-baseline-20260808b.md`](.deploy-baseline-20260808b.md)）。

**版本号推断不出线上代码是哪一版**（19 装的是 17 的镜像，见下方第二次演练：DEFAULT 端点无法被
`update-agent-runtime-endpoint` 重新指向，只能用 `update-agent-runtime` 重推镜像，而那必然产生
新版本号）。判定线上代码只能查 `containerUri`：

```bash
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id ielts_part1_runtime-fA4wkq8nKf --agent-runtime-version 30 \
  --query 'agentRuntimeArtifact.containerConfiguration.containerUri' --output text
# → 907488872981.dkr.ecr.us-east-1.amazonaws.com/ielts-part1-backend:carrier-parens-20260809
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
| 2026-08-09（午 12:1x） | `comments-20260809`（**仅 frontend**；backend 停在 `carrier-parens-20260809`） | `f3c8bc9` | — | 30（未动） | **45** | **单人批注功能上线，只部 web 层。** 新增批注的增删、严重程度（critical/major/minor）、题目侧栏、Turn 折叠面板、定位跳转，并落 S3 持久化。后端新增 `web/comment_store.py`（`CommentService` + `S3CommentStore`/`InMemoryCommentStore`，last-write-wins，一材料一份 JSON 存在 `_comments/<material_id>.json`）与三条路由 `GET/POST /api/material-comments/{material_id}`、`DELETE /api/material-comments/{material_id}/{comment_id}`。**Runtime 刻意未动**（改动全在 web 层，出题/材料生成逻辑零变更），所以这是台账里第一次两层不同版，回退方式见「当前生产版本」的第一条 ⚠️。**IAM 无需改**：task role `ielts-part1-web-task` 的内联策略已按 `arn:aws:s3:::ielts-part1-materials-907488872981/*` 授 `s3:GetObject`/`s3:PutObject`，覆盖新的 `_comments/` 前缀。三条新路由自动受 `ApiAuthMiddleware` 会话保护（`PUBLIC_API_PATHS` 未加它们）。**验证要点**：taskdef 45 带着 `IELTS_AUDIO_BUCKET`——`build_comment_store()` 在这个变量为空时会静默退回 `InMemoryCommentStore`，那样批注会随任务重启消失，所以这个变量是本轮的关键配置而非可选项；`ALLOWED_EMAIL_DOMAINS=amazon.com,example.com` 已显式带上。路由存在性用**镜像内枚举 `app.routes`** 确认（三条路径与方法齐全），不能靠线上探针推断：`ApiAuthMiddleware` 在路由之前跑，任何未认证的 `/api/*` 都返回 401，连不存在的路径也是 401。另外**带 body 的 GET 会被 CloudFront 挡成 403 HTML**，不是应用行为——干净的 GET 返回应用自己的 JSON 401。前端三个 bundle 哈希全变（`index-CEW6rYx4.js`、`agentcore-B36tGOgS.js`、`index-gv5RB1vA.css`，均 CloudFront 200；旧 `index-DCdFAgHO.js` 已 302），这与「本轮真有前端代码改动」一致。门禁：web 313 项、backend+web 1062 项、`tsc --noEmit`、vitest 461 项 / 27 文件、oxlint 全过；`services-stable` 2:38，`/healthz` 200 @ 0.95/0.64/0.70s；`_comments/` 部署前后均为 0 对象（等人工产生第一条批注） |
| 2026-08-09（晨 10:5x） | `carrier-parens-20260809`（backend + frontend 同 tag） | `f3c8bc9` | — | **30** | **44** | **括号与空 carrier 规则修复。** 有行标签的 form 行（或行列标签齐备的 table 单元格）里，两侧 carrier 同时为空是**正常且正确**的——标签已经做了命名这件事。此前的分类器把这种「带标签的裸字段」按 carrier 为空算成 medial，逼着出题去编 `(as spelt)` 这类填充文字；现在 `has_structural_context()` 判定考生可见标签是否已识别该空格，是则归 `final`，否则仍按 QR-026 报「孤立空格」。括号本身不再是问题，只有四道检查（消除真实歧义、与录音/记录格式一致、非冗余、不泄漏）全过才允许；`(as spelt)`/`(as mentioned)` 这类针对作答行为的评论归入自然度发现。审核规则第 12 项同时明确「**问题是重复本身，不是措辞**」——修法是清空或改写 carrier，不是换同义词。schema 的 `carrier_before` 描述、出题规则、审核规则第 7/12 项四处同轮改齐。**镜像内实跑确认**了六种组合的判定（有标签+空 carrier→final 不报错；无行标签、或 table 缺列标签、或 note 无标签→仍报孤立空格；carrier 存在时分类逻辑不变）。预算四值未受影响（3600/300/3300/420 与 3300/3450 均已复核）。前端 bundle 哈希未变（本轮只改后端 Skill），按同一 tag 重建两层以保持台账与 `rollback.sh --to <tag>` 的一键回退语义 |
| 2026-08-08（夜 23:2x） | `stream-budget-20260808`（backend + frontend 同 tag） | `f3c8bc9` | — | **29** | **43** | **预算改按流式上限算，不再按同步上限。** `generate_sets` 走 SSE，适用 AgentCore 的 60 分钟流式上限，而内部四个值一直按同步的 900s 设：Runtime `IELTS_HARD_LIMIT` 900→**3600**、`IELTS_SAFETY_MARGIN` 90→**300**（工作窗口 810→**3300s**），web 每套墙钟 `WEB_PER_MATERIAL_WALL` 900→**3300**，`WEB_RUNTIME_READ_TIMEOUT` 900→**3450**（夹在 3300 与 3600 之间：低于后端窗口会让 web 先于平台放弃，高于 3600 则等一个已被平台关掉的流）。这正是 `web-1786200345304-1` 只交付 3/6 的根因——三个卡位材料都写完了，却被 `may_start_questions()` 以「剩余不足 420s」挡在出题阶段外，而整批只用掉 534s / 810s。`QUESTION_P95_SECONDS` **保留 420**：它作为阶段启动保护在 55 分钟窗口下仍然合理，而这一批实测出题阶段 280/291/324s（各含 2 轮修改），420 有余量。四个值线上都没有环境变量覆盖，已逐一在两个镜像内部实跑确认。前端 bundle 哈希未变（本轮只改 Python），同 tag 重建保持两层一致。**验收（`web-1786205364950-1`，6 套）：1121s 交付 6/6（`counts={succeeded:6, failed:0, skipped:0, degraded:0}`，六个子请求 `request.json` 全部 `status=succeeded`、`resumable_slots` 为空），`time_budget` 断点 0 次。** 关键证据是**两个卡位在 900s 之后才完成**——slot-6 于 t+941s、slot-4 于 t+1121s 正常 `complete` 并写出 `_questions/`；同步上限若适用，这两次 invocation 会在 t+900 前后被平台掐掉且不通知客户端（见「AgentCore invoke 时限」），它们不可能写出完整题目包。这坐实了 `generate_sets` 适用 60 分钟流式上限，也坐实了 810s 内部窗口就是上一批断点的根因。三个困难卡位这次都是把新预算花在重试上并成功（slot-4 出题崩溃→重启→判不可交付→换候选→通过；slot-5 换候选 1 次；slot-6 重启 1 次），旧窗口下无一能走完。限流 0 条，`/healthz` 全程 0.875–0.918s |
| 2026-08-08（夜 22:4x） | `row-header-20260808`（backend + frontend 同 tag） | `f3c8bc9` | — | **28** | **42** | table 的左上角格改由 `structure.row_header_label` 命名，不再永远留空：`column_labels` 只管右侧内容列，行标签列自己那一列也需要一个名字，否则考生读不出行标签在比什么。schema / 校验器（table 组缺它即报错）/ 出题规则 §4 / 审核规则第 13 项 / 前端 `TableLayout` 五处同轮改齐。**已知副作用**：`20260808-bike-hire-*` 这类旧 table 包现在校验不过（与 note 那条同性质，都是新契约对旧包的回溯要求） |
| 2026-08-08（夜 22:0x） | `rubric-gate-20260808`（backend + frontend 同 tag） | `f3c8bc9` | — | **27** | **41** | **蓝图阶段挡住装不进任何 rubric 的 target**：六条标准 rubric 里最宽的也只允许三词 + 一个裸数字，所以 `9 and 1`（1 词 + 2 数字）无解——出题阶段既不能换 target 也不能改 Script，只能空转到断点（实测 `web-1786195813332-1` 烧完 810s 交付 0 套）。`validate_part1.py` 新增 `WORD_LIMITS`/`budget_of`/`fits_any_rubric`，出题校验器删掉重复实现改为 import（同一套算术，避免两个真相源）；SKILL.md + specification.md 写入这条给出题人的规则。落在 preflight gate 2，所以会先进 3 次重生成反馈、仍不行才换候选材料。前端 bundle 哈希未变（本轮只改后端 Skill），同 tag 重建保持两层一致 |
| 2026-08-08（夜 21:2x） | `note-sections-20260808`（backend + frontend 同 tag） | `f3c8bc9` | — | 26 | 40 | note 版式改用 `structure.note_sections[]`（`heading` + `question_numbers`），校验器要求对该组题目形成精确且不重叠的覆盖，`hierarchy` 降级为旧包只读字段；前端按声明渲染，旧包退化成无标题列表而不按下标硬配。**已知副作用**：`20260808-accommodation-rental-b5cfe833` 等携带 legacy `hierarchy` 的旧包现在校验不过（前端仍能安全降级显示） |
| 2026-08-08（夜 20:5x） | `carrier-style-20260808`（backend + frontend 同 tag） | `f3c8bc9` | — | 25 | 39 | Form 空格不再粘连成 `record1`/`of3`（去掉 `.qp-blank` 的 `margin`，按标点决定是否补空格）；note 标题与对应题目紧邻；signpost 移入审核区不给考生看；Skill 禁止命令式 Form carrier；保留合理的括号单位提示 |
| 2026-08-08（夜 20:0x） | `page-shape-20260808`（backend + frontend 同 tag） | `f3c8bc9` | — | 24 | 38 | 题面按真实试卷排版：candidate face 不再显示 Form/Table/Note badge 和旁白窗口编号，内部信息移入独立 `.qp-audit` 审核区；修 Questions / instruction / title 的间距与层级；出题规则细化（label 与 carrier 各司其职、按自然结构选 layout、无真实比较轴不得用 Table、signpost 必须具体且 Script-grounded、Instructions 用标准 IELTS 固定措辞、Note 默认最多两层）。**taskdef 从 35 跳到 38**：36（`announce-delivered-20260808`）、37（`question-restart-20260808`）是此前已注册但未记账的两次 |
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
