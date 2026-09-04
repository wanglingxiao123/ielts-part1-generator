"""题目包 JSON → QTI 2.2.4 内容包（item + manifest），全部在内存里完成。

流程：

    题目包 JSON ──[准入门禁 G1..G6]──> Material IR ──> QTI 2.2 emitter ──> ExportBundle
                  questions_input.py            │
                                     accept_sets.py（容错集 + 拒绝集）

这一层只做「IR 与序列化」。它不知道 S3、不知道版本、不知道 HTTP：那些在
``qti_export.service`` 里。输入契约就是 ``_questions/{material_id}.json`` 的形状
（``package.question_face`` / ``package.answer_key`` / ``review`` / ``cross_check`` /
``validation``），逐字段映射见 ``qti_export/README.md`` §2。

设计约束：

- **确定性**。同一输入必须逐字节产出同一结果：不使用 set 迭代序、不写时间戳到产物里
  （provenance 只写输入的 sha256），zip 条目的时间戳固定。下游拿两份导出做 diff 时，
  唯一的差异必须是内容差异。
- **不声明产不出来的资源**。这条流水线没有听力音频可交付，所以 item 里**不生成**
  ``qh5:audio``、manifest 里也不声明音频文件。声明了而不存在的资源 XSD 照样全绿，
  导入端却会直接失败 —— 宁可产出一个自洽的无音频包，音频由下游系统另行挂载。
- **emitter 与 IR 分离**。将来加 QTI 3.0 emitter 只改序列化层。
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

from . import accept_sets
from . import questions_input as qin

GateFailure = qin.GateFailure

QTI_NS = "http://www.imsglobal.org/xsd/imsqti_v2p2"
QH5_NS = "http://www.imsglobal.org/xsd/imsqtiv2p2_html5_v1p0"
CP_NS = "http://www.imsglobal.org/xsd/imscp_v1p1"
LOM_NS = "http://ltsc.ieee.org/xsd/LOM"
QTIMD_NS = "http://www.imsglobal.org/xsd/imsqti_metadata_v2p2"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XML_NS = "http://www.w3.org/XML/1998/namespace"

ITEM_SCHEMA_LOCATION = (
    f"{QTI_NS} http://www.imsglobal.org/xsd/qti/qtiv2p2/imsqti_v2p2p4.xsd "
    f"{QH5_NS} http://www.imsglobal.org/xsd/qti/qtiv2p2/imsqtiv2p2p4_html5_v1p0.xsd"
)
CP_SCHEMA_LOCATION = (
    f"{CP_NS} http://www.imsglobal.org/xsd/qti/qtiv2p2/qtiv2p2_imscpv1p2_v1p0.xsd "
    f"{LOM_NS} http://www.imsglobal.org/xsd/imsmd_loose_v1p3p2.xsd "
    f"{QTIMD_NS} http://www.imsglobal.org/xsd/qti/qtiv2p2/imsqti_metadata_v2p2.xsd"
)

NCNAME_RE = re.compile(r"^[A-Za-z_][\w.\-]*$")
EXPECTED_LENGTH_BASE = {"word": 20, "phrase": 25, "numeric": 10}

#: 本 emitter 已实现的版式。其余版式的分组会带着分组名被明确拒绝，
#: 而不是退化成一张 form 表 —— 悄悄换版式等于把卷面判分前提改了。
IMPLEMENTED_LAYOUTS = ("form", "note", "table")

#: ``material_id`` 的形状：``20260808-booking-hotel-45425df4``。中段是场景键。
MATERIAL_ID_RE = re.compile(r"^\d{8}-(.+)-[0-9a-f]{8}$")


class LayoutNotImplemented(RuntimeError):
    """输入合法，但这个版式的 emitter 还没写。"""


# ================================================================== IR
@dataclass
class Gap:
    number: int
    label: str
    #: 所属分组的 group_id。可能是 "A" / "booking_details" / "g1"，不保证是序号
    group_id: str
    target: str
    accept: List[str]
    reject: List[str]
    expected_length: int
    #: 题号之前的卷面文字，如 "for " / "£ "（来自 carrier_before）
    prefix: str = ""
    #: 空格之后的卷面文字，如 " in total" / " per night"（来自 carrier_after）
    suffix: str = ""
    #: 逐题字数上限
    limit: Optional[qin.WordLimit] = None
    #: 卷面已经印着、因此答案里要剥掉的货币符号
    stripped_currency: str = ""
    response_form: str = ""
    answer_category: str = ""
    #: 上游标注的竞争答案（review.reconstructed_answers[].competing_candidates），
    #: 用于生成负向用例
    distractors: List[str] = field(default_factory=list)


@dataclass
class GroupIR:
    """一个题组 = 卷面上的一块（一张表 / 一段 note）。"""

    group_id: str
    title: str
    layout: str
    #: 题头指令原文
    instruction_text: str
    #: "1-5"，单题组可能是裸数字 "10"
    question_range: str
    limit: qin.WordLimit
    numbers: List[int]
    #: row_labels 或 hierarchy，按输入原序。**不保证与题数相等**
    labels: List[str] = field(default_factory=list)
    column_labels: List[str] = field(default_factory=list)
    row_header_label: str = ""
    note_sections: List[dict] = field(default_factory=list)
    table_rows: List[dict] = field(default_factory=list)

    @property
    def heading(self) -> str:
        """题号段落文案：Questions 1–5。"""
        nums = self.numbers
        if not nums:
            return "Questions"
        if len(nums) == 1:
            return f"Question {nums[0]}"
        return f"Questions {nums[0]}–{nums[-1]}"


@dataclass
class Material:
    #: 题目包里的 ``package.material_id``。实测常是自由文本（"Test 1 Part 1"），
    #: 只进 metadata，不当标识符
    material_id: str
    #: 存储键里的材料 id：``20260808-booking-hotel-45425df4``。这才是这套材料的身份
    slug: str
    item_identifier: str
    test_package: str
    part_reference: str
    part_number: int
    groups: List[GroupIR]
    gaps: List[Gap]
    input_sha256: str
    review: List[str]
    #: 题目版本序号（V1 = 原始交付版）。进标识符与标题
    version_ordinal: int = 1

    @property
    def title(self) -> str:
        head = f"IELTS Listening {self.test_package} - {self.part_reference} - {self.scenario_title}"
        return f"{head} (v{self.version_ordinal})"

    @property
    def scenario_key(self) -> str:
        """``20260808-booking-hotel-45425df4`` -> ``booking-hotel``。"""
        m = MATERIAL_ID_RE.match(self.slug)
        return m.group(1) if m else self.slug

    @property
    def scenario_title(self) -> str:
        """booking-hotel -> "Booking hotel"。"""
        parts = [p for p in self.scenario_key.split("-") if p]
        return " ".join(parts).capitalize() if parts else self.scenario_key

    @property
    def item_filename(self) -> str:
        return f"part{self.part_number}-{self.slug}-v{self.version_ordinal}.xml"

    def gaps_of(self, group: GroupIR) -> List[Gap]:
        return [g for g in self.gaps if g.group_id == group.group_id]


# ================================================================== IR 构建
def _labels_for(group: dict) -> List[str]:
    """行标签。form / table 用 row_labels，note 用 hierarchy。"""
    structure = group.get("structure") or {}
    return [str(x) for x in (structure.get("row_labels") or structure.get("hierarchy") or [])]


def _label_for(number: int, group: GroupIR, index: int) -> str:
    """题号 -> 左列标签。

    标签数与题数**不保证相等**（note 版式常见标签少于题数，也有 7 个标签配 5 道题
    的），所以只有**数量完全相等**时才按位置对齐。不等就一律留空，让 emitter 画
    无标签行 —— 错位的标签比没有标签更糟：它会把答案指向另一行的语义，而且看起来
    完全正常。``note_sections`` 给了显式题号归属时优先用它。
    """
    for section in group.note_sections:
        if number in (section.get("question_numbers") or []):
            return str(section.get("heading") or "")
    if len(group.labels) == len(group.numbers) and index < len(group.labels):
        return group.labels[index]
    return ""


def _visible_text(group: GroupIR, questions: List[dict]) -> str:
    """这个分组印在卷面上的全部文字：标题、题头、行列标签、note 小标题、表格印死的单元格、
    各题的 carrier。**不含 signposts**——那是出题意图，不印给考生；上游 validator 把它算进
    「可见文字」是它自己的口径，这里以真实卷面为准。供 accept_sets 的 R8 判断中心词是否在卷面上。
    """
    parts: List[str] = [group.title, group.instruction_text, group.row_header_label]
    parts += group.labels + group.column_labels
    for section in group.note_sections:
        parts.append(str(section.get("heading") or ""))
    for row in group.table_rows:
        for cell in row.get("cells") or []:
            parts.append(str(cell.get("text") or ""))
    for q in questions:
        if str(q.get("group_id")) == group.group_id:
            parts += [str(q.get("carrier_before") or ""), str(q.get("carrier_after") or "")]
    return " ".join(p for p in parts if p)


def build_groups(gate: qin.GateResult) -> List[GroupIR]:
    groups: List[GroupIR] = []
    for raw in gate.groups:
        gid = str(raw.get("group_id"))
        structure = raw.get("structure") or {}
        instruction = gate.instructions[gid]
        numbers = gate.group_numbers[gid]
        try:
            limit = qin.parse_word_limit(instruction.get("word_limit", ""))
        except KeyError:  # G5 已经拦过，这里只是让类型收敛
            limit = gate.limits[numbers[0]]
        groups.append(
            GroupIR(
                group_id=gid,
                # title 可缺，退回 group_id 而不是留空表头
                title=str(raw.get("title") or gid),
                layout=str(raw.get("layout")),
                instruction_text=str(instruction.get("instruction_text") or ""),
                question_range=str(instruction.get("question_range") or ""),
                limit=limit,
                numbers=numbers,
                labels=_labels_for(raw),
                column_labels=[str(x) for x in (structure.get("column_labels") or [])],
                row_header_label=str(structure.get("row_header_label") or ""),
                note_sections=list(structure.get("note_sections") or []),
                table_rows=list(structure.get("table_rows") or []),
            )
        )
    return groups


def ncname(value: str) -> str:
    """把任意 id 收敛成合法 NCName：非法字符换成 ``-``，首字符不是字母/下划线就加前缀。"""
    cleaned = re.sub(r"[^\w.\-]", "-", str(value))
    if not cleaned or not re.match(r"[A-Za-z_]", cleaned[0]):
        cleaned = "id-" + cleaned
    return cleaned


def build_material(
    doc: dict,
    raw: bytes,
    *,
    slug: str,
    version_ordinal: int = 1,
) -> Material:
    """门禁 → IR。``slug`` 是存储键里的材料 id，``raw`` 是输入字节（只用于 provenance 哈希）。"""
    gate = qin.run_gates(doc, slug)
    package = gate.package
    groups = build_groups(gate)
    group_by_id = {g.group_id: g for g in groups}

    evidence_by_number = {
        int(e["number"]): str(e.get("quote") or "")
        for e in (package.get("evidence") or [])
        if isinstance(e.get("number"), int)
    }
    distractors_by_number: Dict[int, List[str]] = {}
    for rec in ((doc.get("review") or {}).get("reconstructed_answers") or []):
        n = rec.get("number")
        if not isinstance(n, int):
            continue
        # equally_supported=True 的竞争答案不是干扰项而是缺陷（G2 已拦），
        # 这里只收上游明确判定为「可排除」的那些。
        distractors_by_number[n] = [
            str(c.get("text"))
            for c in (rec.get("competing_candidates") or [])
            if c.get("text") and not c.get("equally_supported")
        ]

    gaps: List[Gap] = []
    review: List[str] = list(gate.advisories)
    seen_in_group: Dict[str, int] = {}
    visible_by_group = {g.group_id: _visible_text(g, gate.questions) for g in groups}

    for question in gate.questions:
        number = int(question["number"])
        gid = str(question["group_id"])
        group = group_by_id[gid]
        index = seen_in_group.get(gid, 0)
        seen_in_group[gid] = index + 1

        answer = gate.answers[number]
        limit = gate.limits[number]
        canonical = str(answer["canonical"]).strip()

        # 卷面已印货币符号 -> 答案剥掉它。不剥就会产出 "£ £128 per night"，
        # 而且 correctResponse 会要求考生把卷面上已有的符号再打一遍。
        stripped = gate.duplicated_currency.get(number, "")
        target = canonical[len(stripped):].strip() if stripped else canonical

        prefix = str(question.get("carrier_before") or "")
        suffix = str(question.get("carrier_after") or "")

        spec = accept_sets.build(
            question,
            {"canonical": canonical, "target": target, "alternatives": answer.get("alternatives") or []},
            limit,
            evidence=evidence_by_number.get(number, ""),
            prefix=prefix,
            suffix=suffix,
            distractors=distractors_by_number.get(number, []),
            visible_text=visible_by_group.get(gid, ""),
        )
        review += [f"Q{number}: {msg}" for msg in spec.review]

        base = EXPECTED_LENGTH_BASE.get(str(question.get("response_form")), 25)
        widest = max(len(v) for v in spec.accept)
        gaps.append(
            Gap(
                number=number,
                label=_label_for(number, group, index),
                group_id=gid,
                target=spec.target,
                accept=spec.accept,
                reject=spec.reject,
                # 输入框要容得下任何可接受写法，向上取到 5 的整数倍
                expected_length=max(base, -(-(widest + 4) // 5) * 5),
                prefix=prefix,
                suffix=suffix,
                limit=limit,
                stripped_currency=stripped,
                response_form=str(question.get("response_form") or ""),
                answer_category=str(question.get("answer_category") or ""),
                distractors=distractors_by_number.get(number, []),
            )
        )

    item_identifier = ncname(f"ielts-{slug}-v{version_ordinal}")
    if not NCNAME_RE.match(item_identifier):  # pragma: no cover - ncname() 已保证
        raise ValueError(f"identifier {item_identifier!r} 不是合法 NCName")

    reference = str(package.get("reference") or "Part 1")
    m = re.search(r"(\d+)", reference)

    return Material(
        material_id=str(package.get("material_id") or slug),
        slug=slug,
        item_identifier=item_identifier,
        test_package=str(package.get("test_package") or ""),
        part_reference=reference,
        part_number=int(m.group(1)) if m else 1,
        groups=groups,
        gaps=gaps,
        input_sha256=hashlib.sha256(raw).hexdigest(),
        review=review,
        version_ordinal=version_ordinal,
    )


# ================================================================== emitter
def _sub(parent: ET.Element, tag: str, text: Optional[str] = None, **attrs: str) -> ET.Element:
    el = ET.SubElement(parent, tag, {k: v for k, v in attrs.items()})
    if text is not None:
        el.text = text
    return el


def _rubric(parent: ET.Element, group: GroupIR) -> None:
    """题头指令。字数上限那一段加粗 —— 它是判分口径，不是排版重点。

    文案原样来自输入的 ``instruction_text``，不由本模块拼装：上限有 5 种写法，
    自己拼就会出现「题头写 TWO WORDS、容错集按 THREE WORDS 过滤」的漂移。
    """
    text = group.instruction_text
    limit_text = group.limit.text
    p = _sub(parent, f"{{{QTI_NS}}}p", **{"class": "ielts-rubric"})
    head, sep, tail = text.partition(limit_text)
    if not sep:
        p.text = text
        return
    p.text = head
    strong = _sub(p, f"{{{QTI_NS}}}strong", limit_text)
    strong.tail = tail


#: suffix 以这些字符开头时**不**补空格 —— 它们在排版上紧贴前一个词。
HUGGING_SUFFIX_CHARS = ".,;:?!)]}'’%"


def _join_prefix(prefix: str) -> str:
    """prefix 与题号之间保证恰好一个空格。

    ``carrier_before`` 不保证自带尾随空格（裸 ``£`` 很常见），所以既不能原样输出
    也不能无条件补 —— 原样会得到 ``The flat comes6 ___``，无条件补会把已有空格变成双空格。
    货币符号也补：题号是独立的一个 token，``£6 ______`` 会被读成金额是 6。
    """
    if not prefix:
        return ""
    return prefix if prefix.endswith((" ", "\t", "\n")) else prefix + " "


def _join_suffix(suffix: str) -> str:
    """空格与 suffix 之间按需补一个空格：标点紧贴（``___.``），词要隔开（``___ of rent.``）。"""
    if not suffix:
        return ""
    if suffix[0] in HUGGING_SUFFIX_CHARS or suffix.startswith((" ", "\t", "\n")):
        return suffix
    return " " + suffix


def _interaction(parent: ET.Element, gap: Gap) -> None:
    num = _sub(parent, f"{{{QTI_NS}}}strong", str(gap.number), **{"class": "qnum"})
    num.tail = " "
    interaction = _sub(
        parent,
        f"{{{QTI_NS}}}textEntryInteraction",
        responseIdentifier=f"RESPONSE_{gap.number}",
        expectedLength=str(gap.expected_length),
    )
    if gap.suffix:
        interaction.tail = _join_suffix(gap.suffix)


def _fill_gap(parent: ET.Element, gap: Gap) -> None:
    """把一个答题格的内容写进 parent：prefix、加粗题号、空格、suffix。三种版式共用。"""
    if gap.prefix:
        parent.text = _join_prefix(gap.prefix)
    _interaction(parent, gap)


def _fill_gap_after_label(parent: ET.Element, gap: Gap) -> None:
    """与 _fill_gap 相同，但 parent 里已经有子元素（行首标签），
    所以 prefix 挂在最后一个子元素的 tail 上而不是 parent.text 上。"""
    last = list(parent)[-1]
    if gap.prefix:
        last.tail = (last.tail or "") + _join_prefix(gap.prefix)
    _interaction(parent, gap)


def _thead(table: ET.Element, header: List[str]) -> None:
    if not header:
        return
    tr = _sub(_sub(table, f"{{{QTI_NS}}}thead"), f"{{{QTI_NS}}}tr")
    for cell in header:
        _sub(tr, f"{{{QTI_NS}}}th", cell, scope="col")


def _form_header(group: Optional[GroupIR]) -> List[str]:
    """表头两格。两个键都缺时返回空列表（不画 thead）。"""
    if group is None:
        return []
    if group.row_header_label:
        right = str(group.column_labels[-1]) if group.column_labels else ""
        return [group.row_header_label, right]
    if group.column_labels:
        labels = [str(c) for c in group.column_labels]
        return labels[:2] if len(labels) >= 2 else [labels[0], ""]
    return []


def _label_gap_row(tbody: ET.Element, gap: Gap) -> None:
    tr = _sub(tbody, f"{{{QTI_NS}}}tr")
    _sub(tr, f"{{{QTI_NS}}}td", gap.label, **{"class": "qlabel"})
    _fill_gap(_sub(tr, f"{{{QTI_NS}}}td", **{"class": "qgap"}), gap)


def _form_table(parent: ET.Element, gaps: List[Gap], group: GroupIR) -> None:
    """form：若干「标签 | 答题格」行，可选一行表头。"""
    table = _sub(parent, f"{{{QTI_NS}}}table", **{"class": "ielts-form"})
    _thead(table, _form_header(group))
    tbody = _sub(table, f"{{{QTI_NS}}}tbody")
    for gap in gaps:
        _label_gap_row(tbody, gap)


def _note_block(parent: ET.Element, gaps: List[Gap], group: GroupIR) -> None:
    """note：分节小标题 + 每题一行的列表。

    ``note_sections`` 给出「小标题 → 题号」的显式归属，是权威结构，按它分节。
    只有 ``hierarchy`` 时**不**按位置硬分（硬对齐必然错位），退回一张平铺列表。
    没被任何分节覆盖的题不会丢：它们进末尾那张平铺列表。
    """
    by_number = {gap.number: gap for gap in gaps}
    placed: set = set()

    for section in group.note_sections:
        numbers = [n for n in (section.get("question_numbers") or []) if n in by_number]
        if not numbers:
            continue
        heading = str(section.get("heading") or "").strip()
        if heading:
            _sub(parent, f"{{{QTI_NS}}}h5", heading, **{"class": "ielts-note-heading"})
        lines = _sub(parent, f"{{{QTI_NS}}}ul", **{"class": "ielts-note-lines"})
        for number in numbers:
            _fill_gap(_sub(lines, f"{{{QTI_NS}}}li", **{"class": "qgap"}), by_number[number])
            placed.add(number)

    remaining = [gap for gap in gaps if gap.number not in placed]
    if not remaining:
        return
    lines = _sub(parent, f"{{{QTI_NS}}}ul", **{"class": "ielts-note-lines"})
    for gap in remaining:
        li = _sub(lines, f"{{{QTI_NS}}}li", **{"class": "qgap"})
        if gap.label:
            _sub(li, f"{{{QTI_NS}}}span", gap.label, **{"class": "qlabel"}).tail = " "
            _fill_gap_after_label(li, gap)
        else:
            _fill_gap(li, gap)


def _table_block(parent: ET.Element, gaps: List[Gap], group: GroupIR) -> None:
    """table：真表格，表头来自 column_labels（+ row_header_label）。

    ``table_rows`` 显式给出每一行的单元格时按它逐格渲染（``{text}`` 是印死的文字、
    ``{question_number}`` 是答题格，列数可以是 3）；否则退回「标签 | 答题格」两列。
    """
    table = _sub(parent, f"{{{QTI_NS}}}table", **{"class": "ielts-table"})
    by_number = {gap.number: gap for gap in gaps}

    if group.table_rows:
        _thead(table, [str(c) for c in group.column_labels])
        tbody = _sub(table, f"{{{QTI_NS}}}tbody")
        placed: set = set()
        for row in group.table_rows:
            tr = _sub(tbody, f"{{{QTI_NS}}}tr")
            for cell in row.get("cells") or []:
                number = cell.get("question_number")
                if isinstance(number, int) and number in by_number:
                    _fill_gap(_sub(tr, f"{{{QTI_NS}}}td", **{"class": "qgap"}), by_number[number])
                    placed.add(number)
                else:
                    _sub(tr, f"{{{QTI_NS}}}td", str(cell.get("text") or ""), **{"class": "qtext"})
        # table_rows 漏掉的题补在表尾，绝不静默丢题
        for gap in gaps:
            if gap.number not in placed:
                _label_gap_row(tbody, gap)
        return

    _thead(table, _form_header(group))
    tbody = _sub(table, f"{{{QTI_NS}}}tbody")
    for gap in gaps:
        _label_gap_row(tbody, gap)


#: layout → 渲染函数。加版式只需要加一行，且 IMPLEMENTED_LAYOUTS 与它必须对得上。
LAYOUT_EMITTERS = {
    "form": _form_table,
    "note": _note_block,
    "table": _table_block,
}
assert set(LAYOUT_EMITTERS) == set(IMPLEMENTED_LAYOUTS)


def emit_item(mat: Material) -> ET.ElementTree:
    ET.register_namespace("", QTI_NS)
    ET.register_namespace("qh5", QH5_NS)
    ET.register_namespace("xsi", XSI_NS)

    root = ET.Element(
        f"{{{QTI_NS}}}assessmentItem",
        {
            f"{{{XSI_NS}}}schemaLocation": ITEM_SCHEMA_LOCATION,
            "identifier": mat.item_identifier,
            "title": mat.title,
            "adaptive": "false",
            "timeDependent": "false",
            f"{{{XML_NS}}}lang": "en",
        },
    )
    root.append(
        ET.Comment(
            f" generated by qti_export from material {mat.slug} v{mat.version_ordinal}"
            f" sha256={mat.input_sha256[:16]} "
        )
    )

    for gap in mat.gaps:
        root.append(ET.Comment(f" Q{gap.number} {gap.label} -> {gap.target!r} "))
        rd = _sub(
            root,
            f"{{{QTI_NS}}}responseDeclaration",
            identifier=f"RESPONSE_{gap.number}",
            cardinality="single",
            baseType="string",
        )
        _sub(_sub(rd, f"{{{QTI_NS}}}correctResponse"), f"{{{QTI_NS}}}value", gap.target)
        mapping = _sub(rd, f"{{{QTI_NS}}}mapping", defaultValue="0")
        for key in gap.accept:
            # caseSensitive=false 是通用规则 R0：IELTS 不因大小写扣分
            _sub(
                mapping,
                f"{{{QTI_NS}}}mapEntry",
                mapKey=key,
                mappedValue="1",
                caseSensitive="false",
            )

    outcome = _sub(
        root,
        f"{{{QTI_NS}}}outcomeDeclaration",
        identifier="SCORE",
        cardinality="single",
        baseType="float",
        normalMaximum=f"{float(len(mat.gaps)):.1f}",
    )
    _sub(_sub(outcome, f"{{{QTI_NS}}}defaultValue"), f"{{{QTI_NS}}}value", "0.0")

    body = _sub(root, f"{{{QTI_NS}}}itemBody")
    wrap = _sub(body, f"{{{QTI_NS}}}div", **{"class": "ielts-listening-part"})
    first, last = mat.gaps[0].number, mat.gaps[-1].number
    _sub(wrap, f"{{{QTI_NS}}}h3", f"{mat.part_reference} — Questions {first}–{last}")

    # 没有音频可交付时**不生成** qh5:audio。生成了就等于声明一个不存在的资源：
    # XSD 照样全绿（它不管资源在不在），导入端却会因缺文件失败。
    audio_div = _sub(wrap, f"{{{QTI_NS}}}div", **{"class": "ielts-audio"})
    _sub(
        audio_div,
        f"{{{QTI_NS}}}p",
        "[audio pending: supplied separately]",
        **{"class": "ielts-audio-placeholder"},
    )

    unsupported = [g for g in mat.groups if g.layout not in IMPLEMENTED_LAYOUTS]
    if unsupported:
        raise LayoutNotImplemented(
            "以下分组的版式 emitter 尚未实现（已实现："
            + "/".join(IMPLEMENTED_LAYOUTS)
            + "）：\n"
            + "\n".join(f"  - 分组 {g.group_id!r} layout={g.layout!r} 题号 {g.numbers}" for g in unsupported)
        )
    previous_rubric = ""
    for group in mat.groups:
        gaps = mat.gaps_of(group)
        if not gaps:
            continue
        _sub(wrap, f"{{{QTI_NS}}}h4", group.heading, **{"class": "ielts-questions"})
        # 相邻两组题头文案完全相同时只印一次 —— 真实卷面不会把同一句连着印两遍。
        if group.instruction_text != previous_rubric:
            _rubric(wrap, group)
            previous_rubric = group.instruction_text
        _sub(wrap, f"{{{QTI_NS}}}h4", group.title, **{"class": f"ielts-{group.layout}-title"})
        block = _sub(wrap, f"{{{QTI_NS}}}div", **{"class": f"ielts-{group.layout}-block"})
        LAYOUT_EMITTERS[group.layout](block, gaps, group)

    rp = _sub(root, f"{{{QTI_NS}}}responseProcessing")
    cond = _sub(_sub(rp, f"{{{QTI_NS}}}responseCondition"), f"{{{QTI_NS}}}responseIf")
    _sub(cond, f"{{{QTI_NS}}}baseValue", "true", baseType="boolean")
    total = _sub(_sub(cond, f"{{{QTI_NS}}}setOutcomeValue", identifier="SCORE"), f"{{{QTI_NS}}}sum")
    for gap in mat.gaps:
        _sub(total, f"{{{QTI_NS}}}mapResponse", identifier=f"RESPONSE_{gap.number}")

    ET.indent(root, space="  ")
    return ET.ElementTree(root)


def emit_manifest(mat: Material) -> ET.ElementTree:
    ET.register_namespace("", CP_NS)
    ET.register_namespace("imsmd", LOM_NS)
    ET.register_namespace("imsqti", QTIMD_NS)
    ET.register_namespace("xsi", XSI_NS)

    root = ET.Element(
        f"{{{CP_NS}}}manifest",
        {
            f"{{{XSI_NS}}}schemaLocation": CP_SCHEMA_LOCATION,
            "identifier": f"MANIFEST-{mat.item_identifier}",
        },
    )

    md = _sub(root, f"{{{CP_NS}}}metadata")
    _sub(md, f"{{{CP_NS}}}schema", "QTIv2.2 Package")
    _sub(md, f"{{{CP_NS}}}schemaversion", "1.0.0")
    general = _sub(_sub(md, f"{{{LOM_NS}}}lom"), f"{{{LOM_NS}}}general")
    ident = _sub(general, f"{{{LOM_NS}}}identifier")
    _sub(ident, f"{{{LOM_NS}}}catalog", "ielts-material")
    _sub(ident, f"{{{LOM_NS}}}entry", mat.slug)
    _sub(_sub(general, f"{{{LOM_NS}}}title"), f"{{{LOM_NS}}}string", mat.title, language="en")
    _sub(general, f"{{{LOM_NS}}}language", "en")
    _sub(_sub(general, f"{{{LOM_NS}}}keyword"), f"{{{LOM_NS}}}string", mat.scenario_key, language="en")

    _sub(root, f"{{{CP_NS}}}organizations")

    resources = _sub(root, f"{{{CP_NS}}}resources")
    href = f"items/{mat.item_filename}"
    res = _sub(
        resources,
        f"{{{CP_NS}}}resource",
        identifier=f"RES-{mat.item_identifier}",
        type="imsqti_item_xmlv2p2",
        href=href,
    )
    qmd = _sub(_sub(res, f"{{{CP_NS}}}metadata"), f"{{{QTIMD_NS}}}qtiMetadata")
    _sub(qmd, f"{{{QTIMD_NS}}}itemTemplate", "false")
    _sub(qmd, f"{{{QTIMD_NS}}}timeDependent", "false")
    _sub(qmd, f"{{{QTIMD_NS}}}composite", "true")
    _sub(qmd, f"{{{QTIMD_NS}}}interactionType", "textEntryInteraction")
    _sub(res, f"{{{CP_NS}}}file", href=href)

    ET.indent(root, space="  ")
    return ET.ElementTree(root)


def serialize(tree: ET.ElementTree) -> bytes:
    buf = BytesIO()
    tree.write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue() + b"\n"


# ================================================================== 产物
@dataclass
class ExportBundle:
    """一次导出的全部产物，全部在内存里。"""

    material: Material
    item_xml: bytes
    manifest_xml: bytes
    #: 自动推导的容错集与拒绝集，供下游判分回归 / 人工评审
    reject_candidates: bytes
    #: 需人工核对的项，每项一行
    review: List[str]

    @property
    def item_identifier(self) -> str:
        return self.material.item_identifier

    @property
    def item_arcname(self) -> str:
        return f"items/{self.material.item_filename}"

    @property
    def zip_filename(self) -> str:
        return f"{self.material.item_identifier}.zip"

    def entries(self) -> List[tuple]:
        """zip 内容：(arcname, bytes)。manifest 声明的每个文件都在这里，反之亦然。"""
        rows = [
            ("imsmanifest.xml", self.manifest_xml),
            (self.item_arcname, self.item_xml),
            ("reject_candidates.json", self.reject_candidates),
        ]
        if self.review:
            rows.append(("review.txt", ("\n".join(self.review) + "\n").encode("utf-8")))
        return rows

    def manifest_hrefs(self) -> List[str]:
        root = ET.fromstring(self.manifest_xml)
        return [el.get("href", "") for el in root.iter(f"{{{CP_NS}}}file") if el.get("href")]

    def missing_resources(self) -> List[str]:
        """manifest 声明了、zip 里却没有的文件。非空就不许打包。"""
        present = {arc for arc, _ in self.entries()}
        return [href for href in self.manifest_hrefs() if href not in present]

    def zip_bytes(self) -> bytes:
        missing = self.missing_resources()
        if missing:
            raise ValueError("包不完整，拒绝打 zip：" + ", ".join(missing))
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for arc, data in self.entries():
                # 固定 ZipInfo 时间戳，否则同一输入两次导出字节不同
                info = zipfile.ZipInfo(arc, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, data)
        return buf.getvalue()


def render(doc: dict, *, slug: str, version_ordinal: int = 1, raw: Optional[bytes] = None) -> ExportBundle:
    """门禁 → IR → item + manifest。抛 ``GateFailure`` / ``LayoutNotImplemented`` / ``ValueError``。"""
    if raw is None:
        raw = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
    mat = build_material(doc, raw, slug=slug, version_ordinal=version_ordinal)
    item_xml = serialize(emit_item(mat))
    manifest_xml = serialize(emit_manifest(mat))
    rejects = {
        "material_id": mat.slug,
        "version": mat.version_ordinal,
        "note": "自动推导的容错集与拒绝集，供下游判分回归与人工评审",
        "items": [
            {"number": g.number, "target": g.target, "accept": g.accept, "reject": g.reject}
            for g in mat.gaps
        ],
    }
    return ExportBundle(
        material=mat,
        item_xml=item_xml,
        manifest_xml=manifest_xml,
        reject_candidates=(json.dumps(rejects, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        review=list(mat.review),
    )
