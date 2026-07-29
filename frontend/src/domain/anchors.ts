/**
 * 旁注定位：把 blueprint 的 `turn_index` 落到真正带着 evidence 的那一轮。
 *
 * 这是 `backend/deterministic/anchors.py` 那条规则的前端移植，**不是**另一套判据：
 *
 *   evidence 就在 `turns[turn_index]` 里            → 定位成立，原样用；
 *   evidence 恰好只在另外一轮里出现                 → 修正到那一轮（静默）；
 *   evidence 一处都没有，或出现在两轮以上           → 不猜。
 *
 * 三条都照抄后端（`anchor_holds` / `find_evidence_turns` / `repair_anchors`），因为
 * 两边一旦各写一套，页面和后端就会对「这条旁注贴对了没有」给出不同答案——而这正是
 * 旁注存在的意义所在。重复出现的句子是锚点存在的理由，命中两轮时猜一个等于把锚点的
 * 全部价值丢掉，且是悄悄丢掉。
 *
 * 匹配是**大小写不敏感**的，和后端两处实现一致（`validate_part1.py` 的 `anchor_ok`
 * 与 `anchors.py` 的 `_carries` 都对两侧 casefold）。原来前端用 `indexOf` 精确匹配，
 * 于是一份校验器合法通过的材料，只因为首字母大小写不同就会被前端报成「标错位置」——
 * 一条纯属虚报的告警，而且很可能是常见情形。
 *
 * 旁白轮（speaker1）不是合格的落点：contract 要求锚点指向非 speaker1 轮，旁白本来就
 * 不该携带答案信息。所以指向旁白的锚点走「定位不成立」，再按上面的规则决定修正还是剔除。
 *
 * 这一层只决定**显示**。blueprint 本身一个字都不改（校验器要求恰好 10 个信息点），
 * 见 joinArtifacts.ts 里 `displayTurnOf` 的说明。
 */
import type { BlueprintItem, SpeakerId } from '@/contracts'

/** 旁白（规范 §4B-5）。 */
const NARRATOR: SpeakerId = 'speaker1'

/** joinArtifacts 在 ViewTurn 存在之前就要用它，所以只要求 material JSON 的那两个字段。 */
export interface AnchorTurn {
  speaker: string
  text: string
}

export interface EvidenceSpan {
  /** 在**原文**里的字符下标，可以直接拿去切 <mark>。 */
  start: number
  end: number
}

/**
 * 在 `text` 里找 `evidence`，大小写不敏感，返回原文坐标。
 *
 * 先试精确匹配：绝大多数情况会在这里命中，且下标天然正确。退到小写比较时只在
 * **折叠不改变长度**的前提下才敢把小写串上的下标当成原文下标——`'İ'.toLowerCase()`
 * 会变成两个码元，那种串上的下标搬回原文就是错的，宁可当作没找到（后果是这条旁注被
 * 剔除，而不是高亮到错的字上）。
 */
export function locateEvidence(text: string, evidence: string): EvidenceSpan | null {
  if (!evidence.trim()) return null
  const exact = text.indexOf(evidence)
  if (exact >= 0) return { start: exact, end: exact + evidence.length }

  const lowerText = text.toLowerCase()
  const lowerEvidence = evidence.toLowerCase()
  if (lowerText.length !== text.length || lowerEvidence.length !== evidence.length) return null
  const at = lowerText.indexOf(lowerEvidence)
  return at < 0 ? null : { start: at, end: at + evidence.length }
}

/** 这一轮能不能作为锚点落点：非旁白，且确实带着这句话。 */
function carries(turn: AnchorTurn | undefined, evidence: string): EvidenceSpan | null {
  if (!turn || turn.speaker === NARRATOR) return null
  return locateEvidence(turn.text, evidence)
}

/** `turns[index]` 是否真的带着 evidence——「旁注贴对了」的唯一定义。 */
export function anchorHolds(
  turns: readonly AnchorTurn[],
  index: number,
  evidence: string,
): boolean {
  if (!Number.isInteger(index) || index < 0 || index >= turns.length) return false
  return carries(turns[index], evidence) !== null
}

/** 所有带着这句话的合格轮次下标。 */
export function findEvidenceTurns(turns: readonly AnchorTurn[], evidence: string): number[] {
  if (!evidence.trim()) return []
  const out: number[] = []
  turns.forEach((turn, index) => {
    if (carries(turn, evidence) !== null) out.push(index)
  })
  return out
}

/** 一个信息点最终显示在哪一轮、高亮哪一段。 */
export interface AnchorPlacement {
  itemNumber: number
  turnIndex: number
  span: EvidenceSpan
}

/** 静默修正过的定位。用户不需要知道；开发者需要。 */
export interface AnchorRepair {
  itemNumber: number
  /** blueprint 声明的轮次。 */
  declaredTurnIndex: number
  /** 实际带着这句话的轮次。 */
  turnIndex: number
  evidence: string
}

/** 无法确定修正、因此这一次不显示的定位。用户不需要知道；开发者需要。 */
export interface AnchorOmission {
  itemNumber: number
  declaredTurnIndex: number
  /** 'not-found'：一处都没有；'ambiguous'：命中多轮，任选一个都是猜。 */
  reason: 'not-found' | 'ambiguous'
  evidence: string
  /** 'ambiguous' 时命中的轮次，便于开发者一眼看出是哪几句重复了。 */
  matches: number[]
}

export interface AnchorResolution {
  placements: AnchorPlacement[]
  repairs: AnchorRepair[]
  omissions: AnchorOmission[]
}

/**
 * 按上面那条规则解出十个点的显示位置。
 *
 * 返回值里没有「有问题但照样显示」这一档：一条定位不成立又修不了的旁注，贴在任何一句
 * 旁边都是错的，所以它不进 `placements`。剩下九条照常显示——客户的底线是「用户看到的
 * 永远是成品」，而九条对的旁注加一句不显示，比十条里混一条错的更接近成品。
 */
export function resolveAnchors(
  turns: readonly AnchorTurn[],
  items: readonly BlueprintItem[],
): AnchorResolution {
  const placements: AnchorPlacement[] = []
  const repairs: AnchorRepair[] = []
  const omissions: AnchorOmission[] = []

  for (const item of items) {
    const declared = item.turn_index
    const held = anchorHolds(turns, declared, item.evidence)
    if (held) {
      placements.push({
        itemNumber: item.number,
        turnIndex: declared,
        span: carries(turns[declared], item.evidence)!,
      })
      continue
    }

    const hits = findEvidenceTurns(turns, item.evidence)
    if (hits.length === 1) {
      const turnIndex = hits[0]!
      placements.push({
        itemNumber: item.number,
        turnIndex,
        span: carries(turns[turnIndex], item.evidence)!,
      })
      repairs.push({
        itemNumber: item.number,
        declaredTurnIndex: declared,
        turnIndex,
        evidence: item.evidence,
      })
      continue
    }

    omissions.push({
      itemNumber: item.number,
      declaredTurnIndex: declared,
      reason: hits.length === 0 ? 'not-found' : 'ambiguous',
      evidence: item.evidence,
      matches: hits,
    })
  }

  return { placements, repairs, omissions }
}

/* ── 开发者通道 ─────────────────────────────────────────────────────────────── */

/** 同一套材料只播报一次：view 会随每次勾选重新 join。 */
const reported = new Set<string>()

/**
 * 把定位问题送到**开发者**面前，而不是用户面前。
 *
 * 剔除一条旁注意味着我们自己的流水线产出了自相矛盾的构件。用户不该看到这件事（客户
 * 明确要求：不要把「我可能标错了你自己检查一下」给用户），但如果所有方向都咽下去，
 * 就再没人会发现它。所以：页面上一个字都不说，控制台说清楚；`/dev/fixtures` 上另有
 * 一块只在 VITE_MOCK 下存在的清单，方便对着样例核对。
 */
export function reportAnchorProblems(view: {
  materialId: string
  anchorRepairs: readonly AnchorRepair[]
  anchorOmissions: readonly AnchorOmission[]
}): void {
  if (view.anchorRepairs.length === 0 && view.anchorOmissions.length === 0) return
  const key = [
    view.materialId,
    ...view.anchorRepairs.map((r) => `r${r.itemNumber}:${r.declaredTurnIndex}>${r.turnIndex}`),
    ...view.anchorOmissions.map((o) => `o${o.itemNumber}:${o.reason}`),
  ].join('|')
  if (reported.has(key)) return
  reported.add(key)

  if (view.anchorRepairs.length > 0) {
    console.debug(
      `[anchors] ${view.materialId}: ${view.anchorRepairs.length} anchor(s) relocated to the turn that carries the evidence`,
      view.anchorRepairs,
    )
  }
  if (view.anchorOmissions.length > 0) {
    console.warn(
      `[anchors] ${view.materialId}: ${view.anchorOmissions.length} annotation(s) hidden — the blueprint anchor cannot be resolved and guessing would put the note beside the wrong sentence. The stored blueprint still has all ten items; only the display drops these.`,
      view.anchorOmissions,
    )
  }
}

/** 测试用：清掉播报去重表。 */
export function resetAnchorReporting(): void {
  reported.clear()
}
