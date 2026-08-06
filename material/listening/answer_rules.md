# Answer Rules (`AR-xxx`)

## 适用范围

检查标准答案、可接受变体、评分表达和作答限制。这里的“答案”包括文字、数字和选项标识。

| Rule ID | 要求 | 默认严重度 | 核验要点 |
| --- | --- | --- | --- |
| `AR-001` | 每个 canonical answer 必须被脚本中的决定性证据直接支持。 | `MAJOR` | 引用具体 turn/time。 |
| `AR-002` | canonical answer 必须满足题目声明的最大词数、数字数和答案数量。 | `MAJOR` | 按题面规则计数。 |
| `AR-003` | 要求“使用录音中的词”时，尤其是 `ONE WORD ONLY` completion，canonical answer 必须与决定性 Script evidence 中一个完整 orthographic token 的词形一致，不得进行派生、同义替换或语法改写；例如 Script 的 `Educational` 不能以 `education` 作 key。blank 周边题面的同义替换不改变这一答案要求。 | `MAJOR` | 按大小写不敏感的完整 token 对照 Script evidence 与 canonical answer；未找到完整同形 token 即失败。 |
| `AR-004` | accepted alternatives 必须与 canonical answer 在本题语境中语义等价且满足全部限制。 | `MAJOR` | 对每个 alternative 独立验证。 |
| `AR-005` | 不得接受脚本明确排除、修正或仅作为干扰项出现的答案。 | `MAJOR` | 追踪最终决定。 |
| `AR-006` | 可预见且不改变意义的英式/美式拼写变体应一致处理；若只接受一种，必须有项目依据。 | `MINOR`；导致误判时 `MAJOR` | 如 organisation/organization。 |
| `AR-007` | 非语义性大小写或句末标点通常不应改变得分；专有名词要求必须一致。 | `MINOR`；导致误判时 `MAJOR` | 核对评分系统能力。 |
| `AR-008` | 数字、日期、时间、货币和单位的文字/数字形式应一致处理，且不得引入歧义。 | `MAJOR` | 如 15/fifteen、12 May。 |
| `AR-009` | 单复数必须与脚本事实及题干语法一致；不可互换时不得都列为 accepted。 | `MAJOR` | 数量、冠词和谓语。 |
| `AR-010` | 多答案题必须定义答案数量、是否有序和计分粒度；key 不得漏项或多项。 | `MAJOR` | `canonical` 数组和 selection count。 |
| `AR-011` | 选择/匹配题的答案必须使用存在且唯一的 option ID，而非易变的显示文本。 | `MAJOR` | 解析 option reference。 |
| `AR-012` | 在作答限制和语境内，正确答案应唯一；多个等价表述只能作为明确 alternatives。若决定性证据枚举两个或以上同层级候选词，必须把每个候选逐一代回 blank carrier，且只能有一个在语法和语义上成立；仅检查 canonical answer 能否填入不构成唯一性证明。 | `MAJOR` | 尝试构造所有合理答案；对枚举/list evidence 记录全部同层级候选、逐项代入结果和唯一保留理由。 |
| `AR-013` | 每道计分题都必须有完整 answer key；不得使用 TBD、空字符串或审稿注释。 | `MAJOR` | 检查 key 完整性。 |
| `AR-014` | 连字符复合词按一个词计，但必须作为完整 token 保留；若决定性 Script evidence 只有 `eco-tourism`，不得把 `tourism` 当作该 token 的一词答案，也不得在 carrier 中预填 `eco-` 后只要求考生填写片段。只有同一决定性证据另有独立出现的完整 standalone token 时，才可按该独立出现审查。缩略形式不应作为待测答案。 | `MAJOR` | 联合字数限制执行完整 token 扫描，并检查 carrier 没有拆分 Script 的连字符词。 |
| `AR-015` | canonical 与 alternatives 内部不得重复，规范化后也不得冲突。 | `MINOR` | 大小写、空格、标点归一化去重。 |
| `AR-016` | 答案不得包含题干已经给出的词，若重复后会造成语法或语义错误。 | `MAJOR` | 将答案实际代入题干。 |
| `AR-017` | 评分键不得接受比脚本证据更宽泛或更狭窄、从而改变命题目标的表达。 | `MAJOR` | 比较语义粒度。 |
| `AR-018` | Matching answer key 必须遵守题面声明的 reuse contract：non-reuse 题组的 option ID 必须全部不同；允许复用的题组可以重复 option ID，但每道题仍须按 `QR-042` 有唯一、独立的决定性支持。 | `MAJOR` | 从最终 Word instruction、answer key 和 schema v5 audit 交叉核对 `reuse_allowed`、重复 option ID 与逐题唯一证据。 |

## 词数计算基线

除非用户规范另有定义：

- 连字符复合词计 1 个词，但答案必须保留整个复合 token，不得只取其中一段；
- 数字计入允许的 number 数量，不按内部位数拆分；
- 空格分隔的普通词分别计数；
- 斜杠连接的两个备选不得视作一个答案；
- 冠词和介词若由考生填写，计入词数；
- contraction 不作为目标答案使用。

报告必须说明采用的计数规则，不能只写“超出字数限制”。
