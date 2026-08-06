# Script Rules (`SR-xxx`)

## 适用范围

用于审查录音脚本作为“可被听见的语言材料”是否自然、可理解、内部一致，并能公平承载题目答案。仅凭脚本无法判断的问题应交给 `AL-xxx`。

| Rule ID | 要求 | 默认严重度 | 核验要点 |
| --- | --- | --- | --- |
| `SR-001` | 对话或独白必须符合真实口语表达，避免明显书面化、机械枚举或为塞入答案而拼接的句子。 | `MINOR` | 连贯性、话轮长度、回应关系、口语连接。 |
| `SR-002` | 主题、人物关系和交际目的必须符合对应 Part 的语境。 | `MAJOR` | 与 `SC-004` 联合检查。 |
| `SR-003` | 每个 turn 必须有明确且稳定的 speaker；同一 speaker 的身份、角色和称谓不得无解释变化。 | `MAJOR` | speaker ID、姓名、角色一致性。 |
| `SR-004` | 关键信息必须能仅通过音频理解；不得依赖未提供的手势、屏幕文字或“这里/那个”等无法解析的指代。 | `MAJOR` | 检查 deictic reference 和视觉依赖。 |
| `SR-005` | 每道题的正确答案必须在脚本中有明确、可定位的决定性证据。 | `MAJOR` | 与 `AR-001`、`AL-002` 联合检查。 |
| `SR-006` | 决定性证据不得同时同等支持两个互斥答案，除非题目明确要求多选。 | `MAJOR` | 检查同义陈述、未消解的备选方案。 |
| `SR-007` | 答案信息可被自然改述，但脚本不得通过反常重读、拼写或元话语提示“这是答案”。 | `MAJOR` | 识别 answer-signposting 和不自然强调。 |
| `SR-008` | 扫描选择题/匹配题的错误选项是否在音频中被实质激活，并被排除、修正、降级或证明为与题干关系不符。对历史原件和历史 matching 额外 options，`not_supported`、空 evidence 或只在题面出现必须记录为 Script Scan Warning；不得单凭这一项决定历史题目或整套是否通过。对新生/重做的 single choice 和 multiple-selection，`QR-036` 要求使用实质激活的 options，`QR-039` 还要求错误项证据在答案前且最多提前 3 句；对新生/重做 matching，`QR-041` 要求包括未使用项在内的全部 options 有明确 Script 来源与自然显示措辞。任一新改题缺口都属于题目设计失败而不是可留到交付的 warning。正确答案的明确证据、唯一性以及题面互斥/可猜性仍分别由 `SR-005`/`SR-006`、`AL-002`、`QR-006`/`QR-008` 硬性判定。 | `ADVISORY_WARNING`（历史扫描）；新改 choice 按 `QR-036`/`QR-039`、新改 matching 按 `QR-041` 为 `MAJOR` | 为每个 option 记录最小 Script source、题面 display text、状态、自然度、相对答案位置和句距；每个题组报告 activation metrics，并依次审查是否属于新改题 hard gate。 |
| `SR-009` | 人物、日期、时间、价格、地点、数量及因果关系必须内部一致；如发生修正，最终值必须清楚。 | `MAJOR` | 建立事实表并核对重复出现。 |
| `SR-010` | 难度应主要来自自然语速下的词汇、信息密度、改述和语篇关系，而非含混语法或异常冷僻知识。 | `MINOR`；影响作答时 `MAJOR` | 区分有效难度与构念无关难度。 |
| `SR-011` | 不得包含仇恨、羞辱、歧视、露骨色情、无必要的血腥暴力或鼓励危险/违法行为的内容。 | `MAJOR`；严重安全风险为 `CRITICAL` | 同时考虑教育场景的必要、中性提及。 |
| `SR-012` | 人名、电话、地址、邮箱、账号等应为虚构或明确授权的数据，不得暴露真实私人信息。 | `CRITICAL` | 识别直接或可组合识别的个人信息。 |
| `SR-013` | 不得大段复制受版权保护的非授权材料；引用或改编必须有合法来源记录。 | `MAJOR` | 检查 `source_attribution` 和异常熟悉段落。 |
| `SR-014` | 文本必须可自然朗读；缩写、符号、公式、网址和特殊字符应有明确读法。 | `MINOR`；影响答案时 `MAJOR` | 逐项模拟语音实现。 |
| `SR-015` | 录音正文不得出现题号、标准答案、评分规则或“考生应选……”等制作元信息。 | `MAJOR` | 搜索 exam-production leakage。 |
| `SR-016` | 话轮之间必须有可追踪的逻辑关系；突然换题、缺失前提或无法解析的代词不得影响理解。 | `MINOR`；影响答案时 `MAJOR` | 邻接对、指代、主题转换。 |
| `SR-017` | 若提供 stage direction，它不得作为考生唯一可用的答案证据，除非该信息会被真实音频表达。 | `MAJOR` | 区分可听内容与制作备注。 |
| `SR-018` | 拼读、重复或自我修正必须自然且保持最终信息明确。 | `MINOR`；造成多答案时 `MAJOR` | 特别检查姓名、邮编、号码和日期。 |
| `SR-019` | 扫描录音是否仍使用旧版 IELTS Listening 制作话术：Part 1 开头 example/重复播放说明、任意可听位置使用 `Section 1–4` 而非 `Part 1–4`（包括 `end of Section`、`turn to Section`）、或明确暗示录音会播放两次。此类问题需要改动可听 Script，当前只提示，不参与题目通过判断。 | `ADVISORY_WARNING` | 扫描 example/replay 固定话术、所有 `Section 1–4` 语境和明确 second-play cues，并给出 Part/turn；不要把普通的 once-only 说明误报为“播放两次”。 |
| `SR-020` | 扫描高显著度答案承载事实是否在局部 evidence window 内无必要地重复两次以上，导致作答点被过度提示。扫描范围包括最终 canonical answers、reviewer-flagged facts、曾用答案和候选答案事实；不能因为题目后来改测别处就让 date/time/price/dietary requirement 等重复警告消失。重复本身不改变 canonical answer 的正确性；当前作为 Script Scan Warning，除非重复造成两个最终值或歧义，此时仍按 `SR-006`/`SR-009` 阻断。 | `ADVISORY_WARNING` | 按大小写、数字/口语数字、时间/日期和基本标点归一化；记录 normalized fact、来源类别、出现次数、最小 turn 范围，并区分 confirmation、spelling、correction、necessary repetition 与 unnecessary repetition。 |
| `SR-021` | 可听 Script 只能在用户或内容负责人明确授权后修订。获授权时必须创建新的 Script DOCX，不得覆盖源文件；保存逐 turn diff、源/目标 SHA-256、受影响题号/答案/narrator windows 和授权记录。Script 一经改变，既有音频必须标为 `AUDIO_REBUILD_REQUIRED`，直至新音频与修订 Script 的身份被验证。 | `MAJOR`（未授权改动、覆盖源文件或把旧音频当作已同步）；已授权但待重录为制作状态 | 先核验授权，再比较 Script 哈希和 audible turns；输出 revision manifest，并把 `script_production_status` 与题目 QC 状态分开报告。 |
| `SR-022` | 涉及停顿、截断、音量、语速、重音、机器感、情绪或语音语调的 reviewer comment，不能仅凭 Question/Script Word 正文关闭。必须绑定实际音频资产身份和时间点进行听音或波形核验；缺少对应音频时标记 `deferred`，记录 reason、owner、target version、`verification_kind=audio` 和 timestamp，并通过 `script_production_status=WARNINGS_PRESENT` 单独报告。 | `ADVISORY_WARNING`；若把未核验音频问题误称已解决或影响答案可听性，则为 `MAJOR` | 核对 audio SHA-256/版本、时间点和听音证据；无音频时不得用文字推断 pause/prosody/volume 已修复。 |

## 审查输出

每个 script finding 至少定位到 `part_number` 和 `turn_id`。有时间戳时提供
`start_ms`/`end_ms`。跨多个 turn 的问题应列出最小必要 turn 集合。
`SR-008`、`SR-019`、`SR-020`、`SR-022` 的结果统一放在 `script_scan_warnings`；即使存在，
`question_qc_status` 仍可报告 `AUTOMATED_PASS`。`SR-021` 另行驱动
`script_production_status`，不得用题目通过状态暗示录音已可交付。
