# 发布台账

回退时唯一需要查的表。每次 `runtime.sh` 或 `service.sh` 之后，在这里补一行。

不记录就等于没有回退能力：ECR 镜像标签本身不携带 git commit，三天后没人能凭
`two-states-20260801` 反推出它是从哪个 commit 构建的。

## 当前生产版本

**`group-size-20260809`（**无对应 commit**，见下；分支 `fix/question-groups-independent-of-audio-windows`）**，
承载在 Runtime version **38** + ECS taskdef **`ielts-part1-web:48`**。
digest `sha256:a2622bc9f31eda5e03fcb8577fd309a1099b7cdd682641517cf7d5588e3ef4f4`。**未合并 main**。

⚠️ **两层不同版。** 本轮只重建了 backend（改动全在 skills 层的散文规则），web 层停在
`peer-layouts-20260809` / taskdef **48**。按 tag 回退时 `rollback.sh --to group-size-20260809`
找不到 frontend 侧的同名镜像——web 层用 `--taskdef 48`，Runtime 层用
`--runtime-image group-size-20260809`。

❌ **commit 不能复原镜像。** 构建时工作树有 44 处未提交改动，HEAD 停在 `8060974`
（那是 `form-table-20260809` 的代码）。`git checkout 8060974` 得到的**不是**镜像里的代码。
`job-title-20260809`（37）、`layout-fidelity-20260809`（36）与 `peer-layouts-20260809`（35）
同样如此。这四轮要么补一个归档 commit，要么按镜像标签回退——版本号和 commit 都推不出线上代码。
**digest 是唯一能凭线上信息反查的锚点**：tag 虽然 IMMUTABLE，但 digest 才是 Runtime
`containerUri` 解析后真正拉到的层。

上一版是 `job-title-20260809`（Runtime 37 / taskdef 48，digest `sha256:616469d1…90488136`）。
再往前是 `layout-fidelity-20260809`（Runtime 36 / taskdef 48）、
`peer-layouts-20260809`（Runtime 35 / taskdef 48）。再往前是
`form-table-20260809`（Runtime 34 / taskdef 47 未动）、
`multi-col-table-20260809`（`300e95d`，Runtime 33 / taskdef 47，两层同 tag 且 commit 能复原镜像）、
`single-group-20260809`（Runtime 32 / taskdef 46 未动，只部 Runtime；题组偏好改为
先试单一自然题组 + 表格限一个内容列——**后半条已被 v33 取代**）。再往前是
`group-windows-20260809`（Runtime 31 / taskdef 46，题组与旁白窗口解耦）、
`comments-20260809`（Runtime 30 未动 / taskdef 45，单人批注，第一次只部 web 层；
两层不同版，回退时 web 用 `--taskdef 45`、Runtime 用 `--runtime-image carrier-parens-20260809`）、
`carrier-parens-20260809`（Runtime 30 / taskdef 44，括号与空 carrier 规则修复）、
`stream-budget-20260808`（Runtime 29 / taskdef 43，预算改按流式上限算）。往前依次是
`row-header-20260808`（28 / 42）、`rubric-gate-20260808`（27 / 41）、
`note-sections-20260808`（26 / 40）、`carrier-style-20260808`（25 / 39）、
`page-shape-20260808`（24 / 38）、`anchor-fix-20260808`（21 / 35，回滚基线
[`.deploy-baseline-20260808b.md`](.deploy-baseline-20260808b.md)）。

**版本号推断不出线上代码是哪一版**（19 装的是 17 的镜像，见下方第二次演练：DEFAULT 端点无法被
`update-agent-runtime-endpoint` 重新指向，只能用 `update-agent-runtime` 重推镜像，而那必然产生
新版本号）。判定线上代码只能查 `containerUri`：

```bash
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id ielts_part1_runtime-fA4wkq8nKf --agent-runtime-version 38 \
  --query 'agentRuntimeArtifact.containerConfiguration.containerUri' --output text
# → <aws-account-id>.dkr.ecr.us-east-1.amazonaws.com/ielts-part1-backend:group-size-20260809
```

**`deploy/runtime.sh` 只重指向，不构建。** 直接给它一个还没推的 tag 会以
`not found. Push it first` 退出（本轮就撞了一次）。完整的后端发布是两步：

```bash
bash backend/scripts/deploy.sh <aws-account-id>.dkr.ecr.us-east-1.amazonaws.com/ielts-part1-backend <tag>
bash deploy/runtime.sh <tag>
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
| 2026-08-09（夜 22:3x） | `group-size-20260809`（**仅 backend**；frontend 停在 `peer-layouts-20260809`）<br>digest `sha256:a2622bc9…8e3ef4f4` | **无（工作树 44 处未提交，HEAD `8060974`）** | — | **38** | 48（未动） | **题组通常至少 3 题，1–2 题碎组要有独立理由。** 纯散文规则轮：材料生成 SKILL、材料 specification、出题 SKILL、题目审核 SKILL、审核 rules 第 15 项五处同轮写入「Groups should normally contain at least three questions … unless it represents a genuinely independent information structure that cannot naturally join either adjacent group」。审核 rules 第 15 项把它列为 SC-015 finding，第 13 项补入稀疏对角矩阵的占位符判据（`—`/`-`/`N/A`/空串，「只适用于一行的维度不是共享比较轴」），新增第 16 项「伪 Form 或被压制的 Note」；出题 SKILL 把第 2 步从「选择题组」改写为「渲染蓝图题组」并明确三种 layout 的正面判据（form=一条真实记录的字段；note=需求/偏好/流程/设施/建议/理由/安排等主题化说明点；table=重复实体在共享维度上比较），删掉 note 的 fallback 地位。**Python 侧无对应硬校验**——这一层是 Agent 语义判断，`run_tests.py` 加的是**散文断言**：对四个文件各查三个短语（`at least three` / `genuinely independent` / `cannot naturally join`），共 12 条新用例。**`question-rules.md` 与可行性 SKILL 有意不含这条**：前者只讲「组数由蓝图决定、出题阶段不得增删移动边界」，≥3 属规划期判断；后者是**已知缺口**，见下方遗留。门禁：镜像内 `run_tests.py` **610 项全过 exit=0**（含上述 12 条），backend+web 1063 项。**验证手段本轮换过一次**：`docker run` 一度全部卡在 `Created`（daemon API 正常、`docker ps`/`system df` 秒回，但容器起不来），改用 `docker create` + `docker cp` 把 `/app/backend/skills` 整棵取出比对——**镜像内 25 个 skill 文件与工作树逐字节一致**，差异只有 `.DS_Store`/`__pycache__`/`tests/`/zip（本就不入镜像）；随后容器层恢复，又在真容器内复跑了一遍 610 项确认。**只部 Runtime**：改动全是 skills 下的 5 个 md + `run_tests.py`，`frontend/src` 与 `backend/` Python 均未动。Runtime READY，`containerUri` 已核为新镜像，`/healthz` 200 @ 1.00/0.84/0.68s，web 层确认未动（taskdef 48、running 1/1、单 deployment）。未运行实际生成测试（由人在前端验证）。**遗留**：可行性 SKILL 是唯一能在材料落盘前拦住 1–2 题碎组的关卡，但它没有这条表述——上一轮刚给它加了「在蓝图阶段拒绝不自然的组边界」，措辞里不含题数下限，所以碎组只会在出题/审核阶段被发现，那时已无法改题组规划 |
| 2026-08-09（夜 20:5x） | `job-title-20260809`（**仅 backend**；frontend 停在 `peer-layouts-20260809`）<br>digest `sha256:616469d1…90488136` | **无（工作树 43 处未提交，HEAD `8060974`）** | — | **37** | 48（未动） | **`answer_category` 加第 14 类 `job_title`，补上「职位名无处可归」的结构性缺口。** 上一批（`web-1786275974185-2`，Runtime 36 首批）只交付 5/6，slot-3 这个位置连开三代（`slot-1` → `slot-1r1` → `slot-1r2`）、每代烧 3 份候选，正好撞满 `MAX_REPLACEMENT_SLOTS`/`MAX_CANDIDATE_SWAPS` 的 9 份上限；**九次失败全是 `material/feasibility_regenerate` 且全都指向 `answer_category` 语义，零次涉及 layout**。根因是 employment-vacancy 把职位名（`warehouse assistant`）选作答案点，而 13 个枚举值里没有职业/角色，rubric 又明写「There is no catch-all」——换多少份材料都必拒，与 `9 and 1` 装不进任何 rubric 同性质。本轮 `ANSWER_CATEGORIES` 13→**14**（`job_title` 排在 `quantity` 之后），read/write 侧蓝图 schema、`question_package.schema.json` 的 enum 同步 13→14，`feasibility.schema.json` 描述文本改为「14 permitted strings」；`answer-category-decisions.json` 新增 `named_occupation_or_role` 规则并**插在 order 2**，其余规则依次后移（artefact→3、contact→4、location→5、service/facility→6、preference→7、no_catch_all→8），`$comment` 的「seven」改「eight」。**插在 2 而不是追加在末尾是关键**：职位名同时像 facility（工作地点）也像 service（所做的事），排序若在 `performed_or_merely_present`（order 6）之后就仍会被判成 facility——缺的是顺序不是措辞。**镜像内验证**：`ANSWER_CATEGORIES` n=14 且含 `job_title`、仍无 `other`；两处 schema 的 `answer_category` enum 各 n=14 含 `job_title`；决策表 14 类与 read-side enum **完全同序**；`procedure` 的 order 连续 1..8 无重复，`job_title` 落在 order 2 且 facility/service 规则严格排在它之后；13 条 `cases` 的 `expect` 全在 14 类内，其中 `warehouse assistant` 一例 `category=job_title` / `not=facility`。**新旧镜像同一输入差分**：把真实生产蓝图 `20260809-employment-vacancy-b5b8eb21` 的 Q6 改成 `answer_category=job_title`，在 `layout-fidelity`（v36）下报 `blueprint.items[5].answer_category 'job_title' is not in the taxonomy [...]` 1 错，在本轮 v37 下 `ok=True` 0 错——正是上一批走不通的那条路现在通了。另 `shasum` 逐字节比对 9 个相关 skill 文件（校验器 ×2、schema ×4、决策表、可行性 SKILL、审核规则），镜像内与工作树完全一致。**只部 Runtime**：`job_title` 只出现在 skills/schema 与前端 `contracts/*.ts`（由 schema 重新生成的类型联合），grep 确认 `contracts/` 与 `mocks/fixtures/` 之外没有任何运行期 `answer_category` 映射，前端渲染不需要重建。门禁：Skill 基线**全过**（含 `the table's 14 values are the schema's enum, in the same order`、四条 `... accepts job_title`、`'warehouse assistant' -> job_title (rule 2, not facility)`、`'BT14 9BJ' -> location (rule 5, not contact)`）、backend+web **1063 项**、`tsc --noEmit` exit 0。Runtime READY，`containerUri` 已核为新镜像，`/healthz` 200 @ 0.94/0.65/0.70s，web 层确认未动（taskdef 48、running 1/1、单 deployment）。未运行实际生成测试（由人在前端验证）。**遗留**：可行性的拒绝理由不进 CloudWatch（五批 filter 全 0 命中），只落在 slot 文件的 `last_failure.detail.reasons` 里，所以「本轮是否降低了拒绝率」只能靠下一批 employment-vacancy 的实测对比，无法事后复盘 |
| 2026-08-09（夜 19:3x） | `layout-fidelity-20260809`（**仅 backend**；frontend 停在 `peer-layouts-20260809`） | **无（工作树 41 处未提交，HEAD `8060974`）** | — | **36** | 48（未动） | **题目阶段不再重新规划题组，只渲染蓝图结构。** 新增 `validate_layout_fidelity`（`validate_questions_part1.py`，在 `if items:` 守卫下与 `validate_blueprint_fidelity` 并列调用），三条硬检查：印刷组的 `layout` 必须等于其每个成员点的蓝图 `item_form`；一个蓝图 `form_group` 不得拆到多个印刷组；多个蓝图 `form_group` 不得并进一个印刷组。同轮把 Form/Note/Table 改为平级语义判定（`note` 不再是 fallback——出题 SKILL/rules、材料 SKILL/spec、材料审核 SKILL/rubric、可行性 SKILL/rubric 八处都删掉了「note 是 fallback / 默认」的措辞），题目审核规则新增第 16 项「伪 Form 或被压制的 Note」，第 13 项补入稀疏对角矩阵，可行性 SKILL 第 2 步新增在蓝图阶段就拒绝伪 Form / 伪 Table / 不自然组边界。**最强证据是拿真实生产包在三个镜像上跑同一条 CLI**：`20260809-employment-vacancy-191d4462`（Runtime 34 生成、盲审 0 findings、当时 PASS 交付）在 `form-table`（v34）下 **0 错**，在 `peer-layouts`（v35）下 **18 错**（全是 `—` 占位符），在本轮 `layout-fidelity`（v36）下 **20 错**——多出的两条正是本轮新增的：`Q10 is planned as 'table' ... but its printed group 'application_choice' declares layout 'form'` 与 `blueprint form_group 'Vacancy comparison' is split across printed groups ['application_choice', 'vacancy_comparison']`。这个包的蓝图是 2 个 `form_group`，题目阶段却印成了 3 组并把 Q10 从 table 降成 form——**旧三层规则全部放行，本轮才拦住**。同一批的合法包 `20260809-booking-festival-1f80a5a8`（蓝图 1 组 form / 印刷 1 组 form）在新校验下仍 `ok=True` 0 错，无误伤。另在镜像内构造九种判定：layout 一致→0；蓝图 note 印成 form→逐题报错；蓝图 table 印成 note→逐题报错；一组拆两组→split 错；两组并一组→merge 错；两组 1:1→0；纯 note 单组 Q1-Q10→0（平级地位）；`item_form` 缺失或不在 `LAYOUTS`（如 `diagram`）→0（不误伤）。材料侧确认 `TABLE_FORMS` 已删、`ITEM_FORMS={form,note,table}`、1/2/3 组放行而 4/5 组报「1-3 natural candidate-visible Form, Note, or Table groups」。**只部 Runtime**：`skills/` 下 12 个文件的 mtime 晚于 18:33 的 frontend 镜像推送时间，`frontend/src` 无更新——本轮是给生成加约束，前端渲染未变。门禁：Skill 校验器基线全过（含 `blueprint Note points silently reclassified as Form` 与 `blueprint group boundaries replanned by the question stage` 两条新用例）、backend+web 1063 项。Runtime READY，`containerUri` 已核为新镜像，`/healthz` 200 @ 0.95/0.73/0.66s，web 层确认未动（taskdef 48、running 1/1、单 deployment）。未运行实际生成测试（由人在前端验证） |
| 2026-08-09（夜 18:3x） | `peer-layouts-20260809`（backend + frontend 同 tag） | **无（工作树 39 处未提交，HEAD `8060974`）** | — | **35** | **48** | **Form/Note/Table 三者平级，题组限 1–3 组，稀疏对角表被硬拦。** 校验器新增占位符拒绝：`table_rows` 的普通 `{"text"}` 格若整格只是 `—`/`-`/`–`/`N/A`/`n/a`/`Not applicable`/`TBD`/`unknown`，即报 `placeholder-only text ... not padding for a sparse diagonal table`。这条直接针对 v34 那批放行的 7 列对角矩阵（28 格 = 6 空格 + 18 个 `—` + 4 行标签）。schema `groups.maxItems` 与 `instructions.maxItems` 10→**3**；材料侧删掉 `TABLE_FORMS`，`largest` 改按 `ITEM_FORMS`（三种布局）算，新增 `len(labels) > 3` 即报错。前端 `batchEstimate.ts` 的 `WAVE_SECONDS` 由 `[182, 230]` 改为 `[18*60, 40*60]`。**镜像内实跑 11 例**：真实对角表 3 个 `—` 全拦、七种占位符各 1 错、真实两列比较表与三列稠密表 0 错放行、`e-mail`/`co-op` 不误伤。门禁：Skill 基线全过、backend+web 1063、`tsc --noEmit` exit 0、vitest 467/27 文件、oxlint exit 0、`git diff --check` 干净。`services-stable` 2:30，bundle `index-C7mJWqfB.js`、`agentcore-D9zMZr3t.js` 均新（CSS `index-gv5RB1vA.css` 未变），`/assets/<name>` 均 200，旧 `index-bQyZ-yhU.js` 已 302。**注意 bundle 前缀是 `/assets/`，不是 `/static/assets/`**——后者对已存在的 bundle 也返回 302（登录跳转），与「不存在」无法区分，本轮踩过一次 |
| 2026-08-09（下午 16:5x） | `form-table-20260809`（**仅 backend**；frontend 停在 `multi-col-table-20260809`） | `8060974` | — | **34** | 47（未动） | **区分字段表单与比较表格。** 三层规则明确：Form 每行是一个独立字段及其值；Table 至少两个可比较的数据维度；两列表格仍合法但不能只是给 Form 加边框；不为通过规则强行制造第三列。属 Agent 语义判断，Python 侧无对应硬校验，材料生成与题目盲审各查一次。**验收（两批）**：`web-1786265886031-2` 6/6 / 2356s，table **0** 张——读完六套 form 内容确认都是纯字段/值登记表、无可比轴，所以归零正确而非过度纠正；但「真有可比轴的内容仍会被画成 table」这半条**未被考验**。`web-1786268877027-3` 6/6 / 944s 把这半条考出来了并且**失败**：`employment-vacancy-191d4462` 生成 7 列稀疏对角矩阵，盲审 0 findings / PASS，全部硬检查通过。根因是「至少两个可比较维度」被 ≥2 个列标题字面满足，没有要求多行可比观测——这直接导致下一轮（v35）加占位符硬拦、（v36）加 layout 保真 |
| 2026-08-09（下午 16:0x） | `multi-col-table-20260809`（backend + frontend 同 tag） | **`300e95d`** | — | **33** | **47** | **支持真正的多列表格题型。** 上一轮（v32）为了适配前端把表格**限死在一个内容列**，本轮改成让前端支持多列、后端给出精确坐标——**限制被取代而非叠加**。新契约是 `structure.table_rows[]`：按印刷顺序逐行给 `cells[]`，每格**恰好**是固定文字（`{"text": ...}`）或一个题号引用（`{"question_number": N}`），二者不能同时出现也不能都不出现。校验器要求**行矩形**（每行格数 = `len(column_labels)`）且 `table_rows` 引用的题号是该组成员的**精确、升序、不重复**覆盖。**契约方向变了，是最容易踩的一点**：`column_labels` 现在包含**全部**印刷表头（含最左的行标签列），`row_header_label` 与 `row_labels` 降级为旧包只读——所以格数要和"含行标签列"的列数对齐，不是和内容列数对齐。新表格缺 `table_rows` 即报错（legacy 只读）。`has_structural_context()` 同步改为按 `table_rows` 里的题号引用判定，不再靠 `row_labels` 的下标位置；`validate_leakage` 新增扫描固定单元格文字，避免把答案直接印在格子里。**镜像内实跑确认八种判定**：3 列且一行内两个空格→放行（新能力）；1 个内容列→仍放行（旧形态未被废）；行长度不一致→报「must be rectangular」；题号非升序 / 同一题号出现两次→报「exactly once in ascending printed order」；一格同时含 text 和 question_number→报错；空的固定文字格→报错；`question_number` 为布尔值→报错（`isinstance(True, int)` 为真，这条是刻意防的）。**两层都必须部**：前端 `QuestionLayouts.tsx`（+55/-7）与 `contracts/questions.ts`（+22/-2）改了渲染，后端改了 schema/validator/规则，只部一层会让新契约的包渲染不出来或旧渲染收到新字段。门禁：Skill 校验器基线全过（新增用例含 `a genuine three-column row may contain several question blanks`、`a table whose question cells are out of order`、`an answer printed in a fixed table cell`）、backend+web 1063 项、`tsc --noEmit` exit 0、vitest **465** 项 / 27 文件、oxlint exit 0。`services-stable` 2:30，`/healthz` 200 @ 0.90/0.72/0.65s，`ALLOWED_EMAIL_DOMAINS=amazon.com,example.com` 与 `IELTS_AUDIO_BUCKET` 均在。前端 bundle 两个变（`index-bQyZ-yhU.js`、`agentcore-DTalbct_.js`；CSS `index-gv5RB1vA.css` 未变），CloudFront 均 200，旧 `index-B6AqQTiP.js` 已 302。未运行实际生成测试（由人在前端验证）。**上一轮（v32）的验收结论一并记此**：`web-1786258994626-2`（6 套）证明单一题组偏好**确实改变了行为**——6 套里 **3 套出现跨窗口组**（v31 基线是 0/9，21 个组边界无一例外落在 Q5/Q6），但仍有 3 套边界全落在中点，其中 `custom-6cf6e9b3-c2eee8dd` 的 `reservation`/`meal` 同为 form 却在 Q5/Q6 切开——除旁白中点外无理由，说明偏好生效但未彻底 |
| 2026-08-09（午 15:0x） | `single-group-20260809`（**仅 backend**；frontend 停在 `group-windows-20260809`） | **`583ecbe`** | — | **32** | 46（未动） | **题组生成偏好改成「先试一个自然题组」+ 禁止前端表达不出的多内容列表格。** 上一轮（v31）只是**放开**了跨窗口约束，实测发现放开不等于会用：改动后第一批 `web-1786256231029-1`（9 套 / 1250s / 9-9 交付）**21 个组全部单窗口，边界无一例外精确落在 Q5/Q6**，连相邻两组都是 form 的两套（`a712feab`、`8ebaae35`）也在中点硬切——模型仍把旁白当版面边界。所以规则文本从「组数不预设」改成「**组数不预设也不偏好**：先测 Q1-Q10 能否合成一个自然的 Form/Note/Table，能就用跨两窗口的单组；不能才在**可见记录结构真正改变**处切。一/二/三组都是合法结果，没有配额或目标分布，中点提示本身永远不构成拆分理由」。第二件是**表格列结构收严**：`question_package` 没有题号到单元格的映射，所以一张表只能表达「一列行标签 + 恰好一列内容」；此前 schema 允许多个 `column_labels`，模型生成两个内容列时前端渲染不出对应关系。`validate_layout_structure` 新增 `len(columns) != 1` 即报错，出题规则 / SKILL / 审核规则同轮改齐（四处都在镜像内确认到位）。**只部 Runtime**：`git diff --numstat 29a5de2..583ecbe -- frontend/` 为 0 个文件，前端渲染逻辑没动——这次是**给生成加约束以适配现有前端**，不是改前端去适配生成。**镜像内实跑确认五种表格判定**：1 个内容列→放行；2 个内容列（`Requirement`+`Notes`）→报「no question-to-cell mapping」；2 列且都是真实比较轴（`Regular lesson`+`First lesson`）→同样报错（这条是刻意的，规则限制来自 schema 表达能力而非语义质量）；完全没有列标签→仍报旧的 `column_labels` 错；缺 `row_header_label`→上一轮的规则仍生效未被削弱。门禁：Skill 校验器基线全过（含两条新用例 `a table with one content column passes`、`validator catches: a table with two content columns but no cell mapping`，以及上一轮的 `one natural note group may cover Q5-Q7 across the narrator midpoint`）、backend+web 1063 项。Runtime READY，`/healthz` 200 @ 0.92/0.65/0.70s，web 层确认未被触碰（taskdef 46、running 1/1、单 deployment，线上 bundle 仍是 `index-B6AqQTiP.js` / `agentcore-B_tDHizk.js`）。未运行实际生成测试（由人在前端验证）。**待观察**：单一题组偏好是**规则文本**而非校验器检查，镜像内只能确认文本到位，真实效果必须看下一批的组形态——判据是跨窗口组出现率从 0/21 上升，以及不再出现「相邻两组同 layout 却在 Q5/Q6 切开」 |
| 2026-08-09（午 14:1x） | `group-windows-20260809`（backend + frontend 同 tag） | **`29a5de2`** | — | **31** | **46** | **页面题组与旁白证据窗口解耦。** 旁白窗口原本被当成两重边界：既约束每道题的证据落在哪一段，又强制一个印出来的 form/note/table 不许跨过中点提示。后者是过度约束——真实试卷里一张连续的表格完全可以跨过「now look at questions 6-10」，而旁白是听与读的节奏提示，不是版面切割线。改动后：**每道题证据的窗口仍然严格**（`window_of(number)`、declared turn 落点、blueprint item 三方一致，AL-017/SC-019 无容差），但**组不再因跨窗口被拒**。`narrator_window_id` 从 group 的 `required` 里移除，降级为「整组落在单一窗口时的兼容字段」——跨窗口的组必须**省略**它，因为没有哪个标量值是真的。QR-026 的 signpost 计数改为按组实际覆盖的窗口逐个要求，一条不能同时算给两个窗口。材料侧 `validate_part1.py` 同步删掉约束 5（`form_group` 跨窗口即错），非连续编号（约束 3）与证据序连续性（约束 4）保留。`question_metrics._groups` 新增权威字段 `windows`（组实际占据的窗口列表），旧的标量 `window` 保留给旧消费者。前端 `questionPreview` / `QuestionPreviewPanel` 按 `windows` 渲染跨窗口题组。**镜像内实跑确认九种判定**：跨窗口+省略→放行；跨窗口+仍声明→报「必须省略」；单窗口+声明正确→放行；单窗口+声明错窗口→仍报错（旧检查未被削弱）；单窗口+省略→放行；schema 的 `group.required` 已不含 `narrator_window_id`；材料侧跨窗口 form_group→0 错、非连续编号→仍 1 错；`question_metrics` 输出 `windows` 字段。**这是 8/8 以来第一次工作树干净的发布**（只有一个未跟踪的 `material/` 目录），所以 `29a5de2` 真能复原镜像。门禁：backend+web 1063 项、Skill 校验器基线全过、`tsc --noEmit` exit 0、vitest 462 项 / 27 文件、oxlint exit 0。前端三个 bundle 有两个变（`index-B6AqQTiP.js`、`agentcore-B_tDHizk.js`；`index-gv5RB1vA.css` 未变，因为本轮没动样式），CloudFront 均 200，旧 `index-CEW6rYx4.js` 已 302。`services-stable` 2:47，`/healthz` 200 @ 0.91/0.70/0.62s，`ALLOWED_EMAIL_DOMAINS=amazon.com,example.com` 与 `IELTS_AUDIO_BUCKET` 均在。未运行实际生成测试（由人在前端验证）。**基线对照**：部署前用旧镜像（v30）跑完的 `web-1786247321688-1`（9 套）9/9 交付、1634s，每套都是 2 个组且各自锁在单一窗口——正是本轮要解耦的形态，可作为改动前后的对比样本 |
| 2026-08-09（午 12:1x） | `comments-20260809`（**仅 frontend**；backend 停在 `carrier-parens-20260809`） | `f3c8bc9` | — | 30（未动） | **45** | **单人批注功能上线，只部 web 层。** 新增批注的增删、严重程度（critical/major/minor）、题目侧栏、Turn 折叠面板、定位跳转，并落 S3 持久化。后端新增 `web/comment_store.py`（`CommentService` + `S3CommentStore`/`InMemoryCommentStore`，last-write-wins，一材料一份 JSON 存在 `_comments/<material_id>.json`）与三条路由 `GET/POST /api/material-comments/{material_id}`、`DELETE /api/material-comments/{material_id}/{comment_id}`。**Runtime 刻意未动**（改动全在 web 层，出题/材料生成逻辑零变更），所以这是台账里第一次两层不同版，回退方式见「当前生产版本」的第一条 ⚠️。**IAM 无需改**：task role `ielts-part1-web-task` 的内联策略已按 `arn:aws:s3:::ielts-part1-materials-<aws-account-id>/*` 授 `s3:GetObject`/`s3:PutObject`，覆盖新的 `_comments/` 前缀。三条新路由自动受 `ApiAuthMiddleware` 会话保护（`PUBLIC_API_PATHS` 未加它们）。**验证要点**：taskdef 45 带着 `IELTS_AUDIO_BUCKET`——`build_comment_store()` 在这个变量为空时会静默退回 `InMemoryCommentStore`，那样批注会随任务重启消失，所以这个变量是本轮的关键配置而非可选项；`ALLOWED_EMAIL_DOMAINS=amazon.com,example.com` 已显式带上。路由存在性用**镜像内枚举 `app.routes`** 确认（三条路径与方法齐全），不能靠线上探针推断：`ApiAuthMiddleware` 在路由之前跑，任何未认证的 `/api/*` 都返回 401，连不存在的路径也是 401。另外**带 body 的 GET 会被 CloudFront 挡成 403 HTML**，不是应用行为——干净的 GET 返回应用自己的 JSON 401。前端三个 bundle 哈希全变（`index-CEW6rYx4.js`、`agentcore-B36tGOgS.js`、`index-gv5RB1vA.css`，均 CloudFront 200；旧 `index-DCdFAgHO.js` 已 302），这与「本轮真有前端代码改动」一致。门禁：web 313 项、backend+web 1062 项、`tsc --noEmit`、vitest 461 项 / 27 文件、oxlint 全过；`services-stable` 2:38，`/healthz` 200 @ 0.95/0.64/0.70s；`_comments/` 部署前后均为 0 对象（等人工产生第一条批注） |
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
