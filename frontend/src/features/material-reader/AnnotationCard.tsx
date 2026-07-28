/**
 * Annotation card + cluster card (design.md §3.5 / §3.6).
 *
 * A cluster card is NOT a styling variant: it is the honesty patch. Once the
 * column has repositioned a tight run of points, it must stop presenting them
 * as independent evenly-spaced annotations and say so in the header.
 */
import type { BlueprintItem } from '@/contracts'
import { circled, ITEM_FORM_GLYPH, ITEM_FORM_LABEL } from '@/domain/types'
import type { AnchorMismatch } from '@/domain/types'
import type { PlacedCard } from './annotationLayout'

function ItemBody({
  item,
  mismatch,
}: {
  item: BlueprintItem
  mismatch: AnchorMismatch | undefined
}) {
  return (
    <div className="ann-item">
      <div className="ann-item-head">
        <span className="num mono">{circled(item.number)}</span>
        <span className="target">{item.target}</span>
        <span className="flag flag-neutral" title={`item_form: ${item.item_form}`}>
          {ITEM_FORM_GLYPH[item.item_form]} {ITEM_FORM_LABEL[item.item_form]}
        </span>
        {item.form_group && <span className="flag flag-neutral">组 {item.form_group}</span>}
        {mismatch && (
          <span className="flag flag-bad" title={`evidence 不在 turn ${item.turn_index} 的文本中`}>
            锚点失配
          </span>
        )}
      </div>
      <div className="ann-meta">
        turn {item.turn_index} · {item.type} · 第 {item.group} 组 ·{' '}
        {item.confirmed ? '已确认' : '未确认'} · {item.distractor ? '干扰项' : '非干扰'}
      </div>
      <div className="ann-ev">“{item.evidence}”</div>
    </div>
  )
}

interface Props {
  card: PlacedCard
  selectedItem: number | null
  mismatches: AnchorMismatch[]
  onSelect: (itemNumber: number, turnIndex: number) => void
  /** Measured by useAnnotationLayout; must sit on the positioned element. */
  cardRef?: (el: HTMLDivElement | null) => void
}

export function AnnotationCard({ card, selectedItem, mismatches, onSelect, cardRef }: Props) {
  const selected = card.items.some((i) => i.number === selectedItem)
  const hasMismatch = card.items.some((i) => mismatches.some((m) => m.itemNumber === i.number))
  const anchorTurn = card.items[0]?.turn_index ?? card.turnIndexes[0] ?? 0

  return (
    <div
      className={
        'ann-card' +
        (card.isCluster ? ' cluster' : '') +
        (selected ? ' selected' : '') +
        (hasMismatch ? ' mismatch' : '')
      }
      ref={cardRef}
      style={{ top: card.top }}
      data-card-id={card.id}
      data-cluster={card.isCluster ? 'true' : 'false'}
      data-displacement={Math.round(card.displacement)}
      onClick={() => onSelect(card.items[0]!.number, anchorTurn)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') onSelect(card.items[0]!.number, anchorTurn)
      }}
    >
      {card.isCluster && (
        <div className="ann-cluster-head">
          ⚠ {card.items.length} 点集中于 turn {card.clusterTurnStart}–{card.clusterTurnEnd}
        </div>
      )}
      {card.items.length > 1 && !card.isCluster && (
        <div className="ann-meta" style={{ marginTop: 0, marginBottom: 4 }}>
          同一 turn {card.turnIndexes[0]} 上有 {card.items.length} 个点
        </div>
      )}
      {card.items.map((item) => (
        <ItemBody
          key={item.number}
          item={item}
          mismatch={mismatches.find((m) => m.itemNumber === item.number)}
        />
      ))}
    </div>
  )
}
