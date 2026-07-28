/**
 * Annotation column layout (design.md §3.5). Pure — the React hook is a thin
 * wrapper so the six steps can be asserted against fixtures.
 *
 * The three-channel split (§3.2) is the whole point:
 *   overview strip → judge distribution, NEVER avoids overlap
 *   annotation column → read one point, DOES avoid overlap
 *   leader lines → slope reveals how far a card was displaced
 *
 * A conventional greedy push-down alone would spread three clustered points out
 * until they look identical to three evenly spread ones, destroying the exact
 * signal reviewers are hunting. Cluster merging (step 5) is what stops the
 * annotation column from lying once it has repositioned anything.
 */
import type { BlueprintItem } from '@/contracts'

export interface AnchorMeasurement {
  turnIndex: number
  /** offsetTop of the turn element within the shared scroll container. */
  anchorY: number
}

export interface LayoutInputCard {
  /** Stable id: `turn-<index>` for a single-turn card. */
  id: string
  turnIndexes: number[]
  items: BlueprintItem[]
  anchorY: number
  /** anchorY per entry of turnIndexes, so a cluster card can draw one leader
   *  line to each member turn instead of one to the group. */
  anchorTops: number[]
  height: number
}

export interface PlacedCard extends LayoutInputCard {
  top: number
  /** top - anchorY. Non-zero means "this card is not where it belongs". */
  displacement: number
  /** Merged because avoidance displaced a tight run of points. */
  isCluster: boolean
  clusterTurnStart: number
  clusterTurnEnd: number
}

export interface LayoutOptions {
  gap: number
  containerHeight: number
  clusterDispPx: number
  clusterSpan: number
  clusterMinPoints: number
  /** Height of a merged cluster card given N stacked items. */
  clusterHeightFor: (itemCount: number) => number
}

export const DEFAULT_LAYOUT_OPTIONS: Omit<LayoutOptions, 'containerHeight'> = {
  gap: 12,
  clusterDispPx: 24,
  clusterSpan: 3,
  clusterMinPoints: 3,
  clusterHeightFor: (n) => 34 + n * 46,
}

/**
 * Step 2: items anchored to the SAME turn become one card. Same-turn points are
 * not spread apart at all — they stack inside a single card, so "three points on
 * one turn" can never be rendered as three tidy separate rows.
 */
export function groupItemsIntoCards(
  itemsByTurn: Array<{ turnIndex: number; items: BlueprintItem[] }>,
  anchorY: (turnIndex: number) => number,
  heightOf: (turnIndex: number, itemCount: number) => number,
): LayoutInputCard[] {
  return itemsByTurn
    .filter((g) => g.items.length > 0)
    .map((g) => ({
      id: `turn-${g.turnIndex}`,
      turnIndexes: [g.turnIndex],
      items: [...g.items].sort((a, b) => a.number - b.number),
      anchorY: anchorY(g.turnIndex),
      anchorTops: [anchorY(g.turnIndex)],
      height: heightOf(g.turnIndex, g.items.length),
    }))
    .sort((a, b) => a.anchorY - b.anchorY)
}

/** Steps 3+4: forward greedy push-down, then reverse push-up on overflow. */
function place(cards: LayoutInputCard[], options: LayoutOptions): number[] {
  const tops: number[] = []
  let cursor = -Infinity
  cards.forEach((card, i) => {
    const top = Math.max(card.anchorY, cursor)
    tops[i] = top
    cursor = top + card.height + options.gap
  })

  // Step 4: overflow past the container bottom is pushed back up, spreading the
  // squeeze across both ends instead of clipping the tail.
  const last = cards.length - 1
  if (last >= 0) {
    const bottom = tops[last]! + cards[last]!.height
    if (bottom > options.containerHeight) {
      let limit = options.containerHeight - cards[last]!.height
      for (let i = last; i >= 0; i -= 1) {
        tops[i] = Math.min(tops[i]!, limit)
        limit = tops[i]! - cards[i]!.height - options.gap
      }
      // Never push above the container top.
      let floor = 0
      for (let i = 0; i < cards.length; i += 1) {
        tops[i] = Math.max(tops[i]!, floor)
        floor = tops[i]! + cards[i]!.height + options.gap
      }
    }
  }
  return tops
}

/**
 * Step 5: a run of consecutive cards whose anchor turns span <= clusterSpan and
 * which together hold >= clusterMinPoints points is merged into one labelled
 * cluster card, then the placement is re-run.
 *
 * DEVIATION from design.md §3.5, which gates merging on measured displacement
 * (`disp > CLUSTER_DISP_PX`) as well. That makes the honesty patch depend on
 * geometry: with the clustered fixture, turn heights left just enough room for
 * points 6/7/8 (turns 27–29) to be placed at zero displacement, so no cluster
 * card was produced and the column rendered three tightly-clustered points as
 * three ordinary, evenly-spaced cards — precisely the lie §3.2 exists to
 * prevent. Anchor proximity is a property of the blueprint, not of the current
 * viewport, so detection is now driven by it alone. Bonus: the merge set is now
 * identical by construction to domain/distribution.ts's clusters, so the
 * overview strip and the annotation column cannot disagree.
 *
 * `tops` is still used to report displacement for the leader lines.
 */
function mergeClusters(
  cards: LayoutInputCard[],
  options: LayoutOptions,
): { cards: LayoutInputCard[]; merged: boolean; clusterIds: Set<string> } {
  const clusterIds = new Set<string>()
  const out: LayoutInputCard[] = []
  let merged = false
  let i = 0

  while (i < cards.length) {
    let j = i
    while (
      j + 1 < cards.length &&
      Math.max(...cards[j + 1]!.turnIndexes) - Math.min(...cards[i]!.turnIndexes) <=
        options.clusterSpan
    ) {
      j += 1
    }
    const run = cards.slice(i, j + 1)
    const itemCount = run.reduce((n, c) => n + c.items.length, 0)
    const distinctTurns = new Set(run.flatMap((c) => c.turnIndexes)).size
    // A run of one card, or a run that is really just one turn, is not a cluster
    // of the kind §3.2 cares about — that is already handled by same-turn
    // stacking in step 2.
    if (run.length > 1 && distinctTurns > 1 && itemCount >= options.clusterMinPoints) {
      const pairs = run
        .flatMap((c) => c.turnIndexes.map((ti, k) => ({ ti, y: c.anchorTops[k] ?? c.anchorY })))
        .sort((a, b) => a.ti - b.ti)
      const turnIndexes = pairs.map((p) => p.ti)
      const id = `cluster-${turnIndexes[0]}-${turnIndexes[turnIndexes.length - 1]}`
      clusterIds.add(id)
      out.push({
        id,
        turnIndexes,
        items: run.flatMap((c) => c.items).sort((a, b) => a.number - b.number),
        anchorY: Math.min(...run.map((c) => c.anchorY)),
        anchorTops: pairs.map((p) => p.y),
        height: options.clusterHeightFor(itemCount),
      })
      merged = true
    } else {
      out.push(...run)
    }
    i = j + 1
  }
  return { cards: out, merged, clusterIds }
}

export function layoutAnnotations(
  inputCards: LayoutInputCard[],
  options: LayoutOptions,
): PlacedCard[] {
  if (inputCards.length === 0) return []
  const sorted = [...inputCards].sort((a, b) => a.anchorY - b.anchorY)

  const { cards, clusterIds } = mergeClusters(sorted, options)
  const finalTops = place(cards, options)

  return cards.map((card, i) => ({
    ...card,
    top: finalTops[i]!,
    displacement: finalTops[i]! - card.anchorY,
    isCluster: clusterIds.has(card.id),
    clusterTurnStart: Math.min(...card.turnIndexes),
    clusterTurnEnd: Math.max(...card.turnIndexes),
  }))
}
