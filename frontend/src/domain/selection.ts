/**
 * 勾选状态与「每场景至少选 1 套」这条规则。
 *
 * 纯函数，页面只负责渲染。规则本身是客户写在底栏里的那句提示，含义是
 * **有材料的场景**每个都得选一套——不能因为某个场景一套都没生成出来就把整个提交
 * 卡住，那样用户除了重跑没有别的出路。所以判据是「本批次里有卡片的场景」，
 * 而不是「用户当初勾了几个场景」。
 */

export interface SelectionRule {
  /** 已选套数。 */
  selectedCount: number
  /** 有材料但一套都没选的场景 key，升序稳定。 */
  scenariosMissing: string[]
  /** 可以提交：至少选了一套，且没有漏掉任何有材料的场景。 */
  canSubmit: boolean
}

export function evaluateSelection(input: {
  /** 场景 key → 该场景下所有材料 id，顺序即卡片顺序。 */
  byScenario: ReadonlyMap<string, readonly string[]>
  selected: ReadonlySet<string>
}): SelectionRule {
  const missing: string[] = []
  let selectedCount = 0
  for (const [scenarioKey, ids] of input.byScenario) {
    const picked = ids.filter((id) => input.selected.has(id))
    selectedCount += picked.length
    if (ids.length > 0 && picked.length === 0) missing.push(scenarioKey)
  }
  return {
    selectedCount,
    scenariosMissing: missing,
    canSubmit: selectedCount > 0 && missing.length === 0,
  }
}

/** 勾选切换。Set 是不可变替换，让 zustand/useState 的引用比较能生效。 */
export function toggleSelection(
  selected: ReadonlySet<string>,
  materialId: string,
): Set<string> {
  const next = new Set(selected)
  if (next.has(materialId)) next.delete(materialId)
  else next.add(materialId)
  return next
}

/* ── 对比模式：在一个场景里点选 A / B ─────────────────────────────────────── */

export type ComparePick = readonly [string | null, string | null]

export const EMPTY_PICK: ComparePick = [null, null]

/**
 * 点一张卡在对比模式下的结果。
 *
 * 行为按客户的描述：先点的是 A（蓝），后点的是 B（紫），再点第三张时替换 B——
 * 保留 A 更符合「拿这一套跟别的比」的实际用法。再点已选中的那张则取消它。
 */
export function pickForCompare(pick: ComparePick, materialId: string): ComparePick {
  const [a, b] = pick
  if (a === materialId) return [b, null]
  if (b === materialId) return [a, null]
  if (a === null) return [materialId, null]
  if (b === null) return [a, materialId]
  return [a, materialId]
}

/** 两套都选齐了才能进并排对比。 */
export function comparePairReady(pick: ComparePick): pick is readonly [string, string] {
  return pick[0] !== null && pick[1] !== null
}
