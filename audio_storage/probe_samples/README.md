# Polly 阶段 0 探针样本

生成于 2026-07-28，us-east-1，engine=neural，language-code=en-GB。

## 已客观判定（无需试听）

| 探针 | 时长 | 结论 |
|---|---:|---|
| `probe_nobreak.mp3` | 2.184s | 基线 |
| `probe_break.mp3` | 2.976s | **末尾 800ms `<break>` 保留了 792ms** → 停顿可烘进音频 |
| `wpm_default.mp3`（未随附，见下） | 48.72s / 125 词 | **默认 153.9 WPM** → 达到规范 140 WPM 需 `rate="91%"` |

## 仍需人工试听的两项

### 1. `spelling-say-as` —— 拼读串怎么读

同一句 `The surname is P-A-T-E-L.`，三种写法：

| 文件 | 写法 | 时长 |
|---|---|---:|
| `spell_bare.mp3` | 裸文本 `P-A-T-E-L` | 3.648s |
| `spell_hyphen.mp3` | `<say-as interpret-as="characters">P-A-T-E-L</say-as>` | 3.336s |
| `spell_sayas.mp3` | `<say-as interpret-as="characters">PATEL</say-as>` | 2.304s |

**听什么**：三者是否都逐字母朗读？有没有把连字符读成 "dash"？
裸文本比 say-as 长 1.34s，差异明显，需要确认长出来的是"更清晰的停顿"还是"多读了 dash"。

判定影响 `ssml.py` 的 `strip_spelling_hyphens` 与 `spelling_say_as` 是否保持默认开启。

### 2. `digits-zero` —— 长数字串怎么读

同一句 `My number is 04196570156.`：

| 文件 | 写法 | 时长 |
|---|---|---:|
| `digits_bare.mp3` | 裸文本 | 3.600s |
| `digits_sayas.mp3` | `<say-as interpret-as="digits">` | 3.816s |

**听什么**：
- 裸文本有没有被读成基数词（"four billion one hundred ninety-six million…"）？若是，则必须强制加标记。
- 开头的 `0` 读 "zero" 还是 "oh"？英式听力惯用 "oh"，这条是产品判断而非技术判断。

两者仅差 0.22s，倾向裸文本也在逐位读，但必须听过才能确认。

## 播放

```bash
afplay audio_storage/probe_samples/spell_bare.mp3
open audio_storage/probe_samples/          # 或在 Finder 里逐个播放
```

---

# 第二轮：语速与真题惯例（2026-07-28）

第一轮试听后确认**两条 say-as 规则都不需要**，真正要调的是语速。第二轮验证语速取值。

## 拼读（裸文本 `S-U-T-C-L-I-F-F`，8 字母）

| 文件 | rate | 时长 | 每字母 |
|---|---|---:|---:|
| `a1_spell_100.mp3` | 默认 | 6.41s | ~476ms |
| `a2_spell_91.mp3` | **91%（选用）** | 6.84s | ~530ms |
| `a3_spell_85.mp3` | 85% | 7.18s | ~572ms |
| `b1_double.mp3` | 91% | 4.54s | 真题 `S-U-T-C-L-I, double F` 写法，省 2.3s |

## 数字（裸文本 `04196570156`，11 位）

| 文件 | 写法 | 时长 |
|---|---|---:|
| `c2_num_91.mp3` | 91%，不分组 | 4.49s |
| `c4_num_group.mp3` | 91% + 逗号分组 `04196, 570, 156` | 5.90s |

逗号分组多出 **1.42s 停顿**，正是解决「开头 oh 听不清」的手段——真题就是这么做的
（`07958, 8472 double 2`）。建议生成 skill 对长号码采用逗号分组。

## 最终管线产物

`final_turn8.mp3` / `final_turn12.mp3` 由 `ssml.render_turn()` 的真实输出合成
（rate 91% + 600ms 尾部停顿），取自 43 轮参考脚本的拼读轮与电话号码轮。
**这是走完整管线的成品音质，请以这两条为准做最终确认。**
