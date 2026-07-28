import { describe, expect, it } from 'vitest'
import type { BlueprintItem } from '@/contracts'
import { joinFromRecord } from '@/domain/joinArtifacts'
import { buildRecord } from '@/mocks/fixtures'
import {
  DEFAULT_LAYOUT_OPTIONS,
  groupItemsIntoCards,
  layoutAnnotations,
  type LayoutOptions,
  type PlacedCard,
} from './annotationLayout'

const TURN_H = 56
const CARD_H = 92

const options = (containerHeight: number): LayoutOptions => ({
  ...DEFAULT_LAYOUT_OPTIONS,
  containerHeight,
})

/** Anchors turns at a uniform pitch — enough to exercise the layout. */
const anchorY = (turnIndex: number) => turnIndex * TURN_H

function cardsFor(view: ReturnType<typeof joinFromRecord>) {
  const byTurn = view.turns
    .filter((t) => t.items.length > 0)
    .map((t) => ({ turnIndex: t.index, items: t.items }))
  return groupItemsIntoCards(byTurn, anchorY, () => CARD_H)
}

function overlaps(a: PlacedCard, b: PlacedCard): boolean {
  return a.top < b.top + b.height && b.top < a.top + a.height
}

function assertNoOverlap(placed: PlacedCard[]) {
  for (let i = 0; i < placed.length; i += 1) {
    for (let j = i + 1; j < placed.length; j += 1) {
      expect(
        overlaps(placed[i]!, placed[j]!),
        `${placed[i]!.id} overlaps ${placed[j]!.id}`,
      ).toBe(false)
    }
  }
}

const O = { batchId: 'b', scenarioKey: 's', index: 0 }
const balanced = joinFromRecord(buildRecord('balanced', { ...O, materialId: 'bal' }))
const clustered = joinFromRecord(buildRecord('clustered', { ...O, materialId: 'clu' }))

describe('layoutAnnotations — balanced fixture', () => {
  const placed = layoutAnnotations(cardsFor(balanced), options(43 * TURN_H))

  it('renders ten points across ten independent cards', () => {
    expect(placed).toHaveLength(10)
    expect(placed.flatMap((c) => c.items.map((i) => i.number)).sort((a, b) => a - b)).toEqual([
      1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
    ])
  })

  it('produces no overlapping cards', () => {
    assertNoOverlap(placed)
  })

  it('merges nothing: with no cluster there is nothing to be honest about', () => {
    expect(placed.filter((c) => c.isCluster)).toEqual([])
  })

  it('keeps every card close to its anchor', () => {
    for (const c of placed) {
      expect(Math.abs(c.displacement), c.id).toBeLessThanOrEqual(CARD_H)
    }
  })
})

describe('layoutAnnotations — clustered fixture', () => {
  const placed = layoutAnnotations(cardsFor(clustered), options(43 * TURN_H))

  it('produces no overlapping cards', () => {
    assertNoOverlap(placed)
  })

  it('merges 6/7/8 into one labelled cluster card spanning turn 27-29', () => {
    const cluster = placed.find((c) => c.isCluster)
    expect(cluster, 'expected a cluster card').toBeDefined()
    expect(cluster!.items.map((i) => i.number)).toEqual([6, 7, 8])
    expect(cluster!.clusterTurnStart).toBe(27)
    expect(cluster!.clusterTurnEnd).toBe(29)
    // One card for three points, not three cards pretending to be independent.
    expect(placed.filter((c) => c.items.some((i) => [6, 7, 8].includes(i.number)))).toHaveLength(1)
  })

  it('stacks points 4 and 5 in a single card because they share turn 12', () => {
    const shared = placed.find((c) => c.turnIndexes.includes(12))!
    expect(shared.items.map((i) => i.number)).toEqual([4, 5])
    expect(shared.isCluster).toBe(false)
  })
})

describe('layoutAnnotations — worst case: three points on one turn', () => {
  const bp = structuredClone(balanced.blueprint)
  for (const n of [6, 7, 8]) {
    const item = bp.items.find((i) => i.number === n)!
    item.turn_index = 29
    item.evidence = "he'd love a park nearby"
  }
  const view = joinFromRecord({
    ...buildRecord('balanced', { ...O, materialId: 'same-turn' }),
    blueprint: bp,
  })

  it('stacks them inside one card with no overlap', () => {
    const placed = layoutAnnotations(cardsFor(view), options(43 * TURN_H))
    assertNoOverlap(placed)
    const card = placed.find((c) => c.turnIndexes.includes(29))!
    expect(card.items.map((i) => i.number)).toEqual([6, 7, 8])
    expect(placed.filter((c) => c.turnIndexes.includes(29))).toHaveLength(1)
  })
})

describe('layoutAnnotations — mechanics', () => {
  const item = (number: number): BlueprintItem => ({
    ...balanced.blueprint.items[0]!,
    number,
  })

  it('pushes down only as far as needed and records the displacement', () => {
    const placed = layoutAnnotations(
      [
        { id: 'turn-1', turnIndexes: [1], items: [item(1)], anchorY: 0, anchorTops: [0], height: 100 },
        { id: 'turn-2', turnIndexes: [2], items: [item(2)], anchorY: 10, anchorTops: [10], height: 100 },
      ],
      { ...options(1000), clusterMinPoints: 99 },
    )
    expect(placed[0]!.top).toBe(0)
    expect(placed[0]!.displacement).toBe(0)
    expect(placed[1]!.top).toBe(112) // 0 + 100 + gap 12
    expect(placed[1]!.displacement).toBe(102)
  })

  it('pushes back up rather than overflowing the container', () => {
    const cards = [0, 1, 2].map((i) => ({
      id: `turn-${i}`,
      turnIndexes: [i],
      items: [item(i + 1)],
      anchorY: 250,
      anchorTops: [250],
      height: 100,
    }))
    const placed = layoutAnnotations(cards, { ...options(340), clusterMinPoints: 99 })
    assertNoOverlap(placed)
    const bottom = placed[placed.length - 1]!.top + placed[placed.length - 1]!.height
    expect(bottom).toBeLessThanOrEqual(340)
    expect(placed[0]!.top).toBeGreaterThanOrEqual(0)
  })

  it('does not merge a run whose anchors are far apart even when displaced', () => {
    // Displaced by stacking, but anchors 10 turns apart: not a cluster.
    const cards = [
      { id: 'turn-0', turnIndexes: [0], items: [item(1)], anchorY: 0, anchorTops: [0], height: 200 },
      { id: 'turn-10', turnIndexes: [10], items: [item(2)], anchorY: 20, anchorTops: [20], height: 100 },
      { id: 'turn-20', turnIndexes: [20], items: [item(3)], anchorY: 40, anchorTops: [40], height: 100 },
    ]
    const placed = layoutAnnotations(cards, options(2000))
    expect(placed.filter((c) => c.isCluster)).toEqual([])
    expect(placed).toHaveLength(3)
  })
})
