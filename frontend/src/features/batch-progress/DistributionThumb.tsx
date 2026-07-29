/**
 * 结果卡上的信息点时间轴缩略图。
 *
 * 取代原来那一排 1–10 编号圆点。那排圆点只说明「有十个点」——这件事每套材料都成立，
 * 所以它什么也没说。这张缩略图说的是**十个点落在对话的哪里**，客户要的正是
 * 「一眼区分均匀的好材料和扎堆的差材料，不用点进去看」。
 *
 * 和阅读页的完整分布条（DistributionStrip）是**同一份数据、同一套换算**：
 * 点位、扎堆判定、form_group 括号全部来自 `computeDistribution` /
 * `analyseFormGroups`，坐标换算走 `domain/distributionAxis.ts`。缩略图和完整图
 * 因此不可能给出互相矛盾的结论——这是刻意的约束，不要在这里另写一套摆位逻辑。
 *
 * 完整图那条设计性质在这里同样保留（design.md §3.3）：点按**真实 ordinal** 落位，
 * **不做水平避让**。挨在一起就是原文里真的挨在一起，重叠本身就是要看的信号。
 * ordinal 完全相同的点只做垂直堆叠（横坐标一动不动），否则同一轮里的第二个点会被
 * 像素级遮住。
 *
 * 高度预算：整块约 46px（客户给的上限是 40–50px，卡片不能被它压住）。所以扎堆用
 * 黄点表达而不再画一条带文字的色带，括号只留 form_group 一行、说明走 title。
 */
import type { DistributionMetrics } from '@/domain/distribution'
import {
  axisPercent,
  axisTicks,
  axisSpan,
  declaredGroupBrackets,
  pileIndex,
} from '@/domain/distributionAxis'
import type { FormGroupAnalysis } from '@/domain/formGroups'
import { circled, type ViewMaterial } from '@/domain/types'

interface Props {
  view: ViewMaterial
  metrics: DistributionMetrics
  groups: FormGroupAnalysis
  /** 需要看一眼的点号（黄点）。来自 cardPreview.flaggedPoints，不在这里另算。 */
  flagged: readonly number[]
}

export function DistributionThumb({ view, metrics, groups, flagged }: Props) {
  const span = axisSpan(metrics)
  const flaggedSet = new Set(flagged)
  const brackets = declaredGroupBrackets(view, groups, span)
  const placed = metrics.points.length
  const total = placed + metrics.unplacedNumbers.length

  return (
    <div className="dist-thumb">
      <div className="dist-thumb-label">
        <span>信息点分布（{placed}/{total}）</span>
        <span className="dist-thumb-span">对话 {metrics.dialogueTurnCount} 轮</span>
      </div>

      <div
        className="dist-thumb-axis"
        role="img"
        aria-label={
          `信息点在 ${metrics.dialogueTurnCount} 轮对话中的分布：` +
          (metrics.clusters.length > 0
            ? metrics.clusters
                .map((c) => `${c.numbers.length} 点挤在 turn ${c.turnStart}–${c.turnEnd}`)
                .join('；')
            : '没有扎堆') +
          (metrics.unplacedNumbers.length > 0
            ? `；${metrics.unplacedNumbers.length} 个点锚点无法定位`
            : '')
        }
      >
        <div className="dist-thumb-line" />

        {axisTicks(span).map((o) => (
          <span key={`t${o}`} className="dist-thumb-tick" style={{ left: axisPercent(o, span) }}>
            {o}
          </span>
        ))}

        {/* form_group 括号。只画**被声明过**的组：在 form_group=null 的桶上画括号
            等于断言那些点属于同一道题，而 null 恰恰否认这件事。 */}
        {brackets.map((b) => (
          <div
            key={b.key}
            className={`dist-thumb-bracket${b.warn ? ' warn' : ''}`}
            style={{ left: b.left, width: `max(10px, ${b.widthPercent}%)` }}
            title={b.label}
          />
        ))}

        {metrics.points.map((p, i) => {
          const pile = pileIndex(metrics.points, i)
          const warn = flaggedSet.has(p.number)
          return (
            <span
              key={p.number}
              className={`dist-thumb-dot${warn ? ' warn' : ''}`}
              data-point={p.number}
              style={{ left: axisPercent(p.ordinal, span), top: 9 - pile * 9 }}
              title={
                warn
                  ? `第 ${p.number} 题的信息在 turn ${p.turnIndex}，密度过高或需要看一眼`
                  : `第 ${p.number} 题的信息在 turn ${p.turnIndex}`
              }
            >
              {circled(p.number)}
            </span>
          )
        })}
      </div>
    </div>
  )
}
