# Alignment Rules (`AL-xxx`)

## 适用范围

检查 `script -> question -> answer key -> asset` 的逐题可追踪性。Alignment 审查必须覆盖每道适用题目，不能只抽样。

| Rule ID | 要求 | 默认严重度 | 核验要点 |
| --- | --- | --- | --- |
| `AL-001` | 每道计分题必须恰好映射到一个可评分响应定义；每个 key 必须映射回存在的 question。 | `MAJOR` | 双向引用和 orphan 检查。 |
| `AL-002` | 每个 canonical answer 必须有最小、直接、可定位的脚本 evidence。 | `MAJOR` | turn/time + quote。 |
| `AL-003` | 决定性 evidence 的顺序必须与题号顺序一致。 | `MAJOR` | 比较首个决定性 evidence 位置。 |
| `AL-004` | 题干和选项中实际使用的改述必须保持 Script 原意、范围、极性、时态、主体、因果、比较、情态和答案粒度，并使用自然、常用、可从局部证据定位的表达；不得引入 Script 未出现的对象或语境，也不得仅为制造词面差异而换成更生僻、抽象或迂回的措辞。应删除题面中无必要的实义词重复，但专名、固定术语和非答案 Script signpost 可以原样保留；保留项须证明不会形成 canonical-answer 复现、正确项独有镜像或 cross-item cue。`QR-024` 不要求 matching 强制改述；matching 保留 Script signpost 时仍须通过 `QR-008`/`QR-041`。 | `MAJOR` | 对齐 source phrase 与 question phrase，记录 exact/partial/valid paraphrase/invalid、题干词汇重复结果与 exact/signpost 理由；对 matching 记录 `paraphrase_not_required`、Script provenance 和 no-audio 结果。 |
| `AL-005` | 每个干扰项必须在听前具有场景可信性，且不得与正确答案获得同等支持。历史原件中 `not_supported`、空 evidence 或仅在题面出现的 distractor 按 `SR-008` 进入 Script Scan Warning；但新生/重做的 single choice 和 multiple-selection 必须按 `QR-036` 改用对应 narrator 窗口内可实质激活、排除或纠正的 options，并按 `QR-039` 保证干扰项证据在答案前且最多提前 3 句；新生/重做 matching 的全部 options（含未使用项）必须按 `QR-041` 有明确 Script 来源并使用自然题面措辞。若题面本身可用常识、语法或词汇镜像稳定排除干扰项，仍按 `QR-006`/`QR-008` 阻断。 | `MAJOR`（可信性/唯一性、新改 choice 的激活/位置覆盖或新改 matching 的 Script-first 缺口）；历史原件的纯音频激活缺口为 `ADVISORY_WARNING` | 记录 plausible-before-listening 理由、最小 Script source、题面显示措辞、状态、自然度、相对答案位置和句距；每题组报告 activation metrics，并区分历史扫描与新改题 hard gate。 |
| `AL-006` | 题目归因的人物必须与实际说话者一致。 | `MAJOR` | speaker、opinion、proposal ownership。 |
| `AL-007` | 时间戳或 turn reference 必须包含实际证据，不得只指向相邻内容。 | `MINOR`；无法复核时 `MAJOR` | 检查 quote 是否位于定位范围。 |
| `AL-008` | 答案所需前提必须在作答点前已建立，不得依赖录音后续对前文的无提示推翻。 | `MAJOR` | 信息状态和最终修正。 |
| `AL-009` | 脚本、题干、选项、key 和 asset 中的姓名、数字、单位、地点及术语必须一致。 | `MAJOR` | 建立跨制品事实表。 |
| `AL-010` | 所有计分题都必须完成 alignment 记录；不得有未审查题目却宣称 full coverage。 | `MAJOR` | `coverage.reviewed_question_ids` 对比全集。 |
| `AL-011` | 正确答案不得依赖录音外的世界知识、常识或视觉猜测。 | `MAJOR` | 只使用提供材料重做题目。 |
| `AL-012` | 每个 accepted alternative 必须由相同证据支持，并保持题目要求的粒度。 | `MAJOR` | alternative-by-alternative proof。 |
| `AL-013` | plan/map/diagram 题的口头方向、起点、路径和目标必须与 asset 的空间关系一致。 | `MAJOR` | 按指令模拟路径。 |
| `AL-014` | 相邻题目的题干、carrier、标题、无 blank signpost 或答案不得为当前题提供非预期的答案提示。Completion 题组还须按 `QR-040` 将同组全部考生可见文本与每个 canonical answer（含简单屈折变化）逐题比对。 | `MAJOR` | 隐藏 Script/answer key 执行 group-scope cross-item cue 检查；不得只检查 blank 单句。 |
| `AL-015` | completion 题将答案代入后，完整文本必须同时符合题意、脚本事实和语法。 | `MAJOR` | rendered question 检查。 |
| `AL-016` | 若脚本包含自我修正或多次提及，key 必须对应清楚的最终有效信息。 | `MAJOR` | 时间顺序与 discourse marker。 |
| `AL-017` | 当 narrator 明确划分题号窗口时，每道题的决定性证据与 annotated-script 标签必须落在该题号所属的窗口内；不得在考生被提示查看/作答该题之前播放完决定性证据。无 narrator 拆分时不适用本窗口限制。 | `MAJOR` | 比较 Q 标签行与对应 `answer questions X to Y` cue、下一窗口 cue 的行序。 |
| `AL-018` | carrier/stem 所断言的主体、对象、地点、时间和关系必须与 canonical answer 的决定性 evidence 属于同一事实命题；不得把一个例子、地点或对象的题面框架与另一个例子、地点或对象中的答案词拼接。把 Q 标签放在答案词所在行不能替代命题级对齐。 | `MAJOR` | 每题记录 `carrier_entity`、`evidence_entity`、`proposition_relation` 和 `proposition_alignment_result`；任一关键实体或关系跨事实拼接即失败。 |

## 逐题审查记录

内部审查时为每题建立：

| 字段 | 内容 |
| --- | --- |
| `question_id` | 稳定题目 ID |
| `expected_response` | canonical + alternatives |
| `evidence_location` | part、turn、time |
| `evidence_quote` | 最小直接引文 |
| `paraphrase_relation` | exact / partial overlap / valid paraphrase / invalid / paraphrase_not_required；matching 可使用最后一项 |
| `distractor_resolution` | 每个选项的音频状态、最小 Script source、题面显示措辞、自然度、相对答案位置和句距；历史题/matching 额外 option 的 `not_supported` 进入非阻断 Script Scan Warning，新生/重做 choice 按 `QR-036`/`QR-039`、新生/重做 matching 按 `QR-041` 阻断 |
| `proposition_alignment` | `carrier_entity`、`evidence_entity`、主体/地点/时间/关系是否来自同一事实命题，以及 `PASS`/`FAIL` 理由 |
| `constraint_check` | 字数、数量、格式 |
| `result` | pass / finding IDs |

该工作表可以不全部输出，但所有失败项必须转为 report finding，coverage 必须反映逐题完成情况。
