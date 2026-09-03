#!/usr/bin/env python3
"""题目包 JSON（`_questions/{material_id}.json`）的读取层与准入门禁。

这是 **新输入契约**。它与旧的 `blueprint` schema 没有兼容关系：顶层不再有
`verdict` / `scenario_key` / `listening_material_parts`，取而代之的是

    label / package{reference,test_package,material_id,question_face,answer_key,evidence}
    review / cross_check / validation / status / ok / advisories

两个结构性变化会一路影响到 emitter：

1. **卷面文字进了输入。** `question_face.questions[]` 直接给出
   `carrier_before` / `blank` / `carrier_after` / `blank_position`，
   `groups[].structure` 给出行标签。旧方案里那本必须人工维护的题面词典
   因此失去存在理由 —— 逐字段映射见 qti_export/README.md §2。
2. **字数上限进了输入，且逐题不同。** `answer_key[].word_limit` 有 5 种文案，
   `numeral_allowance` 有 0 和 1 两种取值。转换器不能再把「两词 + 一个数字」
   写成常量，必须逐题读。

门禁只做**一致性**判断，不做内容判断：内容质量由上游 review / cross_check /
validation 三段负责，本模块检查它们是否自称通过，以及各段之间的题号、分组、
字数上限有没有互相矛盾。

单独运行可对一批文件跑门禁：

    python3 -m qti_export.questions_input path/to/*.json
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

#: 支持的版式。note / table 的 emitter 在 Stage 5 落地，门禁这里先放行，
#: 因为「输入合法」与「emitter 已实现」是两件事，不该混在同一层拒绝。
SUPPORTED_LAYOUTS = ("form", "note", "table")

#: 题头字数上限文案 → (实词上限, 数字额度)。
#: 这是**输入文案的解析表**，不是本项目的政策 —— 政策已经交还给输入。
WORD_LIMIT_SPECS: dict[str, tuple[int, int]] = {
    "ONE WORD ONLY": (1, 0),
    "ONE WORD AND/OR A NUMBER": (1, 1),
    "NO MORE THAN TWO WORDS": (2, 0),
    "NO MORE THAN TWO WORDS AND/OR A NUMBER": (2, 1),
    "NO MORE THAN THREE WORDS AND/OR A NUMBER": (3, 1),
}

#: 上游把「空格」写成两种形状：`3 ................` 与 `3 __________`。
BLANK_RE = re.compile(r"^\s*(\d+)\s*[.\u2026_\-\u2013\u2014\s]*$")

_RANGE_RE = re.compile(r"^(\d+)\s*[-\u2013\u2014]\s*(\d+)$")
_SINGLE_RE = re.compile(r"^(\d+)$")

#: 一个 token 是否消耗「数字额度」而不是实词额度。
#: 序数词 `14th`、带货币符号的 `£128`、时间 `7.30`、`7pm` 都按数字算 ——
#: 雅思按「是不是一个数」而不是按拼写形态计数，`14th of July` 因此是
#: 「一个数 + 两个实词」，在 TWO WORDS AND/OR A NUMBER 之内。
_NUMERAL_RE = re.compile(
    r"^[£$€]?\d[\d,.:/\u2013\-]*(?:st|nd|rd|th)?(?:\s*)?(?:am|pm|a\.m\.|p\.m\.)?[.,;]?$",
    re.IGNORECASE,
)

CURRENCY_SYMBOLS = "£$€"

#: cross_check 允许出现的非 agree 结论。`anchor_adjacent` 的含义是「两侧答案一致，
#: 只是引文落在相邻的两个 turn」—— 对 QTI 而言进入 correctResponse 的是答案而不是
#: 引文位置，所以它不阻断转换，但必须出现在 needs_review 里并写进 review.txt。
TOLERATED_CROSS_CHECK_OUTCOMES = ("agree", "anchor_adjacent")

#: 逐字符拼读形式：`G-R-E-E-N`、`B-H-2`。要求每段只有一个字符，
#: 所以 `14 June` 不会被误判成拼读。
_SPELLED_RE = re.compile(r"^(?:[A-Za-z0-9][-\u2013 ])+[A-Za-z0-9]$")


class GateFailure(Exception):
    """准入门禁不通过。一次列全部失败项，不在第一条就退出。"""

    def __init__(self, failures: list[str], source: str = "") -> None:
        self.failures = failures
        self.source = source
        head = f"准入门禁未通过（{source}）：" if source else "准入门禁未通过："
        super().__init__(head + "\n" + "\n".join(f"  - {f}" for f in failures))


@dataclass(frozen=True)
class WordLimit:
    """逐题字数上限。`text` 原样进题头文案，两个数值驱动容错集过滤。"""

    text: str
    max_words: int
    numerals: int

    def fits(self, answer: str) -> bool:
        lexical, numerals = count_tokens(answer)
        return lexical <= self.max_words and numerals <= self.numerals


@dataclass
class GateResult:
    """门禁通过后交给 IR 构建层的、已归一化的输入视图。"""

    package: dict
    #: 按题号升序
    questions: list[dict]
    #: 题号 → answer_key 条目
    answers: dict[int, dict]
    #: 文档顺序的分组
    groups: list[dict]
    #: group_id → instruction 条目
    instructions: dict[str, dict]
    #: 题号 → 字数上限
    limits: dict[int, WordLimit]
    #: group_id → 该组题号（升序）
    group_numbers: dict[str, list[int]]
    numbers: list[int]
    #: 题号 → canonical 领头、且 carrier_before 已经印在卷面上的货币符号。
    #: 这类答案填进空格时**不该再带符号**（卷面已是 `£ ___ per night`），
    #: Stage 3 的容错集与 Stage 4 的 emitter 都要读这个字段，避免双写。
    duplicated_currency: dict[int, str] = field(default_factory=dict)
    #: 不阻断转换、但需要写进 review.txt 的项
    advisories: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ 解析工具
def normalize_answer(text: str) -> str:
    """NFKC + 折叠空白 + 去尾部标点 + casefold。用于答案的**一致性比对**，
    不用于判分 —— 判分归一化在 accept_sets 里，口径更严。"""
    s = unicodedata.normalize("NFKC", str(text))
    s = re.sub(r"\s+", " ", s).strip()
    return s.rstrip(".,;:'\u2019").casefold()


def answers_agree(writer: str, auditor: str) -> bool:
    """writer 与 auditor 的答案是否是同一个答案。

    容三种上游书写差异，因为它们不改变答案本身：
      - 大小写与尾部标点（`Breakfast` / `breakfast`、`six weeks'`）
      - 领头货币符号（`£128` / `128`）—— 符号往往已经印在卷面上
      - 拼读形式（`G-R-E-E-N` / `GREEN`）—— 录音里逐字母拼读
    """
    w, a = normalize_answer(writer), normalize_answer(auditor)
    if w == a:
        return True
    if w.lstrip(CURRENCY_SYMBOLS).strip() == a.lstrip(CURRENCY_SYMBOLS).strip():
        return True
    for x, y in ((w, a), (a, w)):
        if _SPELLED_RE.match(x) and re.sub(r"[-\u2013 ]", "", x) == y:
            return True
    return False


def leading_currency(text: str) -> str:
    s = str(text).strip()
    return s[0] if s and s[0] in CURRENCY_SYMBOLS else ""


def parse_word_limit(text: str) -> WordLimit:
    key = re.sub(r"\s+", " ", str(text)).strip().upper()
    if key not in WORD_LIMIT_SPECS:
        raise KeyError(key)
    max_words, numerals = WORD_LIMIT_SPECS[key]
    return WordLimit(text=str(text).strip(), max_words=max_words, numerals=numerals)


def count_tokens(answer: str) -> tuple[int, int]:
    """→ (实词数, 数字数)。空白切分；连字符复合词算一个词。"""
    lexical = numerals = 0
    for tok in re.split(r"\s+", str(answer).strip()):
        if not tok:
            continue
        if _NUMERAL_RE.match(tok):
            numerals += 1
        else:
            lexical += 1
    return lexical, numerals


def parse_question_range(text: str) -> list[int] | None:
    """`"1-5"` → [1..5]；单题组写成裸数字 `"10"` → [10]。无法解析返回 None。"""
    s = re.sub(r"\s+", " ", str(text)).strip()
    m = _RANGE_RE.match(s)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return list(range(lo, hi + 1)) if lo <= hi else None
    m = _SINGLE_RE.match(s)
    return [int(m.group(1))] if m else None


def parse_blank_number(blank: str) -> int | None:
    m = BLANK_RE.match(str(blank))
    return int(m.group(1)) if m else None


def structure_question_numbers(structure: dict) -> list[int]:
    """结构块里内联引用的题号（note_sections / table_rows），升序去重。"""
    found: set[int] = set()
    for section in structure.get("note_sections") or []:
        for n in section.get("question_numbers") or []:
            if isinstance(n, int):
                found.add(n)
    for row in structure.get("table_rows") or []:
        for cell in row.get("cells") or []:
            n = cell.get("question_number")
            if isinstance(n, int):
                found.add(n)
    return sorted(found)


def load(path: str | Path) -> tuple[dict, bytes]:
    raw = Path(path).read_bytes()
    return json.loads(raw.decode("utf-8")), raw


# ------------------------------------------------------------------ 门禁
def run_gates(doc: dict, source: str = "") -> GateResult:
    """G1..G6。通过则返回 GateResult，否则抛 GateFailure（列全部失败项）。"""
    failures: list[str] = []
    advisories: list[str] = [str(a) for a in (doc.get("advisories") or [])]

    package = doc.get("package") or {}
    qface = package.get("question_face") or {}
    questions = list(qface.get("questions") or [])
    groups = list(qface.get("groups") or [])
    instructions = list(qface.get("instructions") or [])
    answer_key = list(package.get("answer_key") or [])
    evidence = list(package.get("evidence") or [])

    # ---------------------------------------------------------- G1 上游审核
    if doc.get("ok") is not True:
        failures.append(f"G1: ok={doc.get('ok')!r}，只转换 ok=true 的素材")
    status = str(doc.get("status"))
    if status not in ("PASS", "WARNING"):
        failures.append(f"G1: status={status!r}，只接受 PASS / WARNING")
    validation = doc.get("validation") or {}
    if validation.get("ok") is not True:
        failures.append(f"G1: validation.ok={validation.get('ok')!r}")
    if validation.get("errors"):
        failures.append(f"G1: validation.errors 非空：{validation['errors']}")
    review = doc.get("review") or {}
    readiness = str(review.get("content_review_readiness"))
    if readiness != "READY_FOR_HUMAN_REVIEW":
        failures.append(f"G1: review.content_review_readiness={readiness!r}")
    qc = str(review.get("question_qc_status"))
    if qc not in ("PASS", "WARNING"):
        failures.append(f"G1: review.question_qc_status={qc!r}")
    counts = (review.get("summary") or {}).get("counts") or {}
    for severity in ("CRITICAL", "MAJOR"):
        if int(counts.get(severity) or 0):
            failures.append(f"G1: review.summary.counts.{severity}={counts[severity]}")
    if str(review.get("visual_qc_status")) == "NOT_RUN":
        advisories.append("visual_qc_status=NOT_RUN：卷面视觉未经上游核对")

    # ------------------------------------------------- G2 题目与脚本互证
    cc = doc.get("cross_check") or {}
    if cc.get("ok") is not True:
        failures.append(f"G2: cross_check.ok={cc.get('ok')!r}")
    for key in ("hard_defects", "leakage", "equally_supported_rivals"):
        if cc.get(key):
            failures.append(f"G2: cross_check.{key} 非空：{cc[key]}")
    cc_items = list(cc.get("items") or [])
    compared, agreed = cc.get("compared"), cc.get("agreed")
    if compared != len(cc_items):
        failures.append(f"G2: cross_check.compared={compared!r} != items 条数 {len(cc_items)}")
    if compared != len(questions):
        failures.append(f"G2: cross_check.compared={compared!r} != 题数 {len(questions)}")
    needs_review = {
        int(i["number"]) for i in (cc.get("needs_review") or []) if isinstance(i.get("number"), int)
    }
    if isinstance(agreed, int) and agreed + len(needs_review) != compared:
        failures.append(
            f"G2: agreed({agreed}) + needs_review({len(needs_review)}) != compared({compared})"
        )
    # 结论口径：非 agree 的结论必须是白名单里的，并且必须被上游挂进 needs_review。
    # 只要答案两侧一致，引文相邻不阻断转换 —— 进 correctResponse 的是答案。
    for item in cc_items:
        n, outcome = item.get("number"), str(item.get("outcome"))
        if outcome not in TOLERATED_CROSS_CHECK_OUTCOMES:
            failures.append(f"G2: Q{n} 的 cross_check.outcome={outcome!r} 不在白名单 {TOLERATED_CROSS_CHECK_OUTCOMES}")
        elif outcome != "agree" and n not in needs_review:
            failures.append(f"G2: Q{n} 结论是 {outcome!r} 却没有进 needs_review")
        if not answers_agree(item.get("writer_answer", ""), item.get("auditor_answer", "")):
            failures.append(
                f"G2: Q{n} 两侧答案不一致：writer={item.get('writer_answer')!r} "
                f"auditor={item.get('auditor_answer')!r}"
            )
    consistency = cc.get("consistency") or {}
    if consistency.get("ok") is not True:
        failures.append(f"G2: cross_check.consistency.ok={consistency.get('ok')!r}")
    if consistency.get("errors"):
        failures.append(f"G2: cross_check.consistency.errors 非空：{consistency['errors']}")
    if cc.get("quotes_checked") is not True:
        advisories.append("cross_check.quotes_checked 不为 true：引文未逐条比对")
    if needs_review:
        advisories.append(
            "cross_check.needs_review 覆盖 Q" + ", Q".join(str(n) for n in sorted(needs_review))
            + "：答案一致但引文落在相邻 turn，需人工确认"
        )

    # --------------------------------------- G3 题号 / 答案键 / 证据三方闭合
    numbers = sorted(int(q["number"]) for q in questions if isinstance(q.get("number"), int))
    if len(numbers) != len(questions):
        failures.append("G3: 存在 number 缺失或非整数的 question")
    if numbers != list(range(1, len(numbers) + 1)):
        failures.append(f"G3: 题号不是从 1 连续无重复：{numbers}")
    ak_numbers = sorted(int(a["number"]) for a in answer_key if isinstance(a.get("number"), int))
    if len(ak_numbers) != len(set(ak_numbers)):
        failures.append(f"G3: answer_key 题号有重复：{ak_numbers}")
    if sorted(set(ak_numbers)) != numbers:
        failures.append(f"G3: answer_key 题号 {ak_numbers} 与题面题号 {numbers} 不一致")
    ev_numbers = sorted({int(e["number"]) for e in evidence if isinstance(e.get("number"), int)})
    if ev_numbers != numbers:
        failures.append(f"G3: evidence 覆盖 {ev_numbers} 与题号 {numbers} 不一致")
    for q in questions:
        n = q.get("number")
        inline = parse_blank_number(q.get("blank", ""))
        if inline is None:
            failures.append(f"G3: Q{n} 的 blank={q.get('blank')!r} 不是「题号 + 空格」形状")
        elif inline != n:
            failures.append(f"G3: Q{n} 的 blank 内联题号是 {inline}，与 number 不符")
    # canonical 必须就是被互证过的那个答案，否则 correctResponse 会与 cross_check 脱钩
    canonical_by_number = {
        int(a["number"]): str(a.get("canonical") or "")
        for a in answer_key
        if isinstance(a.get("number"), int)
    }
    for item in cc_items:
        n = item.get("number")
        if not isinstance(n, int) or n not in canonical_by_number:
            continue
        if not answers_agree(canonical_by_number[n], item.get("writer_answer", "")):
            failures.append(
                f"G3: Q{n} 的 answer_key.canonical={canonical_by_number[n]!r} 与 "
                f"cross_check.writer_answer={item.get('writer_answer')!r} 不是同一个答案"
            )

    # ------------------------------------------------ G4 分组与指令闭合
    group_ids = [str(g.get("group_id")) for g in groups]
    if len(group_ids) != len(set(group_ids)):
        failures.append(f"G4: groups 的 group_id 有重复：{sorted(group_ids)}")
    if not groups:
        failures.append("G4: question_face.groups 为空")
    instruction_ids = sorted({str(i.get("group_id")) for i in instructions})
    if instruction_ids != sorted(set(group_ids)):
        failures.append(f"G4: instructions 分组 {instruction_ids} != groups 分组 {sorted(set(group_ids))}")
    group_numbers: dict[str, list[int]] = {gid: [] for gid in group_ids}
    for q in questions:
        gid = str(q.get("group_id"))
        if gid not in group_numbers:
            failures.append(f"G4: Q{q.get('number')} 的 group_id={gid!r} 不在 groups 里")
        else:
            group_numbers[gid].append(int(q["number"]))
    for gid in group_numbers:
        group_numbers[gid].sort()
    for g in groups:
        layout = str(g.get("layout"))
        if layout not in SUPPORTED_LAYOUTS:
            failures.append(f"G4: 分组 {g.get('group_id')!r} 的 layout={layout!r} 不在 {SUPPORTED_LAYOUTS}")
        if not str(g.get("title") or "").strip():
            advisories.append(f"分组 {g.get('group_id')!r} 没有 title，卷面标题将退回 group_id")
    covered: list[int] = []
    for inst in instructions:
        rng = parse_question_range(inst.get("question_range", ""))
        if rng is None:
            failures.append(
                f"G4: 分组 {inst.get('group_id')!r} 的 question_range="
                f"{inst.get('question_range')!r} 无法解析"
            )
            continue
        covered += rng
        gid = str(inst.get("group_id"))
        if gid in group_numbers and sorted(rng) != group_numbers[gid]:
            failures.append(
                f"G4: 分组 {gid!r} 的 question_range 解析为 {sorted(rng)}，"
                f"实际题号是 {group_numbers[gid]}"
            )
    if sorted(covered) != numbers:
        failures.append(f"G4: instructions 覆盖 {sorted(covered)} != 题号 {numbers}")

    # ------------------------------------------------ G5 字数上限自洽
    limits: dict[int, WordLimit] = {}
    inst_by_group = {str(i.get("group_id")): i for i in instructions}
    group_of = {int(q["number"]): str(q.get("group_id")) for q in questions if isinstance(q.get("number"), int)}
    for a in answer_key:
        n = a.get("number")
        if not isinstance(n, int):
            continue
        try:
            limit = parse_word_limit(a.get("word_limit", ""))
        except KeyError as exc:
            failures.append(f"G5: Q{n} 的 word_limit={a.get('word_limit')!r} 不是已知文案（{exc.args[0]!r}）")
            continue
        if int(a.get("numeral_allowance", -1)) != limit.numerals:
            failures.append(
                f"G5: Q{n} numeral_allowance={a.get('numeral_allowance')!r} 与文案"
                f"「{limit.text}」推出的 {limit.numerals} 不符"
            )
        inst = inst_by_group.get(group_of.get(n, ""))
        if inst is not None:
            if str(inst.get("word_limit", "")).strip() != limit.text:
                failures.append(
                    f"G5: Q{n} 的 word_limit「{limit.text}」与题头"
                    f"「{inst.get('word_limit')}」不一致"
                )
            if int(inst.get("numeral_allowance", -1)) != limit.numerals:
                failures.append(
                    f"G5: Q{n} 的 numeral_allowance 与题头 "
                    f"{inst.get('numeral_allowance')!r} 不一致"
                )
        canonical = str(a.get("canonical") or "").strip()
        if not canonical:
            failures.append(f"G5: Q{n} 的 canonical 为空")
        elif not limit.fits(canonical):
            lexical, nums = count_tokens(canonical)
            failures.append(
                f"G5: Q{n} 的标准答案 {canonical!r} 越限（{lexical} 实词 + {nums} 数字，"
                f"上限「{limit.text}」）"
            )
        for alt in a.get("alternatives") or []:
            if not limit.fits(str(alt)):
                failures.append(f"G5: Q{n} 的 alternatives 项 {alt!r} 越限「{limit.text}」")
        limits[n] = limit

    # ------------------------------------ G6 结构块内联题号不越组
    for g in groups:
        gid = str(g.get("group_id"))
        structure = g.get("structure") or {}
        inline = structure_question_numbers(structure)
        own = set(group_numbers.get(gid, []))
        stray = [n for n in inline if n not in own]
        if stray:
            failures.append(f"G6: 分组 {gid!r} 的结构块引用了不属于本组的题号 {stray}")
        labels = structure.get("row_labels") or structure.get("hierarchy") or []
        if not labels and not inline:
            advisories.append(
                f"分组 {gid!r}（layout={g.get('layout')}）的 structure 既无标签也无内联题号，"
                "卷面只能按题序平铺"
            )

    # -------------------------- 货币符号双写：卷面已有 £，答案不该再带 £
    duplicated_currency: dict[int, str] = {}
    carrier_by_number = {
        int(q["number"]): str(q.get("carrier_before") or "")
        for q in questions
        if isinstance(q.get("number"), int)
    }
    for n, canonical in sorted(canonical_by_number.items()):
        symbol = leading_currency(canonical)
        if symbol and symbol in carrier_by_number.get(n, ""):
            duplicated_currency[n] = symbol
    if duplicated_currency:
        advisories.append(
            "货币符号双写（卷面已印符号，标准答案又带了一个，答案按裸数字处理）："
            + "、".join(f"Q{n} {canonical_by_number[n]!r}" for n in sorted(duplicated_currency))
        )

    if failures:
        raise GateFailure(failures, source)

    questions.sort(key=lambda q: int(q["number"]))
    return GateResult(
        package=package,
        questions=questions,
        answers={int(a["number"]): a for a in answer_key},
        groups=groups,
        instructions=inst_by_group,
        limits=limits,
        group_numbers=group_numbers,
        numbers=numbers,
        duplicated_currency=duplicated_currency,
        advisories=advisories,
    )


# ------------------------------------------------------------------ CLI
def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    ok = 0
    failed: list[tuple[str, list[str]]] = []
    advisory_count = 0
    for path in argv:
        doc, _raw = load(path)
        try:
            result = run_gates(doc, source=Path(path).name)
        except GateFailure as exc:
            failed.append((Path(path).name, exc.failures))
            continue
        ok += 1
        advisory_count += len(result.advisories)
    print(f"门禁通过 {ok}/{len(argv)}，advisory {advisory_count} 条")
    for name, reasons in failed:
        print(f"\n✗ {name}")
        for r in reasons:
            print(f"    {r}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
