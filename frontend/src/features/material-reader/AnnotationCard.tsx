/**
 * Annotation card + cluster card (design.md §3.5 / §3.6).
 *
 * A cluster card is NOT a styling variant: it is the honesty patch. Once the
 * column has repositioned a tight run of points, it must stop presenting them
 * as independent evenly-spaced annotations and say so in the header.
 */
import type { BlueprintItem, Blueprint } from '@/contracts'
import { DISTRACTION_HINT, DISTRACTION_LABEL, distractionOf } from '@/domain/pointFacts'
import { circled, ITEM_TYPE_LABEL, needsSpelling } from '@/domain/types'
import type { PlacedCard } from './annotationLayout'

/**
 * 一个信息点的旁注。
 *
 * 先说「考什么 + 答案是什么」，这是命题人唯一必读的两项；badge 只在带信息时出现。
 *
 * 删掉的东西和原因：
 *   `非干扰`      纯否定，不带信息——没有 badge 就是不干扰。
 *   `第 N 组`     题组由 split_after 推导，旁注旁边重复一遍毫无用处。
 *   `未确认`      同上，是「已确认」的缺席，不需要自己的 badge；只有拼读点未确认
 *                 才是真问题（§3「关键信息须复述/确认」），那一条单独说。
 *   `item_form`   题型适配是整篇的判断，题型面板已经有一整张表，逐点重复只是噪音。
 *   `name`/`option` 等内部枚举 → 换成规范 §4B-3 的中文类型名。
 *   `旁注位置可疑`「这句话不在 turn N 里，旁注可能贴错位置」是把我们自己的标注 bug 摊给
 *                 命题人自查。客户的底线：能确定修的静默修好（domain/anchors.ts 按后端
 *                 同一条规则把锚点挪到真正带着这句话的那一轮），确定不了的干脆不显示这条
 *                 旁注。所以这个 badge 没有存在的余地——它只会出现在我们已经决定不显示的
 *                 旁注上。
 * turn N 留下：它是唯一的跳转坐标，旁注和原文靠它对上，客户团队也一直用它沟通。它显示的是
 * **解出来的**那一轮（joinArtifacts 把挪正后的 turn_index 写进 item 副本），所以点它一定
 * 能跳到真的带着这句话的地方。
 */
function ItemBody({ item, blueprint }: { item: BlueprintItem; blueprint: Blueprint }) {
  const distraction = distractionOf(item, blueprint)
  const spelling = needsSpelling(item.type)
  return (
    <div className="ann-item">
      <div className="ann-item-head">
        <span className="num mono">{circled(item.number)}</span>
        <span className="ann-type">{ITEM_TYPE_LABEL[item.type]}</span>
        <span className="target">{item.target}</span>
      </div>
      <div className="ann-badges">
        {spelling && (
          <span className="flag flag-neutral" title="需要拼读，答案按字母逐个听写">
            须拼读
          </span>
        )}
        {item.confirmed && (
          <span className="flag flag-good" title="对话中复述或确认过，一遍就能听清">
            有复述确认
          </span>
        )}
        {spelling && !item.confirmed && (
          <span className="flag flag-warn" title="拼读类信息只播一次且无人复述，考生极易听错">
            拼读却没人复述
          </span>
        )}
        {distraction && (
          <span className="flag flag-warn" title={DISTRACTION_HINT[distraction]}>
            {DISTRACTION_LABEL[distraction]}
          </span>
        )}
      </div>
      <div className="ann-ev">“{item.evidence}”</div>
      <div className="ann-meta">turn {item.turn_index}</div>
    </div>
  )
}

interface Props {
  card: PlacedCard
  /** Needed to name WHICH distraction mechanism a point uses (§4B-4). */
  blueprint: Blueprint
  selectedItem: number | null
  onSelect: (itemNumber: number, turnIndex: number) => void
  /** Measured by useAnnotationLayout; must sit on the positioned element. */
  cardRef?: (el: HTMLDivElement | null) => void
}

export function AnnotationCard({ card, blueprint, selectedItem, onSelect, cardRef }: Props) {
  const selected = card.items.some((i) => i.number === selectedItem)
  const anchorTurn = card.items[0]?.turn_index ?? card.turnIndexes[0] ?? 0

  return (
    <div
      className={
        'ann-card' + (card.isCluster ? ' cluster' : '') + (selected ? ' selected' : '')
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
          <span className="muted" style={{ fontWeight: 400 }}>
            · 考生来不及记
          </span>
        </div>
      )}
      {card.items.length > 1 && !card.isCluster && (
        <div className="ann-meta" style={{ marginTop: 0, marginBottom: 4 }}>
          同一句里有 {card.items.length} 个信息点（turn {card.turnIndexes[0]}）
        </div>
      )}
      {card.items.map((item) => (
        <ItemBody key={item.number} item={item} blueprint={blueprint} />
      ))}
    </div>
  )
}
