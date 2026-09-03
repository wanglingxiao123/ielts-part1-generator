# qti_export：题目包 → QTI 2.2.4 内容包

把已交付的题目包（`_questions/{material_id}.json`，或批注修订产出的不可变版本）导出成
**IMS QTI 2.2.4** 内容包，供下游题库 / 投递引擎导入。规范：
<https://www.imsglobal.org/question/qtiv2p2/imsqti_v2p2_impl.html>（勘误至 2.2.4，2021-03-18）。

```
GET /api/material-qti/{material_id}                    zip：imsmanifest.xml + items/*.xml + 侧车
GET /api/material-qti/{material_id}?format=item        只要 assessmentItem XML
GET /api/material-qti/{material_id}?format=summary     JSON 概览：标识符、题数、待人工确认项
GET /api/material-qti/{material_id}?version_id=<id>    指定题目版本；缺省为当前采用版本

python3 -m qti_export _questions/20260808-booking-hotel-45425df4.json -o build/ --validate
```

产物：

```
ielts-<material_id>-v<n>.zip
├── imsmanifest.xml                          IMS Content Package manifest（QTIv2.2 Package）
├── items/part1-<material_id>-v<n>.xml       assessmentItem：十题共一个 item，十个 textEntryInteraction
├── reject_candidates.json                   每题的接受集 / 拒绝集，供下游判分回归与人工评审
└── review.txt                               需人工确认的判分口径（存在时才有）
```

`<n>` 是页面上的题目版本号（V1 = 原始交付版）。它进 `assessmentItem/@identifier`
（`ielts-<material_id>-v<n>`），所以同一材料的两个版本在下游题库里不会互相覆盖。

## 0. 五条最先要知道的结论

1. **QTI 没有 2.3。** 官方线是 1.2 → 2.0 → 2.1 → 2.2（→ 2.2.4）→ 3.0。本模块对 2.2.4。
2. **导出不是「把 JSON 换个格式」，先过门禁。** 转换前重跑六道一致性门禁（§4）：上游审核
   通过、命题与盲审两侧答案一致、题号闭合、字数上限自洽。任何一道不过就整份拒绝并列出全部
   原因（HTTP 422），**绝不**把出问题的题丢掉后导出九道题的包——那种包能过 XSD，会一路走到
   考生面前才暴露。
3. **不声明产不出来的资源。** 这条流水线没有听力音频可交付，所以 item 里**不生成** `qh5:audio`，
   manifest 也不声明音频。声明了而不存在的资源 XSD 照样全绿，导入端却会直接失败。音频由下游
   另行挂载。
4. **卷面文字决定答案形态，必须进 QTI。** 真实卷面是 `The reservation covers ______ in total`，
   不是「标签 + 空格」。这些文字（`carrier_before` / `carrier_after`）决定了前者必须带单位
   （裸 `3` 判错）、`£ ______ per night` 是裸数字（`128 pounds` 判错）。容错集据此裁剪（§3 R6/R7）。
5. **`package.material_id` 不能当标识符。** 实测常是自由文本（`"Test 1 Part 1"`），既不唯一也不是
   合法 NCName。存储键里的材料 id（`20260808-booking-hotel-45425df4`）才是身份：全部唯一、合法
   NCName、还能还原出场景键。

## 1. 模块

| 文件 | 职责 |
|---|---|
| `questions_input.py` | 输入读取 + 准入门禁 G1–G6；字数上限文案解析表 `WORD_LIMIT_SPECS`；计数口径 `count_tokens` |
| `accept_sets.py` | 每题的接受集（→ `mapping/mapEntry`）与拒绝集；两张人工确认表 `DISTINCTIVE` / `EXTRA` |
| `emitter.py` | IR（`Material` / `GroupIR` / `Gap`）与 QTI 2.2 序列化：三种版式、manifest、zip |
| `service.py` | 两种存储形状（原始交付 / 修订版本）收敛成同一输入；`export_document` 入口 |
| `validate.py` | 对官方 XSD 校验（需 lxml + 本地镜像）；测试与 CLI 用，Web 运行时不依赖 |
| `fetch_schemas.py` | 镜像 QTI 2.2.4 的 XSD 依赖链（49 个文件）到 `schemas/mirror/`，不入库 |
| `__main__.py` | 离线 CLI |
| `tests/` | 三份真实交付集夹具（覆盖 form / table / note）与测试 |

只依赖标准库。`web/Dockerfile` 把整个目录拷进 Web 镜像。

## 2. 字段映射

| JSON 路径 | QTI 2.2 | 规则 |
|---|---|---|
| 存储键 `{material_id}` | `assessmentItem/@identifier` | `ielts-<material_id>-v<n>`；同时进 `manifest/@identifier`（`MANIFEST-` 前缀）与 `resource/@identifier`（`RES-` 前缀） |
| 存储键 `{material_id}` | `imsmd:general/imsmd:identifier/imsmd:entry` | 原值，catalog 写 `ielts-material` |
| 存储键中段 `booking-hotel` | `imsmd:keyword` | 场景键，从 `<日期>-<场景>-<hash8>` 还原 |
| `package.test_package` + `package.reference` | `assessmentItem/@title` | `IELTS Listening Test 1 - Part 1 - Booking hotel (v1)` |
| `package.reference` | `itemBody//h3` | `Part 1 — Questions 1–10`；`part_number` 取其中数字 |
| `question_face.instructions[].question_range` | `itemBody//h4.ielts-questions` | `"1-5"` → `Questions 1–5`；单题组裸数字 `"10"` 也要能解析 |
| `question_face.instructions[].instruction_text` | `p.ielts-rubric` | 原文，字数上限那一段加粗 |
| `question_face.instructions[].word_limit` | 题头文案 + 容错集过滤 | 见 §3 R5 |
| `question_face.groups[].layout` | 版式 | `form` → `table.ielts-form`；`table` → `table.ielts-table`；`note` → `h5` + `ul.ielts-note-lines` |
| `question_face.groups[].title` | 分组标题 `h4` | 可缺，缺时退回 `group_id` |
| `question_face.groups[].structure` | 版式骨架 | `row_labels` / `column_labels` / `row_header_label` / `hierarchy` / `note_sections` / `table_rows` |
| `question_face.groups[].signposts[]` | — | 出题意图，含答案线索，**不进卷面** |
| `question_face.questions[].number` | `responseDeclaration/@identifier` | `RESPONSE_<n>`；题号以 `strong.qnum` 内联在答题格里 |
| `question_face.questions[].carrier_before` | 题号**之前**的卷面文字 | 接缝补空格：prefix 与题号之间总是恰好一个空格（`£ 6 ___`，题号是独立 token） |
| `question_face.questions[].carrier_after` | 空格**之后**的卷面文字 | 首字符是 `.,;:?!)]}'’%` 时紧贴，否则补一个空格 |
| `question_face.questions[].blank` | — | 只用于 G3 校验内联题号；空格本身由 `textEntryInteraction` 承担 |
| `question_face.questions[].response_form` | `textEntryInteraction/@expectedLength` | `max(base, 向上取到 5 的倍数(最长接受写法 + 4))`，base = word 20 / phrase 25 / numeric 10 |
| `answer_key[].canonical` | `correctResponse/value` | 原值；卷面已印货币符号时剥掉符号（否则卷面变成 `£ £128`） |
| `answer_key[].alternatives[]` | `mapping/mapEntry` 种子 | 实测几乎全空，容错集仍按 §3 推导 |
| `review.reconstructed_answers[].competing_candidates[]` | 拒绝集 | 只取 `equally_supported=false` 的；`true` 的是缺陷，G2 直接拒收 |
| `package.evidence[].quote` | — | 门禁素材；不进卷面（进了就是答案泄漏） |
| `advisories[]`、门禁 advisory、容错集待审项 | `review.txt` | 逐行 |

**标签只在数量与题数完全相等时才按位置对齐。** note 版式常见标签少于题数（也有 7 个标题配
5 道题的），硬对齐必然错位，而错位的标签比没有标签更糟——它把答案指向另一行的语义，且看起来
完全正常。`note_sections[].question_numbers` 与 `table_rows[].cells[].question_number` 是题号的
权威归属，优先按它们排布；漏掉的题补在末尾，不静默丢题。

## 3. 答案容错规则（接受集 → `mapEntry`）

用 `mapping/mapEntry` 枚举而不用 `patternMatch` 正则：枚举能被 XSD 校验、能人工评审、能 diff、
能逐条写回归用例。一个放宽了的正则会静默把干扰项收进来，而枚举里多一行 `132` 评审时一眼可见。

| 规则 | 说明 |
|---|---|
| R0 大小写不敏感 | 所有 `mapEntry` 一律 `caseSensitive="false"` |
| R1 target 本身必入集 | 无条件 |
| R2 拼写必须精确 | 不生成编辑距离 / 近音变体。IELTS 拼错就是错 |
| R3 不跨语义放宽 | 只生成同义写法，绝不生成上位词（`twin` 可以，`room` 不行） |
| R4 干扰项必须排除 | 上游标注的竞争答案一律进拒绝集；与接受集撞了时**不静默丢任何一侧**，上报待人工裁决 |
| R5 字数上限过滤 | 超出该题 `word_limit` 的写法从接受集移入拒绝集。上限逐题来自输入，有 5 种文案，其中两种（`ONE WORD ONLY` / `NO MORE THAN TWO WORDS`）**不允许任何数字** |
| R6 卷面已供单位 → 剔除重复单位的写法 | `£ ___ per night` 里 `128 pounds` 拒；但 `£128` 保留（符号重复是考生近乎普遍的无害习惯，剔除会误扣分） |
| R7 卷面没写出单位 → 剔除裸数字 | `covers ___ in total` 里裸 `3` 拒，`3 nights` 收；`for ___ nights` 里裸 `3` 收。判据是 target 的单位词是否出现在 carrier 里 |

按 `answer_category` 展开：

| 类目 | 展开 |
|---|---|
| `person_name` / `contact` | 只有 R1 |
| `document` | 字母数字交界处允许插一个空格：`HM62` → `HM62`, `HM 62` |
| `date` | `14 September` → 6 种：`14th September`, `September 14`, `September 14th`, `14 Sept`, `14/9`（不生成美式 `09/14`、不生成年份） |
| `time` | `:` 与 `.` 互通，`a.m.` 四种写法互通；**不**生成 24 小时制换算与英文读法 |
| `duration` / `quantity` | `3 nights` → `3 nights`, `three nights`, `3`, `three`（再经 R7 裁剪） |
| `price` | `128` → `128`, `£128`, `128 pounds`, `£128.00`；币种从卷面 / 引文语境判定 |
| `preference` / `option` / `service` / `requirement` | 原样 + 冠词变体 + **人工登记**的区别性成分（`DISTINCTIVE`） |
| 其他（`location` / `facility` / `job_title` …） | 只生成原样写法并写进 `review.txt` |

**仍然需要人工的部分**都写进 `review.txt`：短语的区别性成分（`DISTINCTIVE`）、词类答案的分类名
扩展（`EXTRA`）、无展开规则的类目。宁缺勿滥——少一个变体是漏判（考生可申诉），多一个变体可能把
干扰项判成对（静默错误，永远发现不了）。

**已知限制（投递层职责，不在 QTI 里解决）**：`mapEntry` 是精确比对，首尾空白、多空格折叠、
Unicode 归一化都不裁剪，各引擎行为不一。建议下游导入后在投递层加一道归一化，并用
`reject_candidates.json` 做一次判分回归。

## 4. 准入门禁

| 门 | 检查 |
|---|---|
| G1 | 上游审核：`ok=true`；`status ∈ {PASS, WARNING}`；`validation.ok` 且 `errors` 为空；`review.content_review_readiness = READY_FOR_HUMAN_REVIEW`；`review.question_qc_status ∈ {PASS, WARNING}`；CRITICAL / MAJOR 计数为 0 |
| G2 | 题目与脚本互证：`cross_check.ok`；`hard_defects` / `leakage` / `equally_supported_rivals` 为空；`compared = items 条数 = 题数`；每题 `outcome ∈ {agree, anchor_adjacent}`；**两侧答案必须一致**（容大小写 / 尾部标点 / 领头货币符号 / 逐字符拼读三类书写差异） |
| G3 | 题号闭合：1..N 连续无重复；`answer_key`、`evidence` 与题面题号一致；`blank` 内联题号 = `number`；`canonical` = `cross_check.writer_answer` |
| G4 | 分组闭合：`group_id` 无重复；`instructions` 与 `groups` 集合相等；`question_range` 解析后等于该组实际题号；`layout ∈ {form, note, table}` |
| G5 | 字数上限自洽：文案可解析；与题头逐题一致；`canonical` 与 `alternatives` 都在上限内 |
| G6 | 结构块内联题号不越组 |

**修订版本怎么过门禁。** 批注修订产出的版本（`_question_versions/…`）把 `package` 放顶层、审核
结论收在 `quality` 下，没有 `ok`。`service.normalize_document` 把它翻成原始交付文档的形状再走
同一条门禁（`ok` 取自版本的 `status == "ready"`），而不是给修订版本开旁路——修订同样要满足
两侧答案一致，否则导出去的是一个未经互证的 `correctResponse`。

## 5. 校验与测试

```bash
python3 -m qti_export.fetch_schemas          # 一次性：镜像 XSD 到 qti_export/schemas/mirror（需外网）
python3 qti_export/tests/run_tests.py        # 有镜像时含 XSD 校验；没有则 XSD 那条 SKIP 并说明
python3 web/tests/run_tests.py web/tests/test_qti_export_route.py
```

XSD 校验为什么必须走本地镜像：libxml2（lxml / xmllint）不支持 HTTPS，而 QTI 2.2.4 的 `xs:import`
全指向 `https://`，不镜像就会 "failed to load external entity"，并把未解析的 `xml:lang` /
MathML / SSML 引用报成 parser error。`QTI_XSD_MIRROR=/path` 可以指向别处镜像好的目录。

## 6. 不可映射项

| 需求 | QTI 2.2 能力 | 处置 |
|---|---|---|
| 听力音频 | `qh5:audio` | 本流水线不产出交付音频，item 只放 `[audio pending: supplied separately]` 占位段，由下游挂载。真要内嵌时记住 `@controls` 是 `xs:boolean`，写 `controls="true"` 而不是 HTML 的 `controls="controls"` |
| 音频只播一次 | 无此属性 | 交给投递引擎 |
| `NO MORE THAN TWO WORDS AND/OR A NUMBER` | 无原生约束 | 写进题头文字；容错集不收录越限写法（R5）。QTI 层无法阻止输入，只能判分时不给分 |
| 读题窗口 / 30 秒检查时间 | `timeLimits` 只在 test / section 级 | 由音频自身承载 |
| `signposts` 出题意图 | — | 不进卷面也不进 metadata |
