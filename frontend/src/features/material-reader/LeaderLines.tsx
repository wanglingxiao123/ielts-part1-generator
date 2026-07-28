/**
 * SVG leader lines (design.md §3.5 step 6).
 *
 * The slope carries information: a steep line means the card is far from its
 * anchor, i.e. "this annotation is not beside its sentence". That is an honest
 * self-report of the avoidance the column just performed.
 */
import type { PlacedCard } from './annotationLayout'

interface Props {
  cards: PlacedCard[]
  /** x of the script column's right edge, within the layout box. */
  fromX: number
  /** x of the annotation column's left edge. */
  toX: number
  height: number
  selectedItem: number | null
}

export function LeaderLines({ cards, fromX, toX, height, selectedItem }: Props) {
  const dx = Math.max(12, toX - fromX)
  return (
    <svg className="leader-svg" width="100%" height={height} aria-hidden="true">
      {cards.flatMap((card) =>
        card.turnIndexes.map((turnIndex, i) => {
          const y0 = card.anchorTops?.[i] ?? card.anchorY
          const y1 = card.top + 14 + (card.isCluster ? 8 : 0)
          const selected = card.items.some((it) => it.number === selectedItem)
          const displaced = Math.abs(card.displacement) > 24
          return (
            <path
              key={`${card.id}-${turnIndex}`}
              d={`M ${fromX} ${y0} C ${fromX + dx * 0.45} ${y0}, ${toX - dx * 0.45} ${y1}, ${toX} ${y1}`}
              fill="none"
              stroke={selected ? '#1f6feb' : displaced ? '#b26a00' : '#c9d0d8'}
              strokeWidth={selected ? 1.8 : 1.2}
              strokeDasharray={displaced ? '4 3' : undefined}
            />
          )
        }),
      )}
    </svg>
  )
}
