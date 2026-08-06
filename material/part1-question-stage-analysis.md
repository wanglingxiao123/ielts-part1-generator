# Part 1 题目生成与题目审核 —— 需求分析与方案对齐

> 初版：2026-08-05　第二轮：2026-08-06（12 处修正）　**第三轮：2026-08-06（5 项定案 + 时限事实修正）**
> 分支：`feat/listening-full-test`
> 状态：**仅分析，未修改任何代码文件**。正式实施前还需完成 §6.4 问题 12 的长 invoke 实测并建立 Trellis 任务。
> 依据：`material/listening/` 下 7 份客户规则（structural / question / answer / alignment /
> language / script / severity）+ 现有 `skills/` 与 `backend/` 代码实读。
>
> **本文档是本需求的唯一事实来源。** `material/part1-blueprint-modification-direction.md`
> 只保留为便于其他模型快速阅读的索引，不再承载独立定案；如两者表述不一致，以本文档为准。
>
> **第二轮修订说明**：初版有 9 处与客户规则或第一次同步存在偏差，另有 3 处属于不该提前写死的设计。
> 已全部修正，修正点在正文中标注 `[修订]`。
>
> **第三轮修订说明**：客户定案 preflight 三出口、题型与题组层次、精确数量交付语义、题目循环与择优四项，
> 并修正了「AgentCore 单次 invocation 15 分钟硬上限」这一**全仓库范围的错误假设**。
> 第三轮内容标注 `[第三轮]`，新增 §7（时限审计）、§8（交付与题目循环）、§9（文件总览/测试/风险）。
> 其中**两处此前的结论被推翻**：「达上限交付一套带 findings 的题目」这个兜底出口已取消（§8.2）；
> 「交付保障与 `batch.py` 现有 refill 同源」的判断是错的，`batch.py` 恰恰是反向设计（§8.1）。
> 修正与定案清单见 §0。

---

# 0. 修正清单（2026-08-06 第二轮同步）

## 0.1 与客户规则或第一次同步存在偏差（9 处）

| # | 初版的错误 | 修正 | 落在本文档 |
|---|---|---|---|
| 1 | 把 `DETAIL_TYPES` 里的 `option` 当作选择题的兄弟项一并删除 | **`option` 必须保留。** 它是**信息点类别**，表示偏好、方案或选择结果（如某人最终选择的服务类型），这类信息完全可以成为 Form/Note/Table 的答案。删除的只有 `item_form` 与 `question_type_coverage` 里的 `multiple_choice` | §2.1, §2.3 |
| 2 | 写成「每个 narrator window 对应一个题目结构」「整套 1–3 个结构」 | **两者都不是客户规则。** SC-019/QR-022/AL-017 只规定题组不跨 window、每题证据在所属 window 内、无拆分时可自由分组。**一个 window 内可以有多个连续题组**，题组总数不预设 | §1.3, §2.2(a), §5.1, §6.3 问题 2 |
| 3 | 写成题目阶段「从 blueprint 里选十个点并按 QR-027 预算重排」 | **必须用全部 10 个点，保持编号与证据顺序，不得删除、替换或重排。** 某点不适合出题 → 出题前检查发现 → 退回材料生成/重生成该 slot | §1.4, §5.1, §5.4 |
| 4 | 把 AR-003 写成「canonical 必须等于 evidence 中的一个 token」 | **分档：**一词答案查完整 token 同形；多词答案查组成答案的完整词/短语确实来自决定性证据且满足词数限制；**不得要求多词答案等于一个 token**；连字符词按 AR-014 保持完整 | §5.3 检查 5 |
| 5 | 「材料阶段 warning、题目阶段一律 error」 | QR-027 **明确允许 Script 缺替代证据时保留并记录理由**。改为 **feasibility preflight**：blueprint 增加真实的 `response_form`/`answer_category`（**按实际 target 判定，不能凭 `type` 推断**）→ 材料完成后、正式出题前预检 → 无合理例外且明显不满足则**阻止进入题目生成并重生成该 slot** → 有规则允许的理由则继续但记录。**同时必须保证「请求多少套就交付多少套」** | §2.2(b), §5.4 |
| 6 | 把 `ANCHOR_TOLERANCE=1` 原样移植 | **分开判断**：答案文本、事实命题、narrator window 严格匹配；quote 必须真实存在于声明的 turn；**仅当审核方锚在相邻确认轮次且仍支持同一答案与同一事实命题时才允许 ±1**。不得仅凭 turn 相邻就判通过 | §4.4 |
| 7 | 断言「agents.py 无需修改」 | **代码核验前不能下此结论。** 仍需检查 Skill 加载、同池 description 路由、生成/盲审/修订/复评的调用编排、prompts、`agent_steps`、输入隔离与结果解析。最终可能不需新增 Agent，但这是待验证项 | §2.2(f) |
| 8 | 默认所有题目指标都跑远程 Code Interpreter | **纯确定性、无副作用的 JSON/文本检查跑 Runtime 本地 Python。** 只有确实依赖 Agent 工具环境或隔离边界的才放远程。**盲审的关键是控制传给审核 Agent 的输入，不是脚本跑在哪** | §4.2, §4.5, §6.1 |
| 9 | QR-015 整体推迟到 DOCX 阶段 | **结构化阶段即可查**：题号/指令/blank 是否存在、顺序与层级是否完整、必需信息是否遗漏或不可靠解析。字体/边框/颜色/分页/最终视觉可访问性等 DOCX 接入后再做，单独计入 `visual_qc_status` | §3.1, §3.2, §3.5, §5.3 检查 15 |

## 0.2 不应提前写死的设计（3 处）

| # | 不预设什么 | 正确表述 | 落在本文档 |
|---|---|---|---|
| A | 一套只能用一种结构 | 客户只限定允许 Form/Note/Table，**未规定一套只能用其中一种**。允许按 Script 结构混用，但**每个题组自身必须是明确、同质的结构** | §6.3 问题 2 |
| B | 题组数量、题号分配、一个 window 内几个题组 | **全部不预设固定数量**，由 Script 证据结构与 narrator 窗口决定 | §2.2(a), §6.1 阶段 2 |
| C | 修订触发与择优直接照搬材料审核 | 严重度与 `question_qc_status` 遵循 severity.md；但**修订触发条件、交叉检查门槛、原题/修订题择优算法需单独定义**，不能照搬材料侧的分数比较 | §6.1 阶段 9, §6.3 问题 5 |

## 0.3 明确保持不变

**blueprint 继续保持恰好 10 个信息点，不扩成 12–14 个候选池。** 客户给出的四条理由都是代码级事实，已逐一核实：`validate_part1.py`:233 强制 `len(items) != 10` 报错；盲审 Skill 重建 10 个信息点；`cross_check.py` 把每个 blueprint 点视为必须恢复的计划点；前端合同、旁注、分布图与测试都假设恰好 10。扩池会同时改变盲审语义、cross-check、前端合同和修订逻辑。

增强方向是这 10 个点自身的出题可行性信息：答案形式、微型类别、窗口归属、连续分组、证据支持。

## 0.4 `[第三轮同步 2026-08-06]` 已定案的决定

上一轮列为「待确认」的五个问题已由客户定案，另有一处平台事实修正。**以下为决定，不再是建议。**

| 定案 | 内容 | 取代了 | 落在本文档 |
|---|---|---|---|
| **preflight 三出口** | `PASS` / `PASS_WITH_JUSTIFICATION` / `REGENERATE_MATERIAL`。**`REGENERATE_MATERIAL` 不是最终交付失败，只是内部 slot 需要补位** | 我用的 `PASS_WITH_RATIONALE` / `BLOCK` 命名，以及「BLOCK = 一次 refill」的含糊表述 | §5.4 |
| **题型与题组** | **顶层题型统一为 completion**，`layout ∈ {form, note, table}`。可只用一种也可混用两三种；组内 layout 一致；题号连续，且 ordered evidence points 中不插入其他 group；组不跨 narrator window；一个 window 可有多个 group | §6.3 问题 2 的「允许混用」建议；新增的是「顶层 completion / layout 三选」这一层次划分 | §1.3, §5.5, §6.3 |
| **交付量语义** | 达到重试上限 → 放弃该 candidate、**创建 replacement slot**；只有收集到 N 套合格结果才能标记成功；**不得把少于 N 套标记为成功**；**不设「总尝试耗尽后少量交付」的正常出口**；接近 Runtime 生命周期上限时**存 checkpoint 由下次 invoke 继续**；基础设施永久故障时保持任务未完成或报告系统故障，**不得伪装成完整交付** | 我在 §5.4/§6.3 问题 9 写的「达上限交付一套带 findings 的题目」——**该出口被明确取消** | §8 |
| **题目循环触发与择优** | validator error / 盲审 CRITICAL·MAJOR / 答案无法重建·不唯一·不一致 / 证据·window·命题不一致 → 修题；仅 MINOR·INFO → 原则保留原题。交叉检查**严格门槛：10/10 独立重建 + 答案·证据·顺序·window·命题全过**，任一关键不一致即非 clean。择优字典序：**硬校验通过 → 交叉检查通过题数 → CRITICAL/MAJOR 数 → MINOR 数 → 其他指标**；硬校验失败者不得因综合分高而胜出 | §6.3 问题 5 的「三项待定义」 | §8.3 |
| **Runtime 时限事实修正** | **AgentCore 没有「单次 invocation 15 分钟硬上限」。** 官方语义只有 `idleRuntimeSessionTimeout=900`（空闲回收 microVM）与 `maxLifetime=28800`（单 microVM 8h），且 Session 可在新 microVM 上继续。`deploy/runtime.sh` 的配置正确；900s 是**项目自设**的业务/网络限制 | 本文档 §6.2 的旧拆分理由，以及全仓库 40+ 处把 900s 当作平台硬限的叙述（`README.md`:975、`batch.py`:3–8/119–134、`web/fanout.py`:98–99 等） | §7 |

## 0.5 `[第三轮]` 已合并的 Blueprint 定案

以下三项来自 Blueprint 专项讨论，现已合并到本文档并成为本文档的一部分。独立的
`material/part1-blueprint-modification-direction.md` 只保留阅读索引，不再单独承载定案：

| # | 定案 | 本文档原结论 | 处置 |
|---|---|---|---|
| 1 | item **新增** `response_form`、`answer_category`、`narrator_window_id` 三字段，且 Python 校验「`response_form` 与实际 target 一致」 | 我建议「只用 Python 推导、本轮不加字段」 | **采纳定案**，改为「声明 + 推导复核」。§5.4 已加 `[定案覆盖]` 标注。这个方案更好，且解掉了我反对加字段的主要理由 |
| 2 | `question_type_coverage` 建议改名 `completion_layout_coverage`；`item_form` 语义明确为 `completion_layout` | 本文档只提到删 `multiple_choice` 键 | **采纳**，见 §5.5 末尾。建议与 MC 删除合并做一次 |
| 3 | `option` 可保留，**也可改名 `choice` / `preference`** | 我只写了「必须保留」 | §2.3 补充：保留是底线，改名是可选优化 |

**同时确认**：「十个 evidence 位置严格递增」在 `validate_part1.py`:291 **已实现且已是 error**，不需新增。

**取值域已对齐**：持久字段 `response_form` 使用 `numeric | word | phrase` 描述作答形态；
Python 内部使用 `qr027_class = numeric | lexical | mixed` 描述字符构成并服务于 QR-027 计数。
两者是不同维度，不合并。见 §6.4 问题 14。

---

# 1. 需求与范围复述

## 1.1 本阶段做什么

只做 **IELTS Listening Part 1 的「题目生成」+「题目审核」**两件事。不做 Part 2–4，不接前端，不接 Runtime action。当前产出是分析与方案，不动代码。

## 1.2 客户新要求（覆盖旧要求）

Part 1 只生成三种题型：**Form completion / Note completion / Table completion**。**不再生成 multiple choice**。现有 Skill、blueprint Schema、validator 中与 multiple choice 相关的类型和「至少两个 multiple choice 信息点」约束，后续需要删除。

这条改动的性质要说清楚：它不是「少一种题型」，而是**把 SC-015 从"应以 form/note/table 为主"收紧为"只允许"**。现有代码把 multiple choice 写成了**硬门槛**（`MIN_CHOICE_ITEMS = 2`，缺了直接 error），所以删除动作是「拆掉一条会误伤的 error」，而不是「放宽一条建议」。

## 1.3 Part 1 题目硬约束（可校验形式）

| # | 约束 | 可确定性校验 | 规则依据 |
|---|---|---|---|
| 1 | 每套 10 题，题号与录音证据顺序一致 | 是 | SC-003/006, QR-004, AL-003 |
| 2 | 十题构成自然的 Form/Note/Table，不是十个割裂句子 | 部分（结构可查，"自然"要 Agent） | SC-015, QR-026 |
| 3 | 题组不得跨 narrator 题号窗口；每题决定性证据落在所属 window 内 | 是 | SC-019, QR-022, AL-017 |
| 3b | `[第三轮]` 组内 layout 一致；组内题号连续，且对应 ordered evidence points 中不得插入其他 group 的考点；一个 window 可含多个 group | 是 | SC-019, QR-022（见 §5.5） |
| 4 | blank 覆盖前/中/末，句末 blank 原则上 ≤7/10 | 是 | QR-026 |
| 5 | 纯数字/时间/金额答案默认 ≤4/10；有客户规则允许的明确理由时可例外 | 是（默认门槛，可例外） | QR-027 |
| 6 | 默认 ≥4 题需要写出有意义的词/短语（≥2 字母，单字母和通用缩写不计）；有明确理由时可例外 | 是（默认门槛，可例外） | QR-027 |
| 7 | 默认不得用三题或以上反复测试同一微型信息类别；有明确理由时可例外 | 是（默认门槛，可例外） | QR-027 |
| 8 | Note completion 需简短、具体、不泄露答案的场景标题 | 半（有无可查，泄不泄露要 Agent） | QR-031 |
| 9 | 每题答案唯一、字数限制明确、有直接 Script 证据 | 半（限制可查，唯一性要 Agent） | AR-002/012/013, AL-002, SR-005 |
| 10 | 整个题组不得通过标题、signpost、相邻题、语法结构泄露答案 | 半（原词/屈折可查，语义近直述要 Agent） | SC-012, QR-040, AL-014 |

### `[修订]` 不属于硬约束的三件事

初版把下面三条当成了规则，它们**不是**：

| 曾误写为约束 | 实际情况 |
|---|---|
| 「每个 narrator window 对应一个题目结构」 | 规则只禁止题组**跨** window。**一个 window 内可以有多个连续题组**，只要每组完整落在该 window 内 |
| 「整套共 1–3 个结构」 | 题组数量、题号分配、每个 window 内的题组数**都不预设固定值**，由 Script 证据结构决定 |
| 「一套只能用一种结构」 | 客户只限定允许 Form/Note/Table 三种，**未规定一套只用一种**。允许按 Script 结构混用；约束落在**每个题组自身必须是明确、同质的结构** |

无 narrator 拆分时，可依据证据结构自由设定题组边界（QR-022 原文）。第三轮进一步明确了题型与 layout 的层次关系，见 §5.5。

## 1.4 `[修订]` blueprint 的 10 个信息点是给定输入，不是候选池

这是初版最实质的一处偏差，单列一节。

现有 blueprint **已经固定包含恰好 10 个、按证据顺序排列、覆盖题号 1–10 的信息点**。题目阶段的正确姿态是：

- **原则上使用全部 10 个点**；
- **保持编号与证据顺序**；
- **不得自行删除、替换或重排**。

初版写的「从 blueprint items 里选，按 QR-027 预算重排」是错的——它把 blueprint 当成了候选池。真实约束是：**blueprint 已经定好了考什么和考的顺序，题目阶段只决定怎么把它们组织成 Form/Note/Table 并写出 carrier、answer key 和 evidence。**

**某个点不适合可靠出题时的正确出口**：在**出题前检查**（§5.4 feasibility preflight）中发现，**退回材料生成或重新生成对应 slot**——而不是在题目阶段另选十个点，也不是改 Script（SR-021）。

**blueprint 不扩成候选池。** 不改成 12–14 个点让题目阶段挑选。增强方向是这 10 个点自身的出题可行性信息（见 §2.3）。

## 1.5 关键定义与硬约束

- **answer key = 标准答案/评分键**，包含每题 canonical answer、accepted alternatives、作答限制。
- **evidence 证明答案正确，但不是考生看到的答案内容**。所以 evidence 与 answer key 都必须与题面**物理分离**（见 §4）。
- **Polly 只是 TTS**，读不懂规则。内容合规必须在合成前由 Agent + Python 保证；Polly 只收最终文本、voice、SSML。LG-012 / SR-014 / SR-018 转化为发音、拼读、停顿、SSML 处理；SR-022 属于音频生成后的听音审核。
- **题目修订只能改题目，不能改 Script。** 这条在客户规则里有直接依据：**SR-021** 规定可听 Script 只能在明确授权后修订，且必须新建 DOCX、保留 diff 与 SHA-256、把既有音频标 `AUDIO_REBUILD_REQUIRED`。因此「Script 支持不了 10 道可靠题目」的唯一合规出口是**明确退回材料阶段**，而不是让题目 Agent 顺手改一句台词。
- **审核独立性**：审核 Agent 应尽量从 Script + 题面重建答案与证据，再由 Python 与生成方的 answer key / evidence 交叉检查，不直接相信生成 Agent 的自评。
- `[修订]` **交付量不可打折**：请求 N 套就必须交付 N 套。§5.4 的 preflight 可以判定某个 slot 不可出题并触发重生成，但**重生成不得导致少交付或返回空结果**，也不得靠降低硬性质量门槛来凑数。`[第三轮修正]` 此前这里写的「与现有 `batch.py` 的 refill 机制同源」**是错的**：`MaterialResult.refill_rounds` 只统计被丢弃的 NOT_ASSESSABLE 次数，而 `batch.py`:42–43 / 102–103 的设计恰恰**反向**——「少返回一套优于丢掉全部」。这是需要被推翻的既有决定，不是可以复用的基础。详见 §8.1。
- `[第三轮]` **不存在「少量交付」的正常出口。** 单 slot 的尝试上限只决定何时放弃当前 candidate、建 replacement slot，不决定 batch 是否可以少交付。基础设施永久故障时，任务保持未完成或报系统故障，**不得标记为成功**。见 §8.2。
- `[第三轮]` **AgentCore 没有「单次 invocation 15 分钟硬上限」。** 只有 `idleRuntimeSessionTimeout=900`（空闲回收 microVM）和 `maxLifetime=28800`（单 microVM 8h），且 Session 可在新 microVM 上继续。仓库里所有把 900s 说成平台同步上限的叙述都是错的，见 §7。

## 1.6 调整后的整体流程

```
创建持久 slot
  → 材料生成 → Python 材料校验 → 独立材料审核 → 必要时修改复评
  → 保存 material_done checkpoint
  → feasibility preflight（Python 结构检查 + 材料审核/可行性语义判断）
      ├─ PASS / PASS_WITH_JUSTIFICATION → 题目生成
      └─ REGENERATE_MATERIAL → replacement candidate / slot refill
  → Python 题目校验 → 独立题目审核 → Python 答案与证据交叉检查
  → 必要时修题复评
  → slot complete
  → 收集满请求数量 N 后交付材料/题目/答案/审核报告
```

前半段（到「确定最终材料」）**已实现并在线上运行**（`backend/orchestration/loop.py`）。本阶段新增的是后半段。

---

# 2. 现有 Skill / Schema / validator 的受影响点

## 2.1 multiple choice 删除面（已逐处核实）

### 生成侧

| 文件 | 位置 | 内容 | 处置 |
|---|---|---|---|
| `scripts/validate_part1.py` | :26 | `ITEM_FORMS = {"form","table","multiple_choice","note"}` | 删 `multiple_choice` |
| | :35 | `MIN_CHOICE_ITEMS = 2` | 整个常量删除 |
| | :148–151 | `if form == "multiple_choice": choice_count += 1` | 删分支 |
| | :183 | `errors.append(f"blueprint needs at least {MIN_CHOICE_ITEMS} multiple_choice items...")` | 删检查 |
| | :133–137 | `validate_grouping` docstring 提「multiple-choice items」 | 重写 |
| | :28 `DETAIL_TYPES` 含 `"option"` | — | **`[修订]` 不删。见 §2.3** |
| `schemas/blueprint.schema.json` | :41 | `question_type_coverage.multiple_choice` | 删属性 |
| | :171 | `item_form` enum 含 `multiple_choice` | 删枚举值 |
| | item `type` enum 含 `"option"` | — | **`[修订]` 不删。见 §2.3** |
| | item `distractor` | 语义含 option trap | 只需重述**干扰机制**的表述为 completion 场景（自我修正/否定/限定条件）。注意 `specification.md`:114 把「option comparison」列为合法干扰机制之一——**这个机制本身在 completion 里依然成立**（对话中比较两个方案后选定一个），不因删除选择题而失效 |
| `SKILL.md` | :41 | "mark at least 2 points `multiple_choice`" | 删该子句 |
| `references/specification.md` | :93,97,99 | 「Question-type support」整节 | 重写 |
| | :199 | 示例 JSON 的 `coverage` | 去掉 `multiple_choice` 行 |
| | :257 | 自检清单「at least 2 points are `multiple_choice`」 | 改写 |

### 审核侧（材料审核 Skill，非新增的题目审核）

| 文件 | 位置 | 内容 |
|---|---|---|
| `skills/audit/audit-listening-part1/SKILL.md` | :86 | "mutually exclusive options support multiple choice" — 删该判据 |
| `references/audit-rubric.md` | :46 | Minor 项「no mutually exclusive options able to support multiple choice」— 删 |

### 测试与前端

- `skills/shared/tests/build_fixtures.py`:127–128 —— **`[第三轮补]` 必须最先改的一处**，因为它是所有 blueprint fixture 的**唯一生成源**，手改 fixture JSON 会被下次 build 覆盖：
  ```python
  ("option", "park",  "he'd love a park nearby",   "multiple_choice", None, False, False),
  ("option", "house", "always lived in a house",   "multiple_choice", None, True,  False),
  ```
  注意这两行的 `type` 是 `option`——**改 `item_form` 而保留 `type="option"`**，正是 §2.3 的活例子。
- `skills/shared/tests/run_tests.py`:464–480 的 `all_choice` / `mixed` 两个用例需整体重写（不是改数字）；:469,472 + `fixtures/blueprint_valid.json`、`blueprint_bad_grouping.json` 等随 build 脚本同步。
- `backend/docs/sample/blueprint.json`:17, 30, 138 —— `[第三轮补]` 示例文档里的 MC 痕迹，含 `question_type_coverage`。文档 sample 不影响运行，但它是新人和模型读到的第一份样例，留着等于埋回归。
- 前端：`src/contracts/blueprint.ts`:25,74；`src/domain/types.ts`:88(`◉`),95(`'多选'`)；`src/domain/validationNotes.ts`:129（正则 `/blueprint needs at least \d+ multiple_choice items/`，规则删掉后这条注解永远不触发，属死代码）；`src/domain/domain.test.ts`:540,541,550,559；`src/mocks/fixtures/generated.ts`（6 处）。

**`[第四轮补·核实]` 上面这份清单漏了三处，实测补齐（2026-08-06 逐处 grep 核实）：**

- **`src/domain/formGroups.ts`:61 + :64 + :173** —— 漏得最要紧的一处。`:61` 的接口字段
  `multipleChoiceCount: number`、`:64` 的 `FORMS: ItemForm[] = ['form','table','multiple_choice','note']`、
  `:173` 的 `items.filter(i => i.item_form === 'multiple_choice').length` 计算。其中只有 `:64`
  会被 `tsc` 报错（union 收窄后数组字面量非法），`:61`/`:173` **留着不报错**——字段和计算都还自洽，
  只是永远算出 0。漏改的后果不是编译失败，而是 UI 永远显示「出不了，可选择的点只有 0 个」。
- **`src/features/material-reader/QuestionTypePanel.tsx`:88–102** —— 消费 `multipleChoiceCount`
  的整块「选择题可否出」UI（`>= 2 ? 可以出 : 出不了`）。同样不报错，只会永远显示否定结论。
- **`backend/docs/sample/events-batch3.jsonl`** —— 两条 slot 记录的 `question_type_coverage`
  与 `item_form` 都含 MC。

**关于 `events-batch3.jsonl` 与 `real-batch.sse.txt` 的处置修订：不改数据，只加说明。**
`backend/docs/handover.md`:6 明确声明这些样例是「**real output from a live run, not
hand-written**」；`frontend/src/api/agentcore.test.ts`:264–267 也写明「The capture is left
untouched — it is a record of a real response, not a fixture to edit」。把 MC 从实况抓包里
手抹掉，等于伪造一份「实况记录」——这比留下过时样例更糟，因为它销毁了「当时的真实行为」这个
唯一有价值的信息。正确做法是在 `handover.md` 注明这些样例采集于 MC 尚存的版本、读时以当前
schema 为准。`backend/docs/sample/blueprint.json` 同属实况样例，处置一致。

## 2.2 删除之外的连带影响（这部分比删除更要紧）

**(a) `[修订]` `MIN_GROUPED_ITEMS` 的语义变了，但不能改成固定结构数。** 现在的规则是「至少一个同质 form/table 组含 3+ 点」，因为其余点可以是 multiple_choice。删掉选择题后**十题全部是 completion**，这条门槛不再够用——validator 会容忍「一组 3 点 + 7 个散点」，恰好是 QR-026 要禁的形态。

初版据此写了「十题落在 1–3 个结构内、每结构对应一个 window」，**这是加了客户没有的约束**。正确的替代门槛是**关系式的，不是计数式的**：

- 每个题组内部 `structure` 必须同质（一组不能混 form 和 table）；
- 每个题组必须**完整落在单一 narrator window 内**（SC-019/QR-022）；
- 十题必须**全部归属某个题组**，不存在游离点；
- 题组内的题号必须**连续**；
- **不限制题组总数，也不限制一个 window 内的题组数。**

这组约束能挡住「7 个散点」（散点无题组归属即失败），又不预设任何数量。

**(b) `[修订]` QR-027 可行性要前移，但形式是 preflight，不是「材料 warning + 题目 error」。**

初版的判断——「可行性必须前移」——方向对，但落法错了两处：

**错误一：不能凭 `type` 推断答案形式。** 有代码级证据：`validate_part1.py`:30 的 `NUMERIC_TYPES` 包含 `address`，而 `SPELLED_TYPES` 只有 `name`。可是 `address` 类型的 target 往常是 `42 Oakwood Lane`——**这是拼写型答案，却会被算成纯数值**。反过来，`condition` 或 `option` 类型的 target 也可能是一个纯数字。所以：

> **必须在 blueprint 中增加真实的 `response_form`（numeric / word / phrase）与 `answer_category` 字段，按实际 `target` 判定并由生成方显式声明，不能由 `type` 推断。** `type` 描述的是「这是哪类信息」，`response_form` 描述的是「考生要写什么形式」，两者不是同一维度。

**错误二：题目阶段不能一律 error。** QR-027 原文明确允许——「Script 确实缺少替代证据时可保留，但必须记录阻断或保留理由」。一律 error 会把规则显式许可的例外判成失败。

正确设计见 §5.4：**材料生成完成后、正式出题前的 feasibility preflight**。

**(c) narrator 窗口目前只被当作"split 校验"，没被当作结构边界。** `validate_part1.py`:465 用 `FIRST_RANGE_RE`/`SECOND_RANGE_RE` 解析 narrator 的题号范围，只用来校验 `blueprint.split_after` 一致。SC-019/QR-022/AL-017 要求它是**不可破坏的结构边界**：题组不得跨窗口、每题决定性证据和标签必须落在本窗口内。窗口解析逻辑可复用，但要提升为题目阶段的一等约束，并从 blueprint 传递到 question package（新增 `narrator_window_id`）。

**(d) `[修订]` `cross_check.py` 不能直接复用，容差也不能原样移植。** 它比对的是 `blueprint.items` ↔ `audit.blind_information_map`（信息点是否可复原）。题目阶段要比对的是**审核方独立重建的答案** ↔ **生成方的 answer key + evidence**，语义不同，所以要新建 `cross_check_questions.py` 而不是改这个文件。

可移植的是它的**方法论**：多段式配对、强信号优先、以及「为什么需要容差」的推理。**不可移植的是 `ANCHOR_TOLERANCE=1` 的无条件用法**——材料侧只需判断「这个信息点能否被复原」，题目侧要判断「这个答案是否正确且证据是否指向对的地方」，后者对错位的容忍度低得多。分维度的正确判法见 §4.4。

**(e) 盲审隔离要扩一层。** `backend/deterministic/guards.py` 现在防的是 `BLUEPRINT_ONLY_KEYS` 泄漏。题目阶段要防的是 **answer_key / evidence 泄漏给题目审核 Agent**。需要新增 `ANSWER_ONLY_KEYS`（`answer_key`, `canonical`, `alternatives`, `evidence`, `paraphrase_relation`, `carrier_entity` …）并让 `assert_blind()` 覆盖。

**(f) `[修订]` agents.py 大概率不必新增 Agent，但「无需修改」是待验证项，不是结论。**

初版直接断言 `agents.py` 无需修改。理由（两个 Agent 各持一个技能池、自行按 name+description 选技能）是成立的，但这只说明**不必新增第三个 Agent**，不等于这个文件不用改。核验前**不能声明它一定无需修改**。

必须核验的清单：

| 待验证 | 为什么可能出问题 |
|---|---|
| 新 Skill 是否会被正确加载 | `_load_pool()` 从磁盘加载整池；需确认新目录被包含、`ReadOnlySkillSandbox` 的根路径覆盖到它 |
| 同池 Skill 的 `description` 能否避免路由错误 | **风险最高的一项**。`audit-listening-part1` 与 `audit-questions-part1` 都以「审核 Part 1 + 读 Script」开头，模型可能选错。同理生成池的两个 Skill |
| 生成 / 盲审 / 修订 / 复评的调用编排 | `agent_steps.py` 现有 `generate` / `audit_blind` / `revise` 三个函数是**材料专用**的（签名带 `scenario`、`blueprint`）。题目阶段需要新函数，可能连带调整 `_invoke` 与 `_envelope` |
| prompts 与输入隔离 | `build_audit_payload` / `build_audit_message` 固定为 material + metrics 形状，题目侧需要新的构造函数 + 新的冻结输入类 |
| 结果解析 | `_audit_envelope` 按材料审核 schema 解析，题目审核 schema 不同 |

结论改为：**「预期不需要新增 Agent；`agents.py` 本身可能只需极小改动或不改，但这要在实际加载新 Skill 后确认。」** 这项核验在 §6.1 里单列为阶段 10，不并入其他阶段——它的产出是一个结论，不是一段代码。

**(g) `paths.py` 需要新解析器。** `_script_in_pool()` 按文件名跨池查找且**明确拒绝歧义**（两个同名脚本直接抛错），所以新脚本必须起唯一名：`validate_questions_part1.py`、`question_metrics.py`。

## 2.3 `[修订]` `option` 必须保留

初版把 `DETAIL_TYPES` 里的 `option` 当作 `multiple_choice` 的兄弟项一并删除。这是错的，且有直接的代码证据。

`specification.md`:75 在「可记录的细节类型」清单里写的是：

```
- option or preference.
```

它从一开始就是**信息点类别**，指偏好、方案或选择结果——比如某人最终选定的服务类型、配送方式、房型。这类信息**完全可以成为 Form / Note / Table completion 的答案**（"Service chosen: ______"）。它与选择题题型（`item_form: multiple_choice`）是两个不同维度上的东西：

| 维度 | 字段 | 含义 | 本次处置 |
|---|---|---|---|
| 信息点**是什么类别** | `items[].type` | name / number / address / price / datetime / quantity / condition / **option** | **全部保留** |
| 信息点**能支持什么题型** | `items[].item_form` | form / table / **multiple_choice** / note | **删 `multiple_choice`** |

删错的代价是实质的：`option` 是唯一能表达「对话中比较了几个方案后选定一个」的类别，而这正是 `specification.md`:114 列为合法干扰机制的 **option comparison**。删掉它，材料生成会失去一整类自然的 Part 1 信息点（选服务、选时段、选套餐），而这类点在真实 Part 1 里很常见。

**可选的后续动作（非必需）**：若担心 `option` 一名容易与选择题混淆，可**重命名为 `choice` 或 `preference`**。保留是底线、改名是可选优化。但这属于命名清晰度改进，需先确认语义再动，且要同步 `DETAIL_TYPES`、schema enum、spec:228 的类型清单和前端映射。**保守做法是保留 `option` 不动**，只在 spec 里把「option or preference」这行写得更醒目，说明它与已删除的选择题无关。

**连带确认**：`type` enum 保持 8 个值，「At least four distinct types required」不受影响。

---

# 3. Part 1 题目阶段的 Rule ID 分类

筛选口径：Part 1 专属 + Part 1–4 通用；排除 Part 2–4 专属；排除 multiple choice / matching / plan-map-diagram 题型专项；DOCX / 真实音频 / 视觉排版类只在能力接入后启用。

## 3.1 适用（题目阶段直接校验）

| 命名空间 | Rule ID |
|---|---|
| **SC** | SC-003, SC-004, SC-005, SC-006, **SC-007（收窄为 form/note/table completion）**, SC-008, SC-009, **SC-012**, SC-013, **SC-015**, **SC-019** |
| **QR** | QR-001（收窄）, QR-002, QR-003, QR-004, QR-009, QR-010, QR-011, QR-014, **QR-015（内容可访问性部分——`[修订]`，见 §3.5）**, QR-016, QR-017, QR-018, QR-019, **QR-022**, QR-024（仅 completion 段）, **QR-026**, **QR-027**, **QR-031**, QR-034, QR-037, **QR-040**, **QR-043** |
| **AR** | AR-001, AR-002, **AR-003**, AR-004, AR-005, AR-006, AR-007, AR-008, AR-009, **AR-012**, AR-013, **AR-014**, AR-015, AR-016, AR-017 + **词数计算基线** |
| **AL** | AL-001, AL-002, AL-003, AL-004, AL-006, AL-008, AL-009, AL-010, AL-011, AL-012, **AL-014**, AL-015, AL-016, **AL-017**, **AL-018** |
| **LG** | LG-001, LG-002, LG-003, LG-004, LG-005, **LG-006**, LG-007, LG-008, LG-009, LG-010, LG-011, LG-013, LG-014, **LG-015** |
| **SR**（题目阶段只读不改） | SR-005, SR-006, SR-007, SR-009, SR-015, SR-016, SR-017 |
| **severity** | 全部：严重度枚举、§3.2 `question_qc_status` 算法、finding 状态 `open/resolved/waived/not_applicable`（**不得自行标 waived**）、`visual_findings` 与内容 findings 分离 |

## 3.2 条件适用（依赖尚未接入的能力）

| Rule ID | 触发条件 |
|---|---|
| SC-011 | 提供时间戳后 |
| SC-020 | 接入 DOCX comments 后 |
| QR-015 | **`[修订]` 拆两半。**内容可访问性部分**现在即适用**（见 §3.1 与 §3.5）；纯视觉部分（字体、边框、颜色、间距、分页、最终视觉可访问性）待 DOCX/渲染接入，且只影响 `visual_qc_status` |
| QR-020 | 仅当决定生成示例题 |
| QR-035 | 接入 Question DOCX 后 |
| AL-007 | timestamp 部分待时间戳；turn reference 部分**现在即适用** |
| **LG-012** | 双轨：题目阶段判「拼读是否在 Script 中自然澄清」；**发音/拼读实现转 Polly SSML** |
| **SR-014** | 同上——文本可自然朗读，缩写/符号/URL 需明确读法 → **转 Polly 读法与 SSML** |
| **SR-018** | 同上——拼读/重复/自我修正自然且最终信息明确 → **转 Polly 停顿与 SSML** |
| SR-019 | 属材料/narrator 措辞层；题目阶段只在题面 instruction 引用 Section 时触发 |
| SR-020 | 答案承载事实过度重复——题目阶段选点时可用（避免选被过度提示的点），完整判定需音频 |
| **SR-021** | **现在即生效**，作为「题目修订不得改 Script」的规则依据 |
| **SR-022** | 音频生成后的听音审核；缺音频时标 `deferred` + reason/owner/target version |

## 3.3 排除

| 类别 | Rule ID | 理由 |
|---|---|---|
| 完整测试专属 | SC-001, SC-002, SC-014 | 单 Part 片段。**必须记 `coverage.unreviewed`，不得伪造 finding** |
| 资产依赖 | SC-010 | 无图 |
| Part 2/3/4 专属 | SC-016, SC-017, SC-018 | — |
| Part 4 专属 | QR-023, QR-025, QR-038, QR-044 | — |
| 选择题专属 | QR-006, QR-007, QR-013, QR-021, QR-028, QR-029, QR-030, QR-032, QR-036, QR-039 | 题型已不生成 |
| Matching 专属 | QR-012, QR-033, QR-041, QR-042 | — |
| 图示专属 | QR-005, AL-013 | — |
| 干扰项专属 | AL-005, SR-008 | completion 无 option |
| 选择/匹配 key | AR-010, AR-011, AR-018 | — |
| 材料阶段已负责 | SR-001, SR-002, SR-003, SR-004, SR-010, SR-011, SR-012, SR-013 | 已由现有材料审核覆盖，题目阶段不重复判 |

## 3.4 一个需要留意的边界：QR-008 不能整条排除

QR-008 的主体针对选项和 matching（`compatible_options_without_audio` 等），按口径应排除。但它开头一句——**「不得通过词形、单复数、数量或语法搭配…在不听录音的情况下暴露答案」**——对 completion 直接适用：一个 carrier 如果语法上只能填一个词，就是无音频可解。

处置建议：**不整条排除，而是把 QR-008 的 morphology / cardinality / grammar 泄露维度并入 QR-040 的 group-scope leakage audit 执行**，report 里标注 `QR-008(partial, completion scope)`，选项与 matching 部分标 `not_applicable`。这样既不丢检查，也不伪造一条不适用规则的 finding。

## 3.5 `[修订]` QR-015 不整体推迟到 DOCX 阶段

初版把 QR-015 整条列为「接入 DOCX/渲染后」。这漏掉了它的前半段——规则原文的核验要点是：**「先确认 reviewer 能读取并区分全部必需内容；再把不影响内容读取的视觉偏差单独记录，不得延迟首轮内容 review。」**

「必需内容是否齐备且可靠解析」这件事，**在结构化输出阶段就能查，而且必须现在查**——否则一份缺了 blank 标记或题号错序的 JSON 会一路走到 DOCX 才被发现。

| QR-015 维度 | 何时查 | 计入哪个状态 |
|---|---|---|
| 题号存在且唯一 | **现在**（结构化） | `question_qc_status` |
| 指令存在且覆盖每个题组 | **现在** | `question_qc_status` |
| blank 存在且每题恰好可定位 | **现在** | `question_qc_status` |
| 题号顺序与层级完整、无错序 | **现在** | `question_qc_status` |
| 必需信息是否遗漏或无法可靠解析（表格行列标签、note 层级） | **现在** | `question_qc_status` |
| 字体、边框、颜色、间距、非破坏性分页、视觉一致性 | DOCX/渲染接入后 | **仅 `visual_qc_status`** |
| 渲染是否隐藏/截断/错序必需内容 | DOCX/渲染接入后 | `visual_qc_status` **+** 同时把 `content_review_readiness` 设 `BLOCKED` |

这个拆分与 severity.md §3.1/§3.2 的分离状态模型一致：纯样式问题进 `visual_findings`、只驱动 `visual_qc_status`；一旦影响内容可访问性、答案边界或题号顺序，则另建 content finding 按实际严重度进 `question_qc_status`。

---

# 4. question package 与 question audit 的数据边界

## 4.1 question package（生成方完整输出，三块物理分离）

分离是**必须**的，不是整洁偏好：审核输入只能拿到第一块。放在同一个对象里，早晚会有人整个传过去。

```
{
  "question_package": {
    "reference": "Part 1",
    "test_package": "...",
    "material_id": "...",              // 指向已确定的最终材料

    // ── 块 A：题面（考生可见 + 可给审核）───────────────────
    "question_face": {
      "instructions": [{
        "group_id", "question_range",              // "1-5"
        "task_type",                               // form | note | table
        "instruction_text",                        // LG-006 用词一致
        "word_limit", "numeral_allowance"           // QR-017
      }],
      "groups": [{
        "group_id", "narrator_window_id",           // SC-019 归属
        "structure",                                // form | note | table
        "title",                                    // QR-031（note 必需）
        "signposts": [ ... ],                       // QR-026 每窗口 ≥1，无 blank
        "layout"                                    // 行/列/层级
      }],
      "questions": [{
        "number", "group_id",
        "carrier_before", "blank", "carrier_after",
        "blank_position",                           // initial | medial | final（QR-026）
        "answer_category",                          // QR-027 微类别
        "response_form"                             // numeric | word | phrase
      }]
    },

    // ── 块 B：answer key（绝不给审核）─────────────────────
    "answer_key": [{
      "number", "canonical", "alternatives": [...],
      "word_limit", "numeral_allowance",
      "counting_rule"                               // 词数基线要求报告所用规则
    }],

    // ── 块 C：evidence（绝不给审核）──────────────────────
    "evidence": [{
      "number", "turn_index", "quote",
      "narrator_window_id",                         // AL-017
      "paraphrase_relation",                        // exact | signpost | paraphrase（QR-024）
      "carrier_entity", "evidence_entity",
      "proposition_relation", "proposition_alignment_result"   // AL-018
    }]
  }
}
```

## 4.2 question audit 的输入边界（`BlindQuestionAuditInput`）

沿用现有 `BlindAuditInput` 的做法——**用一个 `__setattr__`/`__delattr__` 全部拒绝的冻结对象把边界写进类型，而不是靠调用方自觉**。

**给：**

1. `material` —— **完整 Script，含 narrator turns**。narrator 必须给，否则审核无法判 SC-019 窗口、AL-017 窗口归属。
2. `question_face` —— 上面的块 A，全部。
3. `question_metrics` —— Python 已算出的确定性度量（题数、题号、blank 位置分类、句末计数、纯数值计数、拼写型计数、窗口归属、微类别计数）。沿用现有做法：**审核 Agent 不自己数**——一个未经计算就断言的数字最容易出错，而这里计算已经做完了。`[修订]` 关于**这些指标在哪里运行**，见 §4.5：不是所有指标都需要跑远程。
4. `material.scenario` —— 建议给。它是一句场景概括，不含答案，能显著降低审核方误判语境的概率。

**不给（任一泄漏即审核失效）：**

- `answer_key`（含 canonical / alternatives）
- `evidence`（含 quote / turn_index / paraphrase_relation）
- `blueprint`（`items[].target` **就是答案**）
- 生成 Agent 的自评、validator 报告、修题指令

## 4.3 审核输出边界

```
{
  "reconstructed_answers": [{                 // ← 核心产物，独立重建
    "number", "answer", "turn_index", "quote",
    "confidence",                              // high | medium | low
    "competing_candidates": [...]              // AR-012：同层级候选，>1 即唯一性问题
  }],
  "per_question_findings": [{
    "number", "rule_id",                       // SC/QR/AR/AL/LG/SR 命名空间
    "severity", "evidence", "fix", "state"     // open|resolved|waived|not_applicable
  }],
  "group_findings": [...],                     // QR-026/031/040 组级
  "coverage": { "reviewed_question_ids": [...], "unreviewed": [...], "reason": "..." },
  "summary": { "counts": {...}, "visual_counts": {...} },
  "question_qc_status": "PASS|WARNING|FAIL",   // 按 severity.md §3.2 算
  "content_review_readiness": "READY_FOR_HUMAN_REVIEW|BLOCKED",
  "visual_qc_status": "NOT_RUN",
  "visual_findings": []
}
```

## 4.4 Python 交叉检查（`cross_check_questions.py`）

这是「不直接相信生成 Agent 自评」的落点。逐题比对，纯 Python，零 token，判决不会漂移：

| 情形 | 判定 |
|---|---|
| 审核重建答案 == answer_key.canonical（归一化后） | 该题答案可独立复原 ✓ |
| 审核**重建不出**任何答案 | **SR-005 / AL-002 失败** —— 决定性证据不可定位，必须修题 |
| 审核重建出**多个**同等成立候选 | **AR-012 失败** —— 答案不唯一 |
| 审核重建 ≠ canonical，但审核给出了确定证据 | **不自动判生成方错**：标记「存在第二个同等成立答案 / carrier 限定不足」，进入修题（QR-010 + AR-012） |
| 重建 turn_index 与 evidence.turn_index 差 > 容差 | **AL-002 / AL-007 失败** —— 证据指向邻近内容而非实际证据 |
| 重建证据顺序与题号顺序不单调 | **QR-004 / AL-003 失败** |
| 重建证据落在其他 narrator 窗口 | **AL-017 / SC-019 失败** |

### `[修订]` 容差必须分维度，不能整体用 ±1

初版写「容差沿用 `ANCHOR_TOLERANCE = 1`」，把材料侧的单一容差整体搬了过来。这是错的——但**机械删除容差同样错**，因为「首次给出答案、下一轮确认」在 Part 1 对话里是标准写法（`blueprint` 的 `confirmed` 字段就是为它存在的，且要求 ≥3 个点被确认）。审核方锚在确认轮而非首次提及，是**合理行为，不是缺陷**。

正确做法是按维度分开判，**只有一个维度允许 ±1**：

| 维度 | 容差 | 理由 |
|---|---|---|
| **答案文本**（重建答案 vs canonical，归一化后） | **严格匹配，无容差** | 答案对不对不存在"差一点" |
| **事实命题**（AL-018：主体/对象/地点/时间/关系是否同一命题） | **严格匹配，无容差** | 命题错位就是 AL-018 失败，与 turn 距离无关 |
| **narrator window 归属** | **严格匹配，无容差** | SC-019 称之为「不可破坏的结构边界」；跨窗口即 AL-017 失败 |
| **evidence quote 是否真实存在于声明的 turn** | **严格，无容差** | 复用 `anchor_ok`；quote 不在那个 turn 里就是 AL-007 失败 |
| **turn_index 位置**（重建锚点 vs 声明锚点） | **有条件 ±1** | 仅当满足下面全部三个前提 |

**±1 的三个前提（必须同时成立）**：

1. 审核方锚定的 turn 是该答案的**相邻确认轮次**（不只是位置相邻，而是内容上确实在确认同一事实）；
2. 该 turn **仍然支持同一答案**；
3. 该 turn **仍属于同一事实命题**且**同一 narrator window**。

> **不得仅凭 turn index 相邻就认为交叉检查通过。** 相邻只是必要条件；上面三条是充分条件的组成部分。两个位置相邻但支持不同答案的 turn，配对成功就是把一个真实缺陷判成了通过——而这类失败是静默的。

实现上意味着 `cross_check_questions.py` 的配对函数**不能像 `cross_check.py:158` 那样只用 `(anchor, type)` 做守卫**，而要把「同一答案 + 同一命题 + 同一窗口」作为 ±1 分支的前置条件。

## 4.5 `[修订]` 指标脚本跑在哪里：本地为默认，远程为例外

初版默认把题目指标全部放进 AgentCore Code Interpreter，理由是「沿用材料侧做法」。这误解了材料侧那个设计的动机。

`sandboxed_metrics.py` 的模块文档说得很清楚，它跑远程是因为一件具体的事：**审核 Agent 需要运行脚本，而唯一可用的工具 `strands_tools.shell` 直接调 `pty.fork()`、签名里没有 `agent` 参数，因此任何 sandbox 都约束不住它**——一个有 `shell` 的审核 Agent 可以直接 `cat` 生成方的文件。Code Interpreter 把黑名单问题变成白名单问题：远程环境初始为空，只有上传的东西存在。

**所以远程隔离要解决的是「Agent 有执行能力」这个问题，而不是「指标必须在别处算」。** 结论：

> **盲审的关键是严格控制传给审核 Agent 的输入，而不是指标脚本运行在哪里。**

分工：

| 步骤 | 跑在哪 | 理由 |
|---|---|---|
| `validate_questions_part1.py`（§5.3 全部确定性检查） | **Runtime 本地 Python** | 纯 JSON/文本检查，无副作用，由 Python 编排层调用而非 Agent 调用。Agent 根本不参与，没有隔离问题。本地还省掉 2.5s 会话建立 + 上传开销 |
| `question_metrics.py`（喂给审核 Agent 的度量） | **Runtime 本地 Python 即可** | 同样是纯计算，且**由编排层算完后放进审核输入**——Agent 收到的是结果，不是执行能力 |
| 需要 Agent 自己执行的步骤 | 远程 Code Interpreter | 目前题目阶段**没有**这类步骤。若将来出现（如让审核 Agent 自行验算），再放远程 |

真正保证盲审的是三件事，都与脚本位置无关：`BlindQuestionAuditInput` 冻结输入形状、审核 Agent 无 `shell`、`guards.assert_blind()` 拦截 `ANSWER_ONLY_KEYS`。

**附带好处**：本地跑意味着阶段 1 的 validator 可以完全离线用手写 fixture 做单元测试，不需要 AWS 凭证或远程会话——这也是阶段 1 风险评为「低」的原因。

---

# 5. 两个 Skill 的设计

## 5.1 `generate-questions-part1`（放入 `skills/generate/`）

**输入**：确定的最终 `material.json` + 其 `blueprint.json`（允许——它在生成侧，不跨边界）。

**工作流**：

1. **解析 narrator 窗口** → 得到 1..n 个不可破坏的题号窗口（无拆分时视为单一窗口，题组边界可依证据结构自由设定）。
2. `[修订]` **划分题组**：把 10 个点按**已有的题号顺序**切成若干连续题组，每组分配一个结构（Form / Note / Table）。约束是关系式的：
   - 每组内部结构**同质**；
   - 每组**完整落在单一 window 内**，不跨窗口、不合并窗口（SC-019 / QR-022）；
   - 组内题号**连续**，十题**全部有归属**，无游离点；
   - **题组数量不预设**——一个 window 内可以有多个连续题组；一套内允许 Form/Note/Table **混用**，只要每组自身同质。
3. `[修订]` **使用全部 10 个 blueprint 点，保持编号与证据顺序**。不删除、不替换、不重排（§1.4）。QR-027 的答案形式配比**已在 §5.4 的 preflight 中确认可行**——题目阶段不通过挑点来满足它，而是通过**为既定的点选择恰当的作答形式与 carrier 写法**来满足。若走到这一步才发现不可行，说明 preflight 漏判，应退回材料阶段（不改 Script，SR-021）。
4. **写 carrier + blank 位置预算**：前/中/末三类覆盖，句末 ≤7/10（QR-026）。
5. **写标题与 signpost**：Note 必须有简短具体标题（QR-031）；每窗口 ≥1 条具体、不含 blank、Script-grounded 的 signpost（QR-026）。
6. `[修订]` **写 answer key**（AR-003 分档，见 §5.3 检查 5）：一词答案与决定性证据中一个**完整 orthographic token 词形一致**；多词答案的**每个组成词/短语都来自决定性证据**且整体满足词数限制；两者都不得派生或同义替换。连字符复合词按 1 词但保留整 token，carrier 不得预填 `eco-`（AR-014）。声明 word_limit / numeral_allowance / counting_rule。
7. **写 evidence**：含 turn_index、quote、窗口归属、`paraphrase_relation`，以及 AL-018 要求的 `carrier_entity` / `evidence_entity` / `proposition_relation` / `proposition_alignment_result`。
8. **自跑 `validate_questions_part1.py`**，读输出修到无 error（沿用现有 Skill 的成熟做法：一次 `shell` 调用、引号 heredoc、绝对路径写全、`PARSE_OK` 检查，卡住就带着报告交付而不是死循环）。
9. **输出**：一个 JSON，三块分离（`question_face` / `answer_key` / `evidence`）。

## 5.2 `audit-questions-part1`（放入 `skills/audit/`）

**盲读要求（写进 SKILL.md 开头，语气比材料审核更强）**：只有 Script + 题面 + metrics。若 answer key、evidence 或 blueprint 以任何形式出现，**拒绝审核并报告泄漏**——因为这种失败是静默的：分数只是偏高，没有任何东西会标出来。

**工作流**：

1. 读 narrator 划窗口。
2. **逐题在 Script 里独立找决定性证据，写出自己认为的答案**（核心产物）。
3. **枚举同层级候选逐一代回 carrier**（AR-012）——只检查 canonical 能否填入不构成唯一性证明。
4. 代入后检查语法语义通顺（QR-009 / AL-015）。
5. **组级泄露审计（QR-040）**：用**自己重建的答案**扫全部考生可见文本（标题、signpost、其他 carrier、相邻题）的原词、大小写、常规屈折。注意这里有个结构性优势——审核方没有 canonical answer，只能用自己重建的，**这比拿着答案去扫更强**：它同时检验了「答案是否可从题面直接推出」。
6. 逐题出 SC / QR / AR / AL / LG / SR findings，每条含 rule_id、severity、evidence、最小具体 fix（"改善节奏"不算 fix，"把第 3 行 carrier 加上时间限定"才算）。
7. 按 severity.md §3.2 算 `question_qc_status`，写 `coverage`。

**输出 schema**：新建 `schemas/audit_questions.schema.json`。

## 5.3 确定性分工

### Python 判（`validate_questions_part1.py`）

| # | 检查 | 规则 |
|---|---|---|
| 1 | 题数 = 10；题号 1–10 唯一连续 | SC-003 |
| 2 | task_type ∈ {form, note, table}；**组内 structure 同质；组完整落在单一 window 内；组内题号连续；十题全部有题组归属。不校验题组总数，也不校验每 window 的题组数** `[修订]` | SC-007/015/019, QR-001, QR-022 |
| 3 | answer_key 每题存在、非空、非 TBD/空串/审稿注释 | AR-013 |
| 4 | word_limit 声明存在，且 canonical 满足（按词数基线：连字符 1 词整 token、数字计入允许数量、空格分词、斜杠不算一个答案、报告须写明所用计数规则） | AR-002, QR-017 |
| 5 | **`[修订]` AR-003 分档校验**（详见下方） | AR-003, AR-014 |
| 6 | evidence.turn_index 指向的 turn 文本确实含 quote（复用 `anchor_ok`） | AL-007 |
| 7 | evidence 顺序单调不倒退 | QR-004, AL-003 |
| 8 | narrator 窗口解析（复用 `FIRST_RANGE_RE`/`SECOND_RANGE_RE`）→ 每题证据落在本题号窗口内；题组不跨窗口 | SC-019, QR-022, AL-017 |
| 9 | blank 位置分类（沿用 QR-025 的实义词判据：前部 = blank 前 ≤1 实义词且后有内容；末尾 = blank 后无实义词且前有内容；其余为中部）→ 三类覆盖 + 句末 ≤7 | QR-026 |
| 10 | 纯数值答案 ≤4；拼写型（≥2 字母，排除单字母与通用缩写）≥4 | QR-027 |
| 11 | 同一 answer_category <3 | QR-027 |
| 12 | **明显答案泄露**：canonical 及常规屈折变化在题组全部考生可见文本中出现 | SC-012, QR-040, QR-008(partial) |
| 13 | note 组必须有 title；每窗口 ≥1 条无 blank signpost | QR-031, QR-026 |
| 14 | questions ↔ answer_key ↔ evidence 三向映射无孤儿；`coverage.reviewed_question_ids` 覆盖全集 | AL-001, AL-010 |
| 15 | `[修订]` **内容可访问性**：题号存在唯一、每题组有指令、每题 blank 恰好可定位、层级与顺序完整、表格行列标签与 note 层级无缺失。**纯视觉项不在此列**（§3.5） | QR-015（内容部分） |
| 16 | `[修订]` 使用的 blueprint 点为全部 10 个，编号与证据顺序与 blueprint 一致（无删除/替换/重排） | §1.4 |

### `[修订]` 检查 5 展开：AR-003 必须分档，且不限于 ONE WORD ONLY

初版把这条写成「canonical 必须等于 evidence 中的一个 token」。两处偏差：

**偏差一：把它窄化成了只管 `ONE WORD ONLY`。** 客户原文是「要求**使用录音中的词**时，**尤其是** `ONE WORD ONLY` completion」——`ONE WORD ONLY` 是最严的场合，不是唯一场合。只要题目要求用录音原词，这条就适用。

**偏差二：多词答案不可能等于一个 token。** 若 word_limit 是 `NO MORE THAN TWO WORDS`，一个合法答案（如 `swimming pool`）本就是两个 token。要求它等于一个 token 会把全部合法多词答案判错。

正确的分档：

| 答案形态 | 校验方式 |
|---|---|
| **一词答案** | canonical 必须与决定性证据中**一个完整 orthographic token 词形一致**。token 化 + 边界匹配，**禁止子串命中**（`Educational` 不能作为 `education` 的依据，也不能反过来） |
| **多词答案** | 组成答案的**每个完整词/短语都必须来自决定性证据**，且**整体满足词数限制**。**不要求它等于单个 token** |
| **连字符复合词** | 按 AR-014：计 1 词，但必须**保留完整 token**。证据只有 `eco-tourism` 时，不得以 `tourism` 作答案，carrier 也不得预填 `eco-` 只让考生填后半段。只有同一决定性证据中另有独立出现的完整 standalone token 时，才可按该独立出现审查 |
| **共同约束** | 一律**不得派生、同义替换或拆分 token** |

实现提示：判断走哪一档要看 `response_form` 与实际 canonical 的分词结果，**不能看 word_limit 的声明**——声明 `NO MORE THAN TWO WORDS` 的题，答案完全可以只有一个词，这时应走一词档的严格 token 校验。

### 独立审核 Agent 判

| 维度 | 规则 |
|---|---|
| 语言自然度、搭配、register、词汇难度不必要升高 | LG-001/002/003/005/015 |
| 改述保真（主体/范围/极性/时态/因果/情态/答案粒度不变，措辞自然可定位） | AL-004, QR-024 |
| **答案唯一性**（枚举同层级候选逐一代入） | AR-012, QR-010 |
| **命题级对齐**（carrier 断言的主体/对象/地点/时间/关系与答案证据同属一个事实命题；Q 标签放在答案词那行不算对齐） | AL-018 |
| **语义泄露**（同一事实的近直述，不是原词命中） | QR-040 后半, SC-012 |
| 代入后语法语义通顺 | QR-009, AL-015 |
| blank 是否对应有意义的信息单位 | QR-010 |
| 拼写负担复核（词频只是人工 triage 信号，不得由自动阈值判错） | QR-043 |
| **十题是否真的构成一张 Form/Note/Table，而不是十个割裂句子** | SC-015, QR-026 |
| 题干单一解释、定位负担可控、不考转录顺序 | QR-003, QR-034, QR-037 |
| 决定性证据是否同等支持两个互斥答案；Script 是否 answer-signposting | SR-006, SR-007 |

## 5.4 `[修订]` 出题可行性预检（feasibility preflight）

替代初版的「材料阶段 warning + 题目阶段 error」。位置：**材料生成完成、材料审核通过之后，正式出题之前**。

### 为什么必须是独立的预检环节

三条理由，缺一不可：

1. **QR-027 允许例外。** 原文：「Script 确实缺少替代证据时可保留，但必须记录阻断或保留理由。」一律 error 会把规则显式许可的例外判成失败。
2. **题目阶段不能改 Script（SR-021），也不能另选点（§1.4）。** 所以不可行必须在**进入题目生成之前**判定，否则唯一出路是白跑一次完整题目生成。
3. **不能等题目全部生成后才第一次发现。** 这是初版设计的实际后果——先生成十道题，再由 validator 报「拼写型不足 4 题」，此时已经花掉一次题目生成的模型调用，而修法是回到材料阶段。

### `[第三轮]` 答案形式怎么判：**声明 + Python 推导复核**

> 定案是**两者都要**：blueprint **声明** `response_form`，Python **用推导独立复核**声明是否与实际 `target` 一致。
>
> 这个方案比我的「只推导、不加字段」更好，而且**恰好解掉了我当时反对加字段的第一条理由**——我担心加字段等于把
> 判断交回模型自评，但「声明 + 确定性复核」正是本系统对付自评的标准手法（与 `cross_check.py` 复核审核方重建
> 结果同构）：模型声明有价值（它知道自己想考什么），只是不能是**唯一**依据。
>
> 下面保留的推导规则**仍然全部有效**，只是角色从「唯一判据」变成「复核判据」。两处需要对齐：
> - **取值域**：定案用 `numeric | word | phrase`（按长度分词/短语），我推导出的是 `numeric | lexical | mixed`
>   （按字符构成分）。两者不是同一个维度——`"Room 4B"` 在定案口径下是 `word`，在我的口径下是 `mixed`。
>   **定案为双维度**：三值作为持久字段（服务于 word_limit 判断），推导结果作为**内部中间量**用于 QR-027 计数，
>   两者各司其职，不合并。见 §6.4 问题 14。
> - **QR-027 计数口径**：仍按下面的 `numeric` / `lexical+mixed` 计，不按字段的三值计。理由见下方计数块。

**核心前提不变：按实际 `target` 判定，不能凭 `type` 推断。** 代码级证据：`validate_part1.py`:30 的 `NUMERIC_TYPES` 含 `address`，而 `SPELLED_TYPES` 只有 `{"name"}`——`address` 的 target 往常是 `42 Oakwood Lane`，一个明显的拼写型答案被算成纯数值。反之 `condition`/`option` 的 target 也可能是纯数字。

第二轮我提议给 blueprint 加三个字段并由生成方声明。随后曾评估「只用 Python 从 `target` 推导、本轮不加字段」，
最终由“声明 + 复核”定案取代。以下三条理由中第 1 条已由定案解决，第 2、3 条降级为实施注意事项。

推导规则（确定性、可离线单测）：

```
去掉货币符号、单位与标点后按 token 切分：
  ├─ 全部 token 均为数字 / 时刻 / 日期格式        → numeric
  ├─ 含至少一个 ≥2 字母的词                      → lexical
  └─ 两者混合（如 "Room 4B"、"3 Oakwood Lane"）  → mixed

QR-027 计数口径：
  · 「纯数值 ≤4/10」只计 numeric
  · 「≥4 题有意义单词/短语」计 lexical + mixed
  · 单字母与通用缩写不计入拼写最低数（QR-027 明文）
```

选推导而非加字段的三条理由：

1. **`type` 已被证明不可用**（见上），而加字段等于把这个判断交回模型自评——这正是本系统一贯避免的（`loop.py` 的设计前提：「A model that can decide it has passed will eventually decide it has passed」）。
2. **加字段的代价是合同变更**：schema + 盲审语义 + 前端合同 + 全部 blueprint fixtures 需同步，属于客户点名的「风险过高」那一类。
3. **推导错了还能补救**：若将来在真实数据上出现歧义（`"Room 4B"` 算 numeric 还是 mixed），再加字段作为**模型对推导结果的复核标注**，代价比现在就加低。

**`answer_category` 与 `narrator_window_id` 同样定案为新增字段。** 曾有“不加”的评估（前者可由 `type` 近似，
后者可由 `validate_part1.py`:465 的 `FIRST_RANGE_RE`/`SECOND_RANGE_RE` 窗口解析 + item `number` 算出），
最终确认两者都需要进入 item 结构。

**这个改动方向是对的，但三个字段的可复核程度不同，实施上要分开看：**

- `answer_category` —— **声明有独立价值**。它比 `type` 细（`type` 只有 8 个值，QR-027 的「微型类别」要细得多，如 `location` / `price` / `service`），不能可靠地由 Python 从文本语义推导。Python 只能检查枚举、完整性和类别计数；类别语义是否准确必须由材料审核 Agent 复核。

  > `[2026-08-06 Stage 3A]` **「由材料审核 Agent 复核」这句与现有代码硬冲突，已定案改为独立的非盲可行性审核。**
  > 三条代码级证据：
  > 1. `answer_category` 在 `backend/deterministic/guards.py:72` 的 `BLUEPRINT_ONLY_KEYS` 中；
  > 2. 材料审核是盲审，`backend/steps/agent_steps.py:308` 在 payload 出网前调 `assert_blind(payload)`；
  > 3. `guards.py:112` 的 `assert_blind` **raise 而不 strip**（其 docstring 说明 strip 会
  >    「keep the batch running while quietly changing what was audited」）。
  >
  > 所以把 `answer_category` 送进材料审核不是「能跑但隔离变弱」，是**必然抛 `BlindnessViolation`**。
  > 定案：**新开一个非盲的可行性审核，盲审守卫一个字不动**——本节下方「预检逻辑」的职责边界段
  > 原本就写的是「材料审核**或专门的可行性审核**结果」，此处按后者执行。
  > 依据也是正面的：盲审看不到 blueprint 是**对的**（它的价值来自独立重建信息点，
  > `cross_check.py` 的全部意义建立在这上面），而可行性审核必须同时看 Script 和 blueprint，
  > 两者需求相反，本就不该复用同一个 Agent。
  > 该 Agent 本体属于 **Stage 3B**（`.trellis/tasks/08-06-stage3b-feasibility-agent`）。
- `narrator_window_id` —— **可以完全推导，所以声明的作用是交叉校验**，不是数据来源。Python 必须用 §465 的窗口解析独立算一遍并比对；若只存不算，等于把 SC-019 的窗口归属交给模型自评。这一点在实现时容易做错成「读字段就算过」。

`[定案覆盖]` 因此**本轮 blueprint item 新增三个字段**：`response_form`、`answer_category`、`narrator_window_id`，全部**必填**。其中 `response_form` 由 Python 复核形态、`narrator_window_id` 由 Python 独立解析窗口后复核，`answer_category` 由 Python 做结构/计数检查并由材料审核 Agent 复核语义。这把 §9.1 的 schema 合同变更从「一处」（`form_group` 必填）扩为「四处」，风险等级相应上调——见 §9.3 风险 1。

### 预检逻辑（`[第三轮]` 三出口已定案）

```
输入：确定的 material + blueprint（十点）
      Python 结构/统计结果 + 材料审核与 blueprint 可行性语义结论

  ├─ 纯数值 ≤4 且 拼写型（lexical+mixed）≥4 且 同一 answer_category <3
  │     → PASS
  │       材料与 blueprint 可以正常进入题目生成
  │
  ├─ 不满足，但只存在 QR-027 明确允许的例外，且记录了具体原因
  │   （Script 确实缺少替代证据，QR-027 明文许可）
  │     → PASS_WITH_JUSTIFICATION
  │       可以继续出题；理由写入交付报告，题目审核阶段复核该理由是否成立
  │
  └─ 结构检查失败，或语义审核判断无法基于这 10 个点生成可靠、唯一、自然的题目
        → REGENERATE_MATERIAL
          终止该 slot 的题目阶段，重新生成材料
```

**`REGENERATE_MATERIAL` 不是最终交付失败**，它只是内部 slot 需要补位。交付语义见 §8。

**职责边界**：`question_feasibility_preflight.py` 是判决聚合器，不是只靠正则和计数判断自然度的万能 validator。
Python 负责字段、数量、顺序、window、分组和 QR-027 统计；“能否形成自然记录结构”“答案事实是否足以支持唯一题目”
等语义结论来自材料审核或专门的可行性审核结果。两者共同决定三出口。

命名变更记录：第二轮我写的是 `PASS_WITH_RATIONALE` / `BLOCK`，第三轮客户定名为 `PASS_WITH_JUSTIFICATION` / `REGENERATE_MATERIAL`。后者不只是改名——`BLOCK` 读起来像终态，`REGENERATE_MATERIAL` 明确指出了下一步动作和它的归属层（slot 补位，不是 batch 失败）。

> `[2026-08-06 Stage 3A]` **三出口之外还有三个「判不了」状态，它们不是新出口。**
> 客户定名的三个出口表示「已判决」；下面三个表示「判不了」，混进出口就等于让客户的判决语义
> 承载它没定义的情形。已实现并有测试钉住：
>
> | 状态 | 何时 | 为什么不能用 `REGENERATE_MATERIAL` 兜底 |
> |---|---|---|
> | `SEMANTICS_MISSING` | 语义结论缺失、形状不合或审核本身报错 | Stage 3B 完成前这是常态，兜底会让每套材料都重生成，链路根本跑不通 |
> | `VALIDATION_INCOMPLETE` | `metrics` 形状不对、版本判不出、`qr027_*` 缺失或类型错 | **系统缺陷伪装成材料缺陷**：白烧一次生成，真正的故障被掩盖 |
> | `UNSUPPORTED_VERSION` | **明确读到**一个不等于 2 的版本号（如 v1 归档记录） | 把「这是历史记录」报成「这份材料要重生成」 |
>
> 关键在于 `REGENERATE_MATERIAL` 是一个**有代价的断言**：它说「这套材料内容不合格」，
> 于是消耗一次阶段 4 的外层配额（candidate 更换）并重跑一次完整材料生成。
> 反向的错误同样要避免——把这三种当 `PASS`：那样语义层就从「共同决定」退化成「只能否决」，
> 而它未接入时**永远**不否决。依据 §8.2(5)「不得把未完成伪装成完整交付」：
> **确定性通过 ≠ 可以出题**，只是「还没判完」。
>
> 另有一条实测事实值得记录：**版本闸门没有第二道兜底。** 一份 v1 记录用 `--allow-v1` 读出来是
> `ok: true`、零 error、零 `qr027_*` 键——于是确定性闸门放行、完整性闸门无话可说、
> QR-027 闸门读不到数。若版本闸门不在最前，它会一路走到底。
> 同理「版本无法识别」与「版本不受支持」是两件事：`validate_part1.py:466` 在读不出版本时显式写
> `metrics["blueprint_schema_version"] = None`，表达的是「我没能判定版本」。把 `None` 当
> `UNSUPPORTED_VERSION`，会把一份**写坏了版本号的新记录**报成「这是历史归档记录」，
> 于是没人去修那个坏版本号。

> `[2026-08-06 Stage 3A 第二轮]` **判决前还要复核输入的取值，不只是形状。**
> 第一轮只校验「键在不在、类型对不对」，实测发现三类**形状全对但取值不可能**的输入
> 全部被放行到 `PASS`。这不是防御性编程，三条各有上游依据：
>
> | 复核 | 上游依据（先实测再实现） | 不查的后果（实测） |
> |---|---|---|
> | `ok` 与 `errors` 的类型 + 一致性 | `validate_part1.py:638` 写的是 `"ok": not errors`，两者一个派生另一个，互检零成本 | `ok: false` + `errors: []` → **PASS**；`errors` 为字符串/`None`/缺失 → **PASS**。即一份传输中被清空了 `errors` 的载荷会被报成「可以出题」 |
> | 三个计数在 `0..10`，且 `numeric + spelled == 10` | 一份 blueprint 恰十个 item（`:504`）；`derive_qr027_class` 对任何 target 都归入 numeric/mixed/lexical 三类之一（探针实测九种刁钻取值），三类**完全划分**十个 item，而 `spelled = lexical + mixed` | `numeric: -1` → **PASS**；和不为 10 时仍拿这两个数去比门槛，等于拿量错的数做算术 |
> | 否决时 `reasons` 至少一条非空字符串 | 材料阶段本应依据 reasons 行动 | `feasible: false` + `reasons: []`/`[""]` → `REGENERATE_MATERIAL`，花掉一次外层配额却没说要修什么 |
>
> 第三条的状态选择值得单记：一次**无理由的否决**，与一个**崩掉后把输出默认成 false 的审核**，
> 从外部完全无法区分——而后者恰恰最不该消耗一次重生成。所以它是 `SEMANTICS_MISSING`
> （结论不可用），不是「细节不足的 `REGENERATE_MATERIAL`」。
>
> 和不变量还带来一个必须如实记下的后果：**`spelled >= 4` 无法被单独触发。**
> `spelled < 4` 要求 `numeric > 6`，而那已越过 `numeric <= 4`。所以 QR-027 的 spelled 规则
> 只能在它真正可达的地方取证（`numeric 6 / spelled 4` 对 `numeric 7 / spelled 3`）。
> 这是不变量的真实后果，不是覆盖缺口——构造一个「spelled 3 + numeric 1」去测，
> 测的是 validator 永远产不出的输入。

## 5.5 `[第三轮]` 题型与 layout 的层次划分（已定案）

这一节取代了此前把 form/note/table 当作三种「题型」的说法。正确的层次是两层：

```
question_type = completion          ← 顶层，Part 1 只有这一种
    └─ layout ∈ { form, note, table }   ← 版式，一套内可只用一种，也可混用两三种
```

区分这两层不是术语洁癖，它直接决定 schema 形状：`instruction` 与 `word_limit` 属于 completion 这一层（对三种 layout 都一样），而 `title` / 表格行列标签 / note 层级属于 layout 这一层。放错层会导致给 table 组生成 note 的标题结构。

**题组（group）的五条约束**——全部可确定性校验：

| # | 约束 | 与第二轮的差异 |
|---|---|---|
| 1 | 每个 item 必属于一个 group | 更严：不再允许 `form_group: null` |
| 2 | 组内 `layout` 一致 | 不变（`validate_part1.py`:158–163 已有同质检查） |
| 3 | 组的**题号连续** | 不变 |
| 4 | 组内 item 在十个 ordered evidence points 中连续，不得插入其他 group 的考点 | 明确“连续”是考点序列连续，不是 turn 距离硬阈值 |
| 5 | 组不跨 narrator window；一个 window 可含多个 group | 不变 |
| — | 所有 group 完整覆盖 Q1–Q10 | 不变（`question_type_coverage` 已有等价检查） |

**约束 4 不需要 turn-span 阈值。** 题号连续、全局 evidence 严格递增、group 不跨 window 后，
“ordered evidence points 中不插入其他 group”可以确定性判断。`MAX_GROUP_SPAN = 14` 没有客户规则依据，
继续保留为 advisory warning；题组是否因跨度过大而显得不自然，由材料审核 Agent 判断，不升级为 Python error。

**`form_group` 字段名保留、语义扩展、nullable → 必填非空。** 保名是为减少合同改动（客户明确允许）；但改必填是一处真实的合同变更：`frontend/src/contracts/blueprint.ts`:74 与 `validate_part1.py`:152–156 都要改，且**现有 `_candidates/` 里存在 `form_group: null` 的历史记录**，旧数据会读失败。按既定原则应走新 key 前缀发布。

另外定了两处命名，都属于「改名或至少改语义」：

- **`item_form` → 明确表示 `completion_layout`。** 允许暂时保留旧字段名以减少迁移成本，但文档语义必须写清它是**版式**、不是 IELTS 顶层题型。这与上面的两层划分是同一件事。
- **`question_type_coverage` → 建议改名 `completion_layout_coverage`。** 不改名则文档语义必须改为 layout coverage。**倾向改名**：删掉 `multiple_choice` 之后，这个字段的所有键都只剩三种 layout，留着 `question_type` 这个名字会持续误导——顶层题型只有 `completion` 一种，「coverage」在那一层没有意义。改名的代价与 MC 删除完全重合（同一批 schema/fixture/前端合同文件都要动），**合并做一次比分两次便宜**。

`[定案补充]` §9 还要求校验「十个 evidence 的位置严格递增」——这条**代码里已经有了**，`validate_part1.py`:291 `"item evidence must occur in strictly increasing, distinct dialogue turns"`，且已是 error。**不需要新增，只需在文档里确认它满足该要求。** 注意它与新增的约束 4（组内证据连续）不重复：全局严格递增管的是十点整体顺序，组内连续管的是同组点是否聚集——前者已有，后者是新的。

---

# 6. 分阶段实施方案与待确认问题

## 6.1 分阶段

| 阶段 | 内容 | 可独立验证 | 风险 |
|---|---|---|---|
| **0** | **时限叙述修正 + read timeout / hard limit 方案定案**（§7），含一次长 invoke 实测 | 一次 >900s 的 invoke 能正常返回 | 中。**必须最先做**：后面每一步的时间预算都建立在它上面 |
| **1** | **删 multiple choice**：validator 5 处 + schema 3 处 + SKILL 1 处 + spec 5 处 + 审核侧 2 处 + 测试 fixtures + 前端 6 文件。**`option` 不动**（§2.3） | `run_tests.py` + 一次真实材料生成 | 低。纯收窄，且删的是一条 error |
| **2** | **Blueprint v2 合同与题组关系化**（§5.5）：`form_group` 必填；新增 `response_form` / `answer_category` / `narrator_window_id`；`MIN_GROUPED_ITEMS` 改为完整覆盖、组内同质、考点序列连续、不跨 window；`MAX_GROUP_SPAN` 保持 warning | v1/v2 兼容单测 + v2 fixtures | 中。新生成严格写 v2，历史 v1 兼容读取 |
| **3A** | `[2026-08-06 已完成]` **可行性预检聚合器 `question_feasibility_preflight.py`**（§5.4）：组合 Python 结构/统计结果与语义可行性结论；输出 `PASS` / `PASS_WITH_JUSTIFICATION` / `REGENERATE_MATERIAL`，另有三个「判不了」状态（见 §5.4 的 2026-08-06 补注）。语义结论本阶段只定契约与注入点 | 离线单测（+249 checks，九个套件）+ 二十轮变异测试（每轮均致死） | 中。确定性部分可离线测，语义结论不能伪装成纯 Python 推导 |
| **3B** | `[2026-08-06 新增]` **非盲可行性审核 Agent + Skill + Schema + 编排接入**：真实产出 `feasibility` 语义结论。盲审守卫（`assert_blind` / `BLUEPRINT_ONLY_KEYS`）一字不动，不复用 `build_audit_payload` | 真实材料端到端跑通，且材料盲审行为逐字不变 | 中偏高。主要风险是这个 Agent 属于哪个 skill 池——放 `audit` 池会破坏「audit 池不含 plan schema」并直接撞上 ci_gates gate 1 |
| **4** | **槽位持久化 `_slots/` + 两级尝试上限 + checkpoint**（§8.1–8.2）。这一步决定「必须交付 N 套」能不能成立 | 单测：耗尽上限 → 建 replacement slot；杀掉进程 → 下次 invoke 从 checkpoint 续 | **高。本方案最实质的结构改动**，且与 `batch.py` 现有三处「少交付优于 504」直接冲突 |
| **5** | `question_package.schema.json` + `validate_questions_part1.py`（§5.3 全部 16 项）。**Runtime-local 纯 Python，可离线用手写 fixture 测**（§4.5） | 单测 + fixtures | 低 |
| **6** | `skills/generate/generate-questions-part1/`（SKILL.md + specification.md + schema） | 拿线上已确定材料手动跑一次 | 中。前提是阶段 3A **与 3B** 都已到位，否则出题方只能靠猜 |
| **7** | `audit_questions.schema.json` + `skills/audit/audit-questions-part1/` + `question_metrics.py`（**Runtime-local**；只有确实依赖 Agent 执行环境的步骤才走远程 Code Interpreter，见 §4.5） | 同一份题目手动跑审核 | 中。核心是盲读隔离要真的成立——靠的是输入裁剪，不是脚本位置 |
| **8** | `cross_check_questions.py`（§4.4 分维度容差）+ `guards.ANSWER_ONLY_KEYS` + `BlindQuestionAuditInput` | 单测：构造「审核重建 ≠ key」「仅 turn index 相邻但答案不同」两类 fixture 验证都能被抓出 | 低 |
| **9** | `orchestration/question_loop.py`：预检 → 题目生成 → 校验 → 审核 → 交叉检查 → 修题复评。复用 `_with_infra_retries` / `is_clean` 的**形状**；`pick_better` 的判据已定案，见 §8.3 | 端到端一次 | 中 |
| **10** | `agents.py` 核验（§2.2(f) 五项清单）。**结论待核验后给出，不预设「无需修改」** | 新 Skill 能被正确加载 + 路由无歧义 | 中 |
| **11** | 前端题目/answer key 展示 + Runtime action（**本阶段不做**，用户已说明不急） | — | — |

阶段 1/2/3A/5 之间可并行准备；阶段 6 依赖 2+**3A+3B**，阶段 7 依赖 5+6，阶段 8 依赖 5+7，
阶段 9 依赖 **3A+3B**+6+7+8。**阶段 0 与阶段 4 是两个前置门槛**：0 定时间预算，4 定交付语义，
两者都不该在 Skill 落地后才补。分支：当前已在 `feat/listening-full-test`，`main` 未受影响。

> `[2026-08-06]` **依赖写成 3A+3B 而不是「阶段 3」，是因为只有 3A 不足以支撑出题。**
> 3A 单独上线时每套材料只能得到 `SEMANTICS_MISSING`——语义结论的真实来源就是 3B。
> 而 §5.4 要求语义与确定性**共同**决定判决，只有确定性一半就放行题目生成，
> 等于把「能否基于这 10 个点出题」这个判断整个跳过，那正是 §5.4 设立预检环节的理由。
> **因此：Stage 3B 完成前不得进入题目生成主流程。**（用户 2026-08-06 明确）

## 6.2 两个需要现在就定下的架构判断（建议，非阻塞）

**(1) 题目阶段应做成独立 invoke。`[第三轮修正]`** 理由已经变了——不再是「15 分钟硬限装不下九次模型调用」（那个硬限不存在，见 §7），而是三条真实的：(a) **checkpoint 需要一个自然的续跑边界**，材料定稿是最好的那一个（§8.2）；(b) 题目阶段失败时不该重跑材料，独立 invoke 让这件事结构上成立而不是靠记得；(c) `web/runtime_client.py` 的 read timeout 约束单次 invoke，拆两次等于把单次时长减半——这是真实收益，只是原因是自设超时而非平台限制。建议新增 `action=generate_questions`，输入 `material_id`。

**(2) 题目数据走新 key 前缀 `_questions/`；槽位状态走 `_slots/`；Blueprint 本身采用版本化兼容。**
`_questions/` 与 `_slots/` 不能单独解决 Blueprint 仍嵌在 `_candidates/` / `_batches/` 中的问题。
新增 `blueprint_schema_version = 2`：新生成结果严格写 v2，历史无版本记录按 v1 兼容读取；前端合同支持 v1/v2 union。
如果拒绝兼容读取，才需要完整的 `_candidates_v2/`，不能误以为只新增 `_questions/` 就隔离了破坏性变更。

## 6.3 已定案（原「仍需确认」问题 1/2/5/9）

第三轮客户已定案四题，原文见 §0.4，展开见对应章节。此处只保留索引，避免与展开处不一致：

| 原问题 | 定案 | 展开 |
|---|---|---|
| 1 preflight 出口 | `PASS` / `PASS_WITH_JUSTIFICATION` / `REGENERATE_MATERIAL`；后者是内部补位不是交付失败 | §5.4 |
| 2 题型与题组 | 顶层 completion + layout 三选可混用；组内 layout 一致、题号连续、ordered evidence points 中不插入其他 group、不跨 window、一 window 可多组 | §5.5 |
| 5 题目循环 | 修题触发条件、10/10 严格交叉检查门槛、五级字典序择优 | §8.3 |
| 9 refill 上限 | 换 candidate → replacement slot → checkpoint 续跑；**取消「少量交付」出口** | §8.1, §8.2 |

## 6.4 决策记录与剩余实测

编号保留以便对照历史讨论。除问题 8 需要在实施前取得 Trellis 建项同意、问题 12 需要实测外，其余已按建议定案。

| # | 问题 | 建议 |
|---|---|---|
| 3 | 默认 word_limit 用 `ONE WORD ONLY` 还是 `NO MORE THAN TWO WORDS AND/OR A NUMBER`？ | **不设全局默认。** 题目生成后按每个题组的实际 canonical answers 选择全部答案都满足的、最严格的标准 instruction，并由 validator 复核 |
| 4 | 审核重建答案 ≠ answer_key 时判谁错？ | **不自动判生成方错**，标为「存在第二个同等成立答案 / carrier 限定不足」进入修题；但**重建为空则直接判 SR-005/AL-002 失败**。与 §8.3 的定案一致（「答案无法重建」与「答案不一致」都触发修题，但不预设是谁的错） |
| 6 | 审核 Agent 能否看 `material.scenario`？ | **条件允许。** 先对 scenario 做 target/答案泄漏扫描；无泄漏才传，命中答案或近似答案时不传 |
| 7 | Part 1 是否需要示例题（QR-020）？ | **建议不生成**。现行 IELTS Part 1 已无示例题，且 SR-019 明确把「Part 1 开头 example 说明」列为旧版制作话术需要改。若不生成，QR-020 记 `not_applicable` |
| 8 | 是否为这一阶段建 Trellis 任务？ | 工作量跨 10 个阶段、涉及 20+ 文件，建议建一个父任务 + 阶段 0/1/2 三个子任务。**由用户决定，未经同意不创建** |
| 10 | **证据连续性的阈值取多少？**（§5.5 约束 4） | **取消该问题：不设 turn-span 硬阈值。** 连续指 ordered evidence points 中不插入其他 group；`MAX_GROUP_SPAN` 保持 warning，自然度由 Agent 判断 |
| 11 | **单 candidate 内的定向修题上限取几次？**（§8.1 内层配额） | 材料侧现为 2 次生成重试 + 1 次修订。**建议题目侧也取 2 次定向修题**，与材料侧对称且有实测成本依据（一次修订+复评约 44s） |
| 12 | **read timeout 与 hard limit 提到多少？**（§7） | 不能凭空换一个数。**建议先做一次长 invoke 实测**（构造 1200s 才返回的调用），确认 AgentCore 对同步响应没有独立服务端超时，再定值 |
| 13 | **`batch.py`:102–103 引用的旧产品要求是否正式作废？** | **已确认正式作废。** 新要求“请求 N 套必须补齐 N 套，少于 N 不得标记成功”明确覆盖旧要求 |
| 14 | **`response_form` 取值域用哪一套？**（§0.5 差异 3、§5.4） | **采用双维度。** 持久字段 `response_form = numeric \| word \| phrase`：numeric 优先表示纯数值作答，word 表示单个非纯数值 orthographic token，phrase 表示多个 token；Python 内部推导 `qr027_class = numeric \| lexical \| mixed`，只用于 QR-027 统计，不持久化。例如 `Room 4B` 是 `phrase + mixed` |

---

**动代码前仍需实测的只有问题 12。** 问题 3/4/6/7/10/13/14 已按上表定案；问题 11 先以 2 次为初始配置并通过观测数据调整。问题 8（Trellis 任务）在正式实施前处理。

> `[2026-08-06]` **问题 12 已实测关闭**，见 §7.5 与 `backend/docs/timing.md`「平台时限实测」。
> 结论与原假设不同：同步 15 分钟硬限**存在**，且超限时平台不通知客户端。

---

# 7. `[第三轮]` Runtime 900 秒错误假设审计

## 7.1 事实修正

> **`[2026-08-06 实测推翻]` 本节标题句是错的。** 下面这句原文保留：
>
> > **AgentCore 没有「单次 invocation 15 分钟同步硬上限」。**
>
> 实际上有，而且实测成立。账号 quota 与官网都写了两条独立不可调的 **invocation** 限制：
> `L-3ED45A13` 同步 15 min、`L-C91AC63F` 流式 60 min。本节讲的
> `idleRuntimeSessionTimeout` / `maxLifetime` 是 **microVM 生命周期**，这部分没错，也确实与
> invocation 时限是两回事——**但「两回事」不等于「后者不存在」**。本节的推理从
> 「生命周期参数里没有这条限制」跳到了「平台没有这条限制」，中间漏掉了同步/流式这一层。
>
> 实测（`backend/docs/timing.md`「平台时限实测」）：1000s 的同步 invoke，其 handler 在
> **(900.5s, 960.5s]** 之间被平台终止（观测到的是 handler 停止推进且永不返回，
> 不是「microVM 被销毁」——本次实测区分不了，对结论也不影响）；
> 1200s 的 SSE invoke 跑到 1205.98s 正常收尾。
> 所以 900s 对同步路径是**平台属性**，不是「六处全是项目自设」。
>
> 还有一条两种说法都没提到、实测才发现的行为：**同步路径超限时平台不通知客户端**——
> 不回 504、不回任何错误码、连接不断开，客户端只能等到自己的 `read_timeout`
> （实测白等到 3600s，异常是 botocore 本地的 `ReadTimeoutError`，**连 RequestId 都没有**）。
> 这直接影响 §7.2 对 `READ_TIMEOUT_SECONDS` 的处理方向，见该节。

官方生命周期只有两个量：

| 参数 | 值 | 真实含义 |
|---|---|---|
| `idleRuntimeSessionTimeout` | 900s | Session **空闲** 15 分钟后回收 microVM。活跃流不触发 |
| `maxLifetime` | 28800s | 单个 microVM 最长运行 8 小时 |

且 **Session 可以在新 microVM 上继续**——microVM 停止不等于 Session 结束。

`deploy/runtime.sh`:36–41 的配置与注释**都正确**，是仓库里唯一把生命周期讲对的地方，注释里那句「A stopped microVM does NOT end the session; the next invoke provisions a fresh one, so this is a ceiling on one instance, not on usability」正是本节要在别处补的认识。**不要动这个文件。**

900s 出现在其他六处，全部是**项目自设**的业务/网络限制，不是平台属性。

> `[2026-08-06 实测修正]` 上面这句要限定：那六处的**取值**确实是项目自设（没人被平台强制写 900），
> 但「900s 与平台无关」是错的——同步路径上 900s 正是平台值。区别在于生产走 SSE，
> 受 3600s 约束，所以那六处对生产而言偏紧；而任何**同步** invoke 仍真实地卡在 900s。

## 7.2 A 类：会真实限制长流程（必须改，本轮不改数值）

> **`[2026-08-06 实测修正]` 下表对 `runtime_client.py` 那句注释的「整句失效」判断要改写。**
> 原判断基于「平台不 bound 这个」，而实测显示平台确实 bound——只是分两层：
>
> | 层 | 平台上限 | 那句注释在这一层 |
> |---|---|---|
> | 同步（`application/json`） | 900s（`L-3ED45A13`） | **成立**。「read timeout 要 bound 平台 bound 的同一个东西，而那是 900s」在同步路径上字面正确 |
> | 流式（SSE，**生产走这条**） | 3600s（`L-C91AC63F`） | **不成立**。生产路径的平台上限是 3600s，不是 900s |
>
> 所以那句注释真正的问题不是「说了平台不存在的限制」，而是**没写清生产走的是 SSE**：
> 它把同步路径的平台值当成了所有路径的平台值。900s 这个数**对生产是项目自设且偏紧**，
> 而不是「与平台无关」。
>
> **并且提高该值的方向要加一条硬约束。** 实测发现同步路径超限时平台**不通知客户端**
> （不回 504、不回错误码、连接挂着），客户端唯一止损是自己的 `read_timeout`——
> 探针白等到 3600s 才报本地 `ReadTimeoutError`，无 RequestId 可查。因此：
>
> - **流式路径**（生产）：提到 1800–3600s 是合理的，原方向不变。
> - **同步路径**（若日后有任何同步 invoke）：`read_timeout` **不应超过 900s**，
>   否则一次超限就是几十分钟的静默等待，而后端早在 900s 就死了。
>
> 依据全文见 `backend/docs/timing.md`「平台时限实测」。

| 位置 | 值 | 问题 | 未来方向 |
|---|---|---|---|
| `web/runtime_client.py`:47 | `READ_TIMEOUT_SECONDS=900` | **最危险的一处。** botocore 单次 socket read 超时。材料+题目全流程按 `timing.md` 的每模型调用 ~32s 估算需 9–11 次调用 → 300–500s，加重试与换 candidate 可轻易超 900s。超时后 `FanOut._pump` 把它变成 `material_failed`，槽位被判失败而后端其实在正常工作——这正是该文件 :30–41 亲自记录过的那个 bug 的更大版本 | 提到 1800–3600s 量级。**但注释里的理由「the read timeout has to bound the same thing the PLATFORM bounds -- one material's whole invocation -- and that is 900s」整句失效**：平台不 bound 这个。新理由应是「一次业务流程的合理上限」。改值时必须与 ALB idle 120s / CloudFront OriginReadTimeout 60s + 15s 心跳的组合一起看——心跳保连接，read timeout 保单次读，只调一个会留下另一个短板 |
| `backend/orchestration/batch.py`:75 | `IELTS_HARD_LIMIT=900` | 减 `SAFETY_MARGIN=90` 得 810s 可用；`may_start()` 要求 `remaining > p95(240)`、`may_revise()` 要求 `> 120`。加题目阶段后 810s 装不下「材料 + 换 candidate + 题目 + 修题」，会产生 `skipped_time_budget`——而新要求禁止槽位不完成 | 与 read timeout 同量级。**`P95_PER_MATERIAL=240` 必须重测**（现值只含材料）；`REVISION_COST=120` 同理。不重测就调值等于换一组猜测 |

## 7.3 B 类：叙述错误（不影响行为，但会误导后续每一个决策）

`README.md`:152, 429，尤其 **:975 把 `IELTS_HARD_LIMIT` 说明写成「平台同步上限」——这是把项目自设值标成平台属性，最该改的一行文档**；`backend/docs/handover.md`:43；`web/fanout.py`:6, 16, 98–99（注释「The platform's synchronous wall on ONE invocation」）, 551；`web/app.py`:31；`web/batch_history.py`:119；`backend/orchestration/batch.py`:3–8, 14–21, 72, 74, 119–134（**`Budget` 的整个 docstring 建立在「平台硬限」之上**）；`backend/request.py`:70–73, 90；`backend/agents.py`:241–242（描述的 `pty.fork()` 挂死现象是真的，但归因错了——挂死是被 read timeout 或 idle 回收终止，不是平台 invocation 上限）；`backend/deterministic/cards.py`:8。

前端：`config/runtimeConfig.ts`:59–61, 96；`config/scenarioTypes.ts`:22；`features/scenario-select/ScenarioSelectPage.tsx`:22–23；`features/batch-progress/BatchProgressPage.tsx`:561–563；`api/agentcore.ts`:363–364, 889；`domain/batchEstimate.ts`:7（且题目阶段会显著拉长预估，需连同 `WAVE_SECONDS` 重测）；`public/config.json`:16。

测试叙述：`backend/tests/test_batch.py`:169, 286, 468, 561, 714（另有约 18 处 `hard_limit=900` 是测试参数，值本身无害）；`test_blindness.py`:460；`web/tests/test_runtime_client.py`:61；`test_fanout.py`:46；`test_batch_history.py`:222。

## 7.4 C 类：正确，不要动

- `deploy/runtime.sh`:36–41 —— 见 §7.1。
- `backend/sandboxed_metrics.py`:57–58 的 `SESSION_TIMEOUT_SECONDS=900` —— 这是 **Code Interpreter 会话**超时，与 Runtime 无关，值本身合理。只是 :57 的措辞「the platform's own timeout is 900s」含糊，易被读成 Runtime 限制，建议改措辞不改值。
- `web/fanout.py`:112 `HEARTBEAT_SECONDS=15` 与 `deploy/edge.sh`:29–31, 140, 186–187 的 ALB 120s / CloudFront 60s —— 针对的是**中间层 idle 超时**，与 Runtime 生命周期是不同约束，实测 96s 静默的依据仍成立。**流程变长后心跳更重要，不是更不重要。**
- `frontend/src/mocks/handlers.ts`:43, 831、`mocks/fixtures/index.ts`:207–208、`LatestBatchRoute.test.tsx`:80、`compareFacts.test.ts`:140 的 900 —— 同名数字巧合（ms、时间戳、字数），无关。

## 7.5 一个仍未验证的点 —— `[2026-08-06 已验证]`

> **已实测，结论见 `backend/docs/timing.md`「平台时限实测」。** 本节原文（保留）说得对：
> 这件事必须实测而不能推断。实测结果与本节的**理论预期一致但更严格**：
>
> - SSE 心跳确实不触发 idle 判定：1200s 的 SSE invoke 跑到 1205.98s 正常收尾，收到收尾帧。
> - **但同步路径有 900s 平台硬限，这是本节和 §7.1 都没预见的**：1000s 的同步 invoke，
>   其 handler 在 (900.5s, 960.5s] 被平台终止。
> - **且超限时平台不通知客户端**，客户端只能等自己的 `read_timeout`。这一条使
>   「提高 read timeout」在同步路径上从「更安全」变成「更危险」，见 §7.2 修正。
> - 附带观测：15s 心跳的实际到达间隔最大 30.0s（有合并投递），仍远低于 ALB 120s /
>   CloudFront 60s，但说明心跳间隔不能假定精确，日后调大心跳需按 2 倍留余量。
>
> 问题 12 至此关闭。**本轮未改任何超时数值**——`P95_PER_MATERIAL` 的重测依赖题目阶段存在。

两个生命周期参数管的是 **microVM**，不直接等于「一次 `InvokeAgentRuntime` 可以挂多久不返回」。SSE 流式响应下每 15s 有心跳，理论上不触发任何 idle 判定；但**把 read timeout 提到 1800s 之前应先实测**（构造一个 1200s 才结束的 invoke），否则只是把一个错误假设换成另一个未验证假设。列为 §6.4 问题 12。

---

# 8. `[第三轮]` 精确数量交付与槽位补齐

## 8.1 与现有设计的三处直接冲突

现状不是疏漏，是**显式的反向设计决定**，所以改动需要明确记录依据：

| 位置 | 现有行为 | 冲突 |
|---|---|---|
| `batch.py`:42–43 | 「Fewer materials than asked for beats a 504 that loses all of them.」 | 直接违反「不能少返回一套」 |
| `batch.py`:102–103 | 引用旧产品要求：「补不上就少返回一套，不放空卡片」 | **旧产品要求与新要求相反**，需明确作废（§6.4 问题 13） |
| `batch.py`:263–271, 296–299 | 预算耗尽 → `refill_abandoned` → 返回已有；全不可评估 → 报 slot 失败 | 槽位可以不完成 |

另有一处结构性冲突：`web/fanout.py`:312–314 把「没有终态事件的槽位」记为 `failed` 并计入 `batch_completed`。槽位变成跨 invoke 实体后，这个「在一次 SSE 流内判定终态」的模型必须改成查 `_slots/` 状态，否则**一个还在重试的槽位会被前端画成失败卡**。

## 8.2 方案

**(1) 槽位从调度单位升为持久实体。** 现在 `slot-N` 只是 `run_batch` 里一个 `asyncio` task 名，进程结束即消失。新要求需要它在 S3 有记录——新 key 前缀 `_slots/{batch_id}/{slot_id}.json`：

```
state: material_pending → material_done → questions_pending → complete
attempts: { candidate_swaps: n, material_repairs: n, question_repairs: n }
current_candidate_id
last_failure
checkpoint_at
```

这一条同时满足「已完成的前置阶段必须持久化」：**材料定稿即写 `material_done`**，题目阶段失败只回退到该状态，不重跑材料。

**(2) 尝试上限分两级。** 内层 = 单 candidate 内的定向修复次数（材料 2 次已有，题目侧建议 2 次，§6.4 问题 11）。外层 = 单槽位的 candidate 更换次数。`REGENERATE_MATERIAL`、题目质量反复不合格、Script 不支持十题——三者都消耗**外层**配额。现有 `MAX_REFILL_ROUNDS=2` 可升格为外层上限，但语义要从「refill NOT_ASSESSABLE」扩为「换 candidate」。

**(3) 外层上限耗尽 → 建 replacement slot，不是降级交付。** 客户定案：

> 单个 slot 的单轮生成和修订次数可以有上限；slot 达到上限后，放弃该 candidate，创建 replacement slot；batch 不能因为部分 slot 耗尽重试就少交付；请求 N 套时，只有收集到 N 套合格结果才能标记成功；**不得把少于 N 套标记为成功；不设置一个「总尝试次数耗尽后少量交付」的正常出口。**

这**取消了我在第二轮 §5.4 写的兜底**（「达上限交付一套带明确 findings 的题目」）。那个出口看起来温和，实际是「用降低门槛换数量」的另一种写法，与「不得通过降低硬性质量门槛来补齐数量」冲突。

**(4) checkpoint 续跑。** 接近 Runtime 生命周期上限时存 checkpoint，由下一次 invoke 继续 refill。`material_done` 是最自然的 checkpoint 边界——这也是 §6.2(1) 把题目阶段拆成独立 invoke 的第一条理由。

**(5) 基础设施永久故障的诚实出口。** 客户明确承认技术上无法保证一定成功，但要求这种情况**保持任务未完成或报告系统故障，不得伪装成完整交付**。落地为 `_slots/` 里的 `state` 停在非 `complete`，batch 状态为 `incomplete` 或 `system_failure`，**不是** `succeeded`。这与「不设少量交付出口」是同一条要求的两面：少交付不是合法结果，但**假称交付完整**更不可接受。

## 8.3 题目循环：触发、门槛与择优（已定案）

**修题触发条件**（任一即触发）：

| 触发 | 说明 |
|---|---|
| 确定性 validator 出现 error | 直接修题 |
| 盲审出现 CRITICAL / MAJOR | 修题 |
| 答案无法重建 / 不唯一 / 不一致 | 修题 |
| 证据 / window / 命题不一致 | 修题 |
| 仅有 MINOR / INFO | **原则上保留原题**，避免无意义循环 |
| 判断材料本身不支持可靠出题 | 返回 `REGENERATE_MATERIAL`，**不能修改 Script** |

**交叉检查采用严格门槛**——这比我第二轮设想的「10 题里几题不一致才退回」严得多，等于门槛为零容忍：

- **10/10 题均能独立重建**；
- 答案、证据、顺序、window、命题归属**全部通过**；
- **任一题存在关键不一致，都不能把题目包视为 clean。**

注意这与 §4.4 的分维度容差不矛盾：容差表定义的是「什么算一致」（turn_index 在三个前置条件下允许 ±1），严格门槛定义的是「几题一致才算 clean」（全部）。前者是判定标准，后者是聚合规则。

**原题 vs 修订题择优：字典序，不是加权分数。**

```
1. 硬校验通过        ← 一票否决，失败者不得因后续项更优而胜出
2. 交叉检查通过题数
3. CRITICAL / MAJOR 数量
4. MINOR 数量
5. 其他质量指标
```

这一条是对材料侧 `pick_better` 的明确否定：材料侧比的是审核总分（一个标量），题目侧**任何硬校验失败的版本都不能因为综合分更高而胜出**。实现上应写成显式的元组比较，而不是加权求和——加权求和的本质就是允许用别的维度补偿硬校验失败。

---

# 9. `[第三轮]` 受影响文件总览、测试策略与风险

## 9.1 按类别的受影响文件

前面各节按**议题**组织，容易漏掉「某个文件被三个议题各改一处」的情况。这里按**文件**组织，作为动手时的清单。

**A. Skill 文本（改写，无代码风险）**
`skills/generate/generate-listening-part1/SKILL.md`:41、`references/specification.md`:93/97/99/199/257、`skills/audit/audit-listening-part1/SKILL.md`:86、`references/audit-rubric.md`:46。
**新增**：`skills/generate/generate-questions-part1/`（SKILL.md + references + schema）、`skills/audit/audit-questions-part1/`（同构）。目录位置必须分别落在 `generate/` 和 `audit/` 下——盲审隔离靠的是 `agents.py` 里两个**物理分离的 skill pool**，放错目录等于取消隔离。

**B. Schema（契约变更，需考虑旧数据）**
`schemas/blueprint.schema.json`:41（删 `question_type_coverage.multiple_choice`，并考虑整个字段改名 `completion_layout_coverage`）、:171（`item_form` 删枚举值 + 语义改为 `completion_layout`）、`form_group` 从 nullable 改必填（§5.5）、**新增三个必填字段 `response_form` / `answer_category` / `narrator_window_id`**（§0.5、§5.4）。
**新增**：`question_package.schema.json`、`question_audit_report.schema.json`。
⚠️ 这四处合起来是新生成合同的**破坏性**变更：363 条旧记录既可能有 `form_group: null`，也一定缺三个新字段。
采用 `blueprint_schema_version = 2`：写入端只产 v2，读取端兼容无版本的 v1，前端使用 v1/v2 union。
只有拒绝兼容读取时才需要完整的新 candidate 前缀；`_questions/` 本身不能解决旧 Blueprint 兼容问题。

**C. 确定性校验（本轮主战场）**
`validate_part1.py` 五处（§2.1）+ 分组门槛重写（§2.2a）+ ordered evidence-point 连续性（§5.5）+ 出题可行性预检的确定性部分（§5.4）+ **三个新字段的复核逻辑**（`response_form` 比对形态、`answer_category` 做枚举/完整性/相对计数、`narrator_window_id` 比对 `validate_part1.py`:465 的窗口解析）+ `ITEM_KEYS`:25 加三个键。
`[第三轮]` 三个新字段的复核里，`narrator_window_id` **必须真的独立算一遍再比对**（用 `validate_part1.py`:465 的 `FIRST_RANGE_RE`/`SECOND_RANGE_RE`），不能读了字段就算过——否则 SC-019 的窗口归属就落回模型自评了。这是实现时最容易做错的一处。
`skills/shared/cross_check.py` —— 核心算法**不动**（它只用 number/type/target/evidence/turn_index，不碰 `item_form`/`form_group`），anchor repair 保留。
**新增**：`validate_questions_part1.py`、`cross_check_questions.py`。
`[2026-08-06 Stage 3A 已落地]` `skills/generate/generate-listening-part1/scripts/question_feasibility_preflight.py`
—— 与 `validate_part1.py` 同目录（它需要 `import validate_part1 as validator` 以在**运行时**读 QR-027 门槛；
用 `from validate_part1 import QR027_...` 会在 import 时把值拷成局部名，此后 monkeypatch 源模块毫无影响，
使「单一事实来源」的测试假绿通过——已实测）。签名 `preflight(validation, feasibility)` **不接 blueprint**：
拿不到 blueprint，聚合器就没有能力自己数 target 或判自然度，§5.4 的职责边界因此落在签名上而非注释里。
测试在 `skills/shared/tests/run_tests.py`（九个套件，+249 checks，由 ci_gates gate 6 覆盖）。

**D. 编排（结构改动最大）**
`backend/orchestration/batch.py` —— 推翻 :42–43 / :102–103 / :263–271 / :296–299 的少交付设计；`Budget` docstring 重写；`P95_PER_MATERIAL` 重测。
`backend/orchestration/publish.py`:113–153 —— `Candidate.__slots__` 加 question package / audit 字段。
`backend/orchestration/candidate_store.py` —— 分阶段持久化落点（现有 `CANDIDATE_PREFIX="_candidates/"`、TTL 30 天）。
`backend/app.py`:66–93 —— 新增 `action=generate_questions` 分派。
`backend/orchestration/loop.py` —— 题目阶段的 revise/复评循环（可复用现有三次重试骨架，但择优逻辑不能复用，见 §8.3）。
**新增**：`_slots/` 槽位状态存储（§8.1）。
`web/fanout.py`:312–314 —— 「无终态事件即 failed」必须改为查 `_slots/` 状态。
`web/runtime_client.py`:47 —— read timeout（等实测，§6.4 问题 12）。

**E. 前端**
`src/contracts/blueprint.ts`:25/74（MC 契约 + `item_form` union + `form_group` 必填）、`src/domain/types.ts`:88/95、`src/domain/formGroups.ts`:61/64/173、`src/features/material-reader/QuestionTypePanel.tsx`:88–102、`src/domain/validationNotes.ts`:129（死代码）、`src/domain/domain.test.ts`:540/541/550/559、`src/mocks/fixtures/generated.ts`（6 处）、`domain/batchEstimate.ts`（题目阶段拉长预估）、`config/runtimeConfig.ts` 的 `perMaterialWallSeconds`。

`[第四轮补]` **两条 codegen 链决定了前端的改动顺序，不是偏好问题**：
`blueprint.schema.json` --`npm run contracts:gen`--> `contracts/blueprint.ts`；
`build_fixtures.py` --> `shared/tests/fixtures/*.json` --`npm run fixtures:gen`--> `mocks/fixtures/generated.ts`。
这两个前端文件头都写着 AUTO-GENERATED, DO NOT EDIT，手改会被下次 `codegen:check` 判为漂移。
所以每一轮改动都必须**先源后产物、先 Python 后前端**。
**新增**：题目/答案/证据的展示与审核报告视图——这部分**尚未设计**，是 §6.1 最后一个阶段。

**F. Fixture 与测试**
`build_fixtures.py`:127–128（**先改这个**）→ 重跑生成 fixture → `run_tests.py`:464–480 重写。
`backend/tests/test_batch.py`（交付语义变了，少交付相关断言会**正当地**失败）、`test_blindness.py`（新增题目审核 Skill 后需扩展隔离断言）、`web/tests/test_fanout.py`（终态判定改了）。

**G. 文档**
`README.md`:975（**最该改的一行**：把项目自设值标成平台属性）、:152、:429；`backend/docs/handover.md`:43；`backend/docs/sample/blueprint.json`。

## 9.2 测试策略

按「什么样的 bug 会溜过去」倒推，而不是按覆盖率：

| 层 | 要防的具体失效 | 手段 |
|---|---|---|
| 单测 | MC 删除删漏一半（枚举删了但检查还在，或反之） | 用 fixture 跑 `validate_part1.py`，断言**不再出现** MC 相关 error，且 `option` 类型的点**仍然合法**——这两条必须同时断言，否则「删干净」和「删过头」区分不开 |
| 单测 | `response_form` 推导对 `Room 4B` / `3 Oakwood Lane` / `9.30` / `two-thirty` 判错 | 表驱动用例，**必须包含 `address` 混合型**——`validate_part1.py`:30 把 `address` 归入 `NUMERIC_TYPES` 而 `SPELLED_TYPES` 只有 `name`，这是推导逻辑最容易出错的地方 |
| 单测 | `[第三轮]` 三个新字段的复核退化成「读字段就算过」 | 构造**声明与事实不符**的 blueprint：`response_form: "numeric"` 但 target 是 `Oakwood`、`narrator_window_id` 指向错误窗口。断言两者都被报错。**这是新字段唯一的价值所在**——不测这个，加字段就纯粹是增加了信任面 |
| 单测 | Blueprint v2 上线后历史记录无法读取 | 无版本的 v1 fixture 走兼容解析且保持原展示；`blueprint_schema_version=2` 缺任一 v2 必填字段必须失败；前端 v1/v2 union 都能解析 |
| 单测/审核 | `answer_category` 只通过枚举却语义错误 | Python 检查枚举、完整性和计数；材料审核 fixture 将明显错误类别报为 finding，避免把语义判断伪装成确定性检查 |
| 单测 | 严格门槛被实现成「多数通过」 | 构造 9/10 通过的题目包，断言**判为 not clean** |
| 单测 | 择优退化成加权求和 | 构造「硬校验失败但其余全优」的版本，断言**不胜出** |
| 集成 | 槽位跨 invoke 丢状态 | 写 `_slots/` → 模拟进程结束 → 新 invoke 读回，断言 `material_done` 的材料**不被重跑** |
| 集成 | 少交付被当成成功 | 构造一个永远失败的 slot，断言 batch 状态**不是** `succeeded` 且 `_slots/` 有非 complete 记录 |
| 隔离 | 题目审核 Agent 拿到生成方答案 | 扩展 `test_blindness.py`：断言 `BlindQuestionAuditInput` 不含 canonical answer / accepted alternatives / evidence（复用现有 `guards.BLUEPRINT_ONLY_KEYS` 的做法） |
| 实测 | 长 invoke 是否真的能返回 | §7.5 的 1200s 实测，**这是阶段 0 的验收条件** |

`test_blindness.py` 这一行不是可选项：整个审核独立性建立在「审核方看不到答案」上，而这件事**只能靠测试保证**——代码上没有任何东西阻止有人往 audit input 里多塞一个字段。

## 9.3 风险

按「会造成什么后果」排序，不按发生概率：

0. **`[第四轮补·实测确认]` v1 兼容问题在删 MC 的那一刻就已经发生，不是等到 v2 加字段才发生。**
   阶段 1 收窄 `item_form` union 之后，`frontend/src/api/__fixtures__/real-batch.sse.txt`
   这份实况抓包（items 2 与 6 是 `multiple_choice`）立刻被 `analyseFormGroups` 判为
   **自相矛盾**：这两个题号不在被遍历的 form 列表里，于是从 coverage 展平中掉出，
   `coversAllTen=false`、`consistent=false`。前端会告诉审阅者「上周生成的材料自相矛盾」。
   实测证据：`agentcore.test.ts` 的 `agrees with the real question_type_coverage` 用例
   在删 MC 后立即失败（`expected false to be true`）。

   这条**提前于风险 1**发生，所以 `blueprint_schema_version` 的引入不能等到「加三个必填字段」时
   才做——只要 union 收窄，历史数据的展示就已经退化。当前处置：阶段 1 把该用例改为
   **断言这个退化确实存在**并写明原因（不修改实况抓包、不伪造一致性），阶段 2 落地 v1 兼容读取后
   再把它翻回 `consistent === true`。**影响范围仅限历史候选的展示，不影响新生成，且阶段 1 不部署。**

1. **blueprint schema 的四处必填变更可能打断旧数据读取。** 原先只有 `form_group` nullable→必填一处；现在又加了 `response_form`、`answer_category`、`narrator_window_id` 三个 v2 必填字段。影响面：363 条 `_candidates/` 记录 + 34 条 `_batches/`，历史记录均按 v1 处理。缓解：增加 `blueprint_schema_version = 2`；新生成严格校验 v2，读取历史无版本记录时走 v1 兼容解析，前端支持 v1/v2 union。仅增加 `_questions/` 或 `_slots/` 不能解决嵌入旧 candidate/batch 的 Blueprint 兼容问题。
2. **推翻少交付设计后，失败模式从「少一套」变成「长时间不结束」。** 现在的行为至少会返回；改成必须凑齐 N 套后，一个持续失败的 slot 会让 batch 长期挂在 incomplete。缓解：`_slots/` 状态必须对前端可见，让「还在重试」和「卡死」在界面上可区分——否则用户看到的就是转圈。
3. **read timeout 提值但没实测。** 后果是把一个已知错误的假设换成一个未知的假设，且下次踩到时更难归因。缓解：阶段 0 的实测是硬前提，不是建议。
4. **`P95_PER_MATERIAL=240` 沿用到题目阶段。** 现值只测了材料。用旧值算新预算会系统性低估，表现为大量 `skipped_time_budget`。缓解：重测后再调 `Budget`。
5. **严格门槛（10/10）导致修题循环变长。** 零容忍 + 有限修题次数，会让更多 candidate 走到「换材料」。这是客户明确选择的取舍（质量优先于速度），但**成本要提前说清**：单套的期望耗时和 token 都会上升。
6. **题目阶段前端未设计。** 后端做完但没有展示面，等于没交付。缓解：§6.1 把它排在最后是对的，但不能因为排在最后就默认它简单——答案 + 证据 + 审核报告三块的展示，尤其「证据必须与题面物理分离」在 UI 上怎么落，需要单独设计。
