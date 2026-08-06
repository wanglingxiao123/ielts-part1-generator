# Severity and Overall Status

## 1. 严重度枚举

| Severity | 定义 | 典型影响 | 发布处理 |
| --- | --- | --- | --- |
| `CRITICAL` | 破坏测试完整性、导致大范围答案泄露/不可评分，或包含高风险违法、安全、隐私问题 | 多题或整套测试失效；无法可信评分 | 必须阻断 |
| `MAJOR` | 导致一道或多道题无唯一可证答案、答案错误、结构不合规或关键输入缺失 | 考生可能被错误评分；核心格式不成立 | 必须阻断 |
| `MINOR` | 不改变预期正确答案，但降低清晰度、自然度、一致性或专业质量 | 可理解但有摩擦或局部歧义 | 不阻断内容人工 review 草稿；正式成品发布前修复或按项目接受风险 |
| `INFO` | 非缺陷性说明、范围备注或可选优化 | 不影响合规和评分 | 不阻断 |
| `ADVISORY_WARNING` | 用于独立的 Script/音频制作扫描提示，例如旧版 example/Section 话术、答案词过度重复、历史题/历史 matching 额外 option 未充分激活，或缺少实际音频而无法核验的停顿/截断/音量/语音语调 comment | 供后续 Script 或音频制作排期；不证明历史题面失败。新生/重做 choice 的激活/近距缺口按 `QR-036`/`QR-039`，新生/重做 matching 的 Script-first、自然措辞、reuse contract 或听后唯一性缺口按 `QR-041`/`QR-012`/`QR-033`/`QR-042` 直接改题，不适用此豁免 | 当前不参与历史题 PASS/FAIL/WARNING 状态计算，也不使 `--strict` 失败；新改题 hard gate 失败仍按 `MAJOR` 阻断，把未核验声学问题误称已解决则仍可按 `SR-022` 阻断 |

## 2. 定级原则

1. 按实际影响定级，不按修改工作量定级。
2. 同一问题影响多题时，可提高严重度一级，但不得超过其真实影响。
3. 仅有“更自然的写法”且原文正确清晰时，最多为 `INFO`。
4. 缺失数据若阻止所选审查模式，至少为 `MAJOR`；若只是可选元数据，使用 `INFO`。
5. 题目存在两个同等可支持答案，通常为 `MAJOR`。
6. 标准答案完全无脚本证据，通常为 `MAJOR`；若系统性发生，升级为 `CRITICAL`。
7. 考生可见内容直接暴露答案，单题为 `MAJOR`，系统性泄露为 `CRITICAL`。
8. 拼写、标点或格式问题只有在导致答案被误判时才是 `MAJOR`；否则一般为 `MINOR`。
9. 新生/重做一词 completion 在存在同等有效低风险候选时仍保留可避免的高拼写负担，默认按 `QR-043` 为 `MINOR`；若单题已使公平评分不可靠，或多个项目累计后明显把构念转向非预期正字法难度，则为 `MAJOR`。低词频或某个自动词表信号本身不决定严重度。
10. 新生/重做 Part 4 保留了一个经人工确认为 V-ing、且同一局部决定性证据窗口内确有同等有效非 V-ing 一词名词/形容词替代项时，默认按 `QR-044` 为 `MINOR`；单题已形成明显可避免的拼写/形态处理负担，或同一 Part 多次发生并累计导致构念偏移时为 `MAJOR`。仅凭 `-ing` 后缀或出现次数不自动升级；词汇化名词、无同等有效替代项和有理由的固定技术术语不形成 finding。
11. 字体、字号、颜色、边框粗细、留白、段间距、视觉一致性、非破坏性分页等纯样式问题默认不得升级为内容阻断项。只有样式或渲染问题造成必需文字、题号、选项、blank、answer 或 evidence 被隐藏、遗漏、错序、无法辨认，或使 reviewer 无法可靠判断内容时，才按实际影响定为 `MAJOR` 并阻断内容人工 review。

## 3. 分离状态模型

题目质量、录音制作、内容 review 可用性和视觉完成度必须分开报告：

- `question_qc_status`：按未解决的 `CRITICAL`/`MAJOR`/`MINOR` findings 计算；兼容字段 `overall_status` 必须与其相同。
- `script_production_status`：只描述 Script/音频制作状态，取值为 `READY`、`WARNINGS_PRESENT`、`REVISION_AUTHORIZED` 或 `AUDIO_REBUILD_REQUIRED`。

`script_scan_warnings` 为空且 Script 未修订时为 `READY`；存在 `SR-008`/`SR-019`/`SR-020`/`SR-022` 时为 `WARNINGS_PRESENT`。已明确授权但尚未修改 Script 时可为 `REVISION_AUTHORIZED`；一旦可听 Script 发生变化，在新音频与目标 Script 身份核验前必须为 `AUDIO_REBUILD_REQUIRED`。

### 3.1 内容 review 与视觉状态

交付阶段必须显式区分，不能把“可供人工审核”与“最终视觉成品”合并为同一门槛：

- `content_review_readiness`：`READY_FOR_HUMAN_REVIEW` 或 `BLOCKED`。当请求范围内的题目、答案、证据/标签和必要说明已经实质存在，candidate DOCX 可打开且 reviewer 能可靠读取这些内容时，可以为 `READY_FOR_HUMAN_REVIEW`。这只说明内容可进入人工审核，不表示 `question_qc_status=PASS`，也不表示内容已获人工批准。
- `visual_qc_status`：`NOT_RUN`、`PENDING`、`PASS` 或 `FAIL`。字体、边框、间距、非破坏性分页和视觉一致性等问题只进入这一状态，不改变 `content_review_readiness`；但若视觉问题隐藏、遗漏、错序或使必需内容不可辨认，则同时把 `content_review_readiness` 设为 `BLOCKED`。
- 纯样式问题必须记录在独立的 `visual_findings` 中，只驱动 `visual_qc_status`，不得混入用于计算 `question_qc_status` 的 content findings。若同一视觉问题已经影响内容可访问性、答案边界、题号顺序或可靠 review，则另建 `QR-015` 等 content finding，并按实际严重度进入 `question_qc_status`。
- 内容人工 review 草稿允许 `visual_qc_status=NOT_RUN|PENDING|FAIL`，但必须清楚列出已知视觉问题，不得称为 production-ready/final。
- production-ready/final 声明要求适用的内容门槛通过，并且 `visual_qc_status=PASS`。不得用内容 review 已完成替代最终逐页视觉 QA。

工作顺序以内容为先：先形成并交付 `READY_FOR_HUMAN_REVIEW` 的内容草稿，再根据人工内容反馈修订；样式统一和最终视觉 polish 不得占用或延迟首轮内容 review，除非当前版面已经妨碍内容读取。

### 3.2 Question QC 算法

按未解决 finding 计算：

```text
if CRITICAL > 0 or MAJOR > 0:
    question_qc_status = FAIL
else if MINOR > 0:
    question_qc_status = WARNING
else:
    question_qc_status = PASS
```

`INFO` 和 `ADVISORY_WARNING` 不改变状态。`summary.counts` 必须统计全部
content findings，包括 `INFO` 和 `ADVISORY_WARNING`；`visual_findings` 使用独立的
`summary.visual_counts`，不得改变 `question_qc_status`。`ADVISORY_WARNING` 应另列在
`script_scan_warnings`，不得混入阻断 finding。`--strict` 只按题目 errors/
warnings 决定退出码，不因 `script_production_status = WARNINGS_PRESENT` 失败；
`AUDIO_REBUILD_REQUIRED` 则必须阻断任何“音频已同步/可交付”的制作声明。

## 4. Finding 状态

- `open`：问题存在且未修复；
- `resolved`：当前输入已包含可验证的修复；
- `waived`：经明确授权接受风险，必须填写 `waiver_reason`；
- `not_applicable`：规则被评估但不适用，不应作为缺陷计入阻断状态。

不得自行把问题标为 `waived`。只有输入中存在明确豁免信息时才可使用。
