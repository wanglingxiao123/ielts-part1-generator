#!/usr/bin/env python3
"""接受集（accept-set）与拒绝集生成 —— 规则见 qti_export/README.md §3。

接受集是声明式枚举，直接落成 QTI 的 mapping/mapEntry：能被 XSD 校验、能被人工
评审、能 diff、能逐条写回归用例。正则做不到后三点。

两类东西必须**人工确认、不得算法猜测**（03 §2 与 §4）：
  - 短语类的「区别性成分」（twin room → twin，但 room 单独不行）
  - 词类答案的分类名扩展（Visa → Visa card）
它们走 DISTINCTIVE / EXTRA 两张覆写表；未登记时不生成该变体，并作为待审项上报。
保守方向是宁缺勿滥：少一个变体是漏判（可由考官申诉修正），多一个变体可能把
干扰项判成对（静默错误，永远发现不了）。

三处输入全部来自题目包 JSON（`_questions/{material_id}.json`），不是本模块的政策常量：
  - **字数上限**逐题来自 `answer_key[].word_limit`（`qin.WordLimit`）
  - **卷面文字**来自 `carrier_before` / `carrier_after`，决定裸数字是否成句
  - **干扰项**来自 `review.reconstructed_answers[].competing_candidates`，
    取代旧 schema 里单个 `correction.earlier`
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import questions_input as qin

# ---------------------------------------------------------------- 分类
EXACT_CATEGORIES = {"person_name", "contact"}
PHRASE_CATEGORIES = {"preference", "option", "service", "requirement"}

MONTHS = {
    "January": ("Jan", 1), "February": ("Feb", 2), "March": ("Mar", 3),
    "April": ("Apr", 4), "May": ("May", 5), "June": ("Jun", 6),
    "July": ("Jul", 7), "August": ("Aug", 8), "September": ("Sept", 9),
    "October": ("Oct", 10), "November": ("Nov", 11), "December": ("Dec", 12),
}

NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven",
    8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve",
    13: "thirteen", 14: "fourteen", 15: "fifteen", 16: "sixteen",
    17: "seventeen", 18: "eighteen", 19: "nineteen", 20: "twenty",
}

CURRENCY_WORDS = {"pound": "\u00a3", "dollar": "$", "euro": "\u20ac", "yen": "\u00a5"}

# 元音字母起首 → 用 "an"。这是近似规则：真实判据是首**音**，
# "an hour"（h 不发音）和 "a university"（读作 /juː/）都是反例。
# 当前所有短语类目标都不落在反例上；新增反例请登记到 AN_EXCEPTIONS。
VOWEL_LETTERS = set("aeiou")
AN_EXCEPTIONS: dict[str, str] = {
    # 小写首词 -> 应使用的冠词
    "hour": "an",
    "honest": "an",
    "university": "a",
    "european": "a",
    "one": "a",
}

# ---- 人工确认表：短语类的区别性成分（保留项 -> 丢弃的上位词，后者进拒绝集）----
DISTINCTIVE: dict[str, tuple[str, str]] = {
    # target: (可单独接受的区别性成分, 必须拒绝的上位词)
    "twin room": ("twin", "room"),
    "airport shuttle": ("shuttle", "airport"),
}

# ---- 人工确认表：词类答案的分类名扩展 ----
EXTRA: dict[str, list[str]] = {
    "Visa": ["Visa card"],
}


@dataclass
class AnswerSpec:
    number: int
    target: str
    accept: list[str]
    reject: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)


def _dedup(values: list[str]) -> list[str]:
    """保序去重，大小写不敏感（mapEntry 一律 caseSensitive=false，同键重复无意义）。"""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        k = v.casefold()
        if k not in seen:
            seen.add(k)
            out.append(v)
    return out


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _detect_currency(*contexts: str) -> str | None:
    """从语境判定币种。符号优先，其次是货币词。

    符号那一路是新加的：卷面 `carrier_before` 常常就是 `"£ "`，引文也多写
    `"£128 per night"`。旧版只认 `pound` / `dollar` 这类**词**，于是在语境里明明
    有 `£` 的题上报「无法判定币种」，白白少生成货币变体。
    """
    joined = " ".join(c for c in contexts if c)
    for symbol in CURRENCY_WORDS.values():
        if symbol in joined:
            return symbol
    low = joined.lower()
    for word, symbol in CURRENCY_WORDS.items():
        if word in low:
            return symbol
    return None


def _article(phrase: str) -> str:
    """选 a / an。见 VOWEL_LETTERS 上方注释：这是首字母近似，不是首音。"""
    head = re.sub(r"[^A-Za-z]", "", phrase.split()[0]).lower() if phrase.split() else ""
    if head in AN_EXCEPTIONS:
        return AN_EXCEPTIONS[head]
    return "an" if head[:1] in VOWEL_LETTERS else "a"


def within_word_limit(value: str, limit: qin.WordLimit) -> bool:
    """题头字数上限判定。

    计数口径**复用门禁的** `questions_input.count_tokens`，不再本地实现一份：
    两份实现一旦漂移，就会出现「门禁放行的标准答案被容错集自己滤掉」这种自相
    矛盾的状态。上限也不再是「N 词 + 最多一个数字」的硬假设 —— 实测有 35 条答案
    的 `numeral_allowance` 是 0（`ONE WORD ONLY` / `NO MORE THAN TWO WORDS`）。
    """
    return limit.fits(value)


# ---------------------------------------------------------------- 各类规则
def _date(target: str) -> tuple[list[str], list[str]]:
    """<日> <月名> → 6 种写法。不生成美式 月/日，也不生成年份（材料里没有）。"""
    m = re.fullmatch(r"(\d{1,2})\s+([A-Z][a-z]+)", target.strip())
    if not m:
        return [target], [f"date 目标 {target!r} 不匹配 '<日> <月名>'，只生成原样写法"]
    day, month = int(m.group(1)), m.group(2)
    if month not in MONTHS:
        return [target], [f"未知月名 {month!r}，只生成原样写法"]
    abbr, num = MONTHS[month]
    return [
        target,
        f"{_ordinal(day)} {month}",
        f"{month} {day}",
        f"{month} {_ordinal(day)}",
        f"{day} {abbr}",
        f"{day}/{num}",
    ], []


def _duration(target: str) -> tuple[list[str], list[str]]:
    """<数字> <单位> → 数字+单位 / 词+单位 / 纯数字 / 纯词。"""
    m = re.fullmatch(r"(\d+)\s+(\w+)", target.strip())
    if not m:
        return [target], [f"duration 目标 {target!r} 不匹配 '<数字> <单位>'，只生成原样写法"]
    n, unit = int(m.group(1)), m.group(2)
    word = NUMBER_WORDS.get(n)
    if word is None:
        return [target, str(n)], [f"数字 {n} 超出 NUMBER_WORDS 覆盖范围，未生成英文数词变体"]
    # 纯数字是否可接受取决于题面，见 03 §4；当前政策取宽松侧。
    return [target, f"{word} {unit}", str(n), word], []


def _price(target: str, *contexts: str) -> tuple[list[str], list[str]]:
    """纯数字 → 裸数字 / 符号+数字 / 数字+货币词 / 两位小数。

    `contexts` 是**多路**语境：引文、卷面 prefix、卷面 suffix。卷面上的 `£` 往往
    比引文更可靠，所以三路一起看。千位分隔符先去掉再判形态（`1,450`）。
    """
    amount = target.strip().replace(",", "")
    if not re.fullmatch(r"\d+(\.\d+)?", amount):
        return [target], [f"price 目标 {target!r} 不是纯数字，只生成原样写法"]
    symbol = _detect_currency(*contexts)
    variants = [target.strip()]
    if amount != target.strip():
        variants.append(amount)
    if symbol is None:
        return variants, [f"无法从语境判定币种（{contexts!r}），未生成货币符号变体"]
    word = next(w for w, s in CURRENCY_WORDS.items() if s == symbol)
    variants += [
        f"{symbol}{target.strip()}",
        f"{target.strip()} {word}s",
        f"{symbol}{float(amount):.2f}",
    ]
    return variants, []


def _time(target: str) -> tuple[list[str], list[str]]:
    """钟点：`:` 与 `.` 互通，a.m./p.m. 的四种写法互通。

    这不是猜测，是这批素材自身的事实：94 道 time 题里 `7:30` 形态 29 条、
    `7.30` 形态 26 条 —— 同一个钟点两种记法并存，只收一种就会把另一半写法
    判错（英式卷面惯用句点，冒号同样通行）。

    刻意**不生成**的两类：24 小时制换算（`19:30`）与英文读法
    （`half past seven`）。它们是不同的答案形态，不是同一写法的变体，
    生成它们会把「答得不合题面要求」也判成对。
    """
    m = re.fullmatch(r"(\d{1,2})[.:](\d{2})\s*(a\.m\.|p\.m\.|am|pm)?", target.strip(), re.IGNORECASE)
    if m:
        hour, minute, meridiem = m.group(1), m.group(2), m.group(3)
        stems = [f"{hour}:{minute}", f"{hour}.{minute}"]
        if not meridiem:
            return stems, []
        return [f"{stem}{tail}" for stem in stems for tail in _meridiem_forms(meridiem)], []

    m = re.fullmatch(r"(\d{1,2})\s*(a\.m\.|p\.m\.|am|pm)", target.strip(), re.IGNORECASE)
    if m:
        hour, meridiem = m.group(1), m.group(2)
        return [f"{hour}{tail}" for tail in _meridiem_forms(meridiem)], []

    return [target], [f"time 目标 {target!r} 不是钟点形态，只生成原样写法"]


def _meridiem_forms(meridiem: str) -> list[str]:
    """`a.m.` → [" a.m.", "a.m.", " am", "am"]，保序，供拼接。"""
    letter = meridiem.strip().lower()[0]
    dotted, plain = f"{letter}.m.", f"{letter}m"
    return [f" {dotted}", dotted, f" {plain}", plain]


def _document(target: str) -> tuple[list[str], list[str]]:
    """字母数字编号：允许在字母/数字交界处插入一个空格（听录时的自然分组）。
    不允许连字符——那会引入原文没有的符号。"""
    variants = [target]
    spaced = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", target.strip())
    if spaced != target.strip():
        variants.append(spaced)
    return variants, []


def _phrase(target: str, response_form: str) -> tuple[list[str], list[str], list[str]]:
    """短语类：原样 / 加冠词 / 区别性成分（需人工登记）。

    冠词变体在 2 词上限下几乎总会被 within_word_limit 滤掉，这是对的——
    仍然生成它们，是为了让上限放宽到 3 词时无需改这里。
    """
    accept = [target]
    if response_form != "word":
        accept += [f"{_article(target)} {target}", f"the {target}"]
    reject: list[str] = []
    review: list[str] = []
    if target in DISTINCTIVE:
        keep, drop = DISTINCTIVE[target]
        accept.append(keep)
        reject.append(drop)
    elif response_form != "word" and " " in target:
        review.append(
            f"短语 {target!r} 未在 DISTINCTIVE 登记，未生成区别性成分变体。"
            f"若考官接受简写，请在 qti_export/accept_sets.py 补 {target!r}: (保留词, 拒绝的上位词)"
        )
    return accept, reject, review


# ---------------------------------------------------------------- 入口
def _affix_redundant(value: str, affix: str) -> bool:
    """affix 已经供了单位时，接受集里重复该单位的写法要剔除。

    只对**词形**单位生效，不对货币符号生效，这个不对称是刻意的：
      - "£ ___ per night" 里填 "128 pounds" → "£ 128 pounds per night"，句子重复，
        考官会判错，收进接受集就是给错分。
      - 同一格填 "£128" → "£ £128 per night"，符号重复是考生近乎普遍的无害习惯，
        剔除它会造成**误扣分**，比多给分更糟。所以保留。
    """
    if not affix.strip():
        return False
    affix_low = affix.lower()
    symbols = {s for s in CURRENCY_WORDS.values() if s in affix}
    # 货币符号出现在 affix 里 → 对应的词形（pound/pounds…）视为重复
    unit_words = {w for w, s in CURRENCY_WORDS.items() if s in symbols}
    unit_words |= {w for w in CURRENCY_WORDS if w in affix_low}
    tokens = {t.strip(".,;:").lower() for t in value.split()}
    return any(w in tokens or f"{w}s" in tokens for w in unit_words)


def _bare_number_forms(target: str) -> set[str]:
    """target 的「只剩数字」写法：`3 nights` → {`3`, `three`}。"""
    m = re.match(r"^(\d+)\b", target.strip())
    if not m:
        return set()
    n = int(m.group(1))
    forms = {str(n)}
    word = NUMBER_WORDS.get(n)
    if word:
        forms.add(word)
    return forms


def carrier_supplies_unit(target: str, prefix: str, suffix: str) -> bool:
    """卷面自己是否已经写出了单位。决定裸数字能不能算对（R7）。

    这条判断旧方案是人工登记的（`labels.Stem.allow_bare_number`），现在从
    carrier 直接推：

        `for ___ nights`            单位在卷面 → 填 `3` 就够   → True
        `covers ___ in total`       单位不在卷面 → 必须填 `3 nights` → False
        `£ ___ per night`           货币符号在卷面 → 填 `128`  → True

    判据是 target 的单位词是否出现在 prefix / suffix 里，而不是「carrier 是否像
    句子」—— 后者会把 `for ___ nights` 也判成句框，从而把正确的裸数字判错。
    """
    m = re.fullmatch(r"\d+[\d,.]*\s+(.+)", target.strip())
    if not m:
        # 目标本身没有单位（纯数字如 price / time），裸数字就是答案形态
        return True
    unit = re.sub(r"[^A-Za-z]", "", m.group(1)).lower()
    if not unit:
        return True
    carrier = f"{prefix} {suffix}".lower()
    singular = unit[:-1] if unit.endswith("s") else unit
    return bool(re.search(rf"\b{re.escape(singular)}s?\b", carrier))


def build(
    question: dict,
    answer: dict,
    limit: qin.WordLimit,
    *,
    evidence: str = "",
    prefix: str = "",
    suffix: str = "",
    distractors: list[str] | None = None,
) -> AnswerSpec:
    """按一道题的输入生成接受集与拒绝集。

    `question` 是 `question_face.questions[]` 的一条，`answer` 是 `answer_key[]`
    的同题号条目，`limit` 是该题的字数上限（题头与 answer_key 已由 G5 校验同源）。

    `answer["alternatives"]` 作为接受集**种子**：上游给了就先收进来。实测这批
    素材 1070 条全是空数组，所以规则推导仍是主力，不是备用路径。

    prefix / suffix 来自 carrier_before / carrier_after——卷面上空格前后的附加
    文字。它们不是排版细节，直接决定哪些写法算对（README §3 的 R6 / R7）。
    """
    number = int(question["number"])
    target = str(answer["target"] if "target" in answer else answer["canonical"]).strip()
    category = str(question.get("answer_category") or "")
    response_form = str(question.get("response_form") or "")
    context = str(evidence)

    review: list[str] = []
    reject: list[str] = []

    if category in EXACT_CATEGORIES:
        accept = [target]
    elif category == "document":
        accept, notes = _document(target)
        review += notes
    elif category == "date":
        accept, notes = _date(target)
        review += notes
    elif category == "time":
        accept, notes = _time(target)
        review += notes
    elif category in ("duration", "quantity"):
        accept, notes = _duration(target)
        review += notes
    elif category == "price":
        accept, notes = _price(target, context, prefix, suffix)
        review += notes
    elif category in PHRASE_CATEGORIES:
        accept, ph_reject, notes = _phrase(target, response_form)
        reject += ph_reject
        review += notes
    else:
        accept = [target]
        review.append(f"answer_category {category!r} 无展开规则，只生成原样写法")

    # 上游给的等价写法：直接进接受集，不再经规则推导
    seeded = [str(a).strip() for a in (answer.get("alternatives") or []) if str(a).strip()]
    accept += seeded
    accept += EXTRA.get(target, [])

    affix = f"{prefix} {suffix}"

    # affix 已供单位 → 剔除重复该单位的写法（R6）
    redundant = [v for v in accept if v != target and _affix_redundant(v, affix)]
    if redundant:
        accept = [v for v in accept if v not in redundant]
        reject += redundant
        review.append(
            f"卷面 affix {affix.strip()!r} 已供单位，以下重复写法移入拒绝集：{redundant}"
        )

    # affix 构成句框且卷面没写出单位 → 裸数字不成句，剔除（R7）
    if not carrier_supplies_unit(target, prefix, suffix):
        bare_forms = _bare_number_forms(target)
        bare = [v for v in accept if v != target and v.strip().lower() in bare_forms]
        if bare:
            accept = [v for v in accept if v not in bare]
            reject += bare
            review.append(
                f"卷面为句框 {prefix.strip()!r} ___ {suffix.strip()!r} 且未写出单位，"
                f"裸数字不成句，以下写法移入拒绝集：{bare}"
            )

    # 干扰项：上游标注的竞争答案（competing_candidates，equally_supported=False 的那些）。
    # 这是最重要的一条负向断言——被作废的旧价格若判成对，是永远发现不了的静默错误。
    # 价格类连同同类写法一起拒（拒 £1,200 就要一并拒 1200 / 1,200 pounds）。
    conflicts: list[str] = []
    accept_keys = {a.casefold() for a in accept}
    for raw in distractors or []:
        text = str(raw).strip()
        if not text:
            continue
        bare = text.lstrip(qin.CURRENCY_SYMBOLS).strip()
        if category == "price":
            expanded, _ = _price(bare, context, prefix, suffix)
        else:
            expanded = [text]
        for variant in expanded:
            if variant.casefold() in accept_keys:
                # 干扰项与接受集撞了：不能静默丢弃任何一侧，交人工裁决
                conflicts.append(variant)
            else:
                reject.append(variant)
    if conflicts:
        review.append(
            f"干扰项 {sorted(set(conflicts))} 与接受集重合，未收进拒绝集 —— "
            f"需人工判断该写法到底算对还是算错"
        )

    accept = _dedup(accept)
    reject = _dedup(reject)

    # 字数上限过滤：超限写法会被考官判错，收进接受集就是判分错误。
    # 被滤掉的写法转入拒绝集——它们是有价值的负向用例（考生真的会这么写）。
    over_limit = [v for v in accept if not within_word_limit(v, limit)]
    if over_limit:
        accept = [v for v in accept if v not in over_limit]
        reject += over_limit
        review.append(
            f"以下写法超出「{limit.text}」，已从接受集移入拒绝集：{over_limit}"
        )

    if not within_word_limit(target, limit):
        raise ValueError(
            f"Q{number}: 正确答案 {target!r} 本身就超出「{limit.text}」——"
            f"题面 rubric 与素材不匹配，需人工处置"
        )
    if not accept:
        raise ValueError(f"Q{number}: 过滤后接受集为空，target={target!r}")

    reject = _dedup(reject)

    # 硬断言：接受集与拒绝集不得相交。相交说明规则把干扰项放进了正确答案。
    overlap = {a.casefold() for a in accept} & {r.casefold() for r in reject}
    if overlap:
        raise ValueError(
            f"Q{number}: 接受集与拒绝集相交 {sorted(overlap)}，"
            f"target={target!r}。检查 DISTINCTIVE / EXTRA 或 correction 处理。"
        )

    return AnswerSpec(number=number, target=target, accept=accept, reject=reject, review=review)
