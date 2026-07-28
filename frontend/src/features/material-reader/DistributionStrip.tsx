/**
 * Distribution overview strip (design.md §3.3).
 *
 * This is the channel that answers "are the ten points spread across the whole
 * script". Its defining property: point marks are placed at their TRUE ordinal
 * position and DO NOT avoid overlap. Three points on adjacent turns render as
 * three nearly-fused marks under a bold shaded band. Do not "fix" the overlap —
 * the overlap IS the finding.
 *
 * sticky at the top of the scroll container so the judgement never requires
 * scrolling (prd R4).
 */
import { getThresholds } from '@/config/runtimeConfig'
import type { DistributionMetrics } from '@/domain/distribution'
import type { FormGroupAnalysis } from '@/domain/formGroups'
import { circled, ITEM_FORM_LABEL, type ViewMaterial } from '@/domain/types'

interface Props {
  view: ViewMaterial
  metrics: DistributionMetrics
  groups: FormGroupAnalysis
  selectedItem: number | null
  onPickItem: (itemNumber: number, turnIndex: number) => void
  /** Playback pointer, in dialogue-ordinal space. */
  playingOrdinal?: number | null
  compact?: boolean
}

const TICK_STEP = 8

export function DistributionStrip({
  view,
  metrics,
  groups,
  selectedItem,
  onPickItem,
  playingOrdinal,
  compact,
}: Props) {
  const t = getThresholds()
  const span = Math.max(1, metrics.dialogueTurnCount - 1)
  const pct = (ordinal: number) => `${(ordinal / span) * 100}%`

  const ticks: number[] = []
  for (let o = 0; o <= span; o += TICK_STEP) ticks.push(o)

  return (
    <div className="strip">
      <div className="strip-title">
        <span>信息点分布</span>
        <span className="muted" style={{ fontWeight: 400 }}>
          横轴为对话轮次（排除旁白）· 点位不避让，重叠即为扎堆信号
        </span>
      </div>

      <div
        className="strip-axis"
        style={
          compact
            ? { height: 48 }
            : {
                height:
                  60 +
                  Math.max(
                    1,
                    groups.groups.filter((g) => !g.ungrouped && g.numbers.length > 1).length,
                  ) *
                    22,
              }
        }
      >
        <div className="axis-line" />

        {ticks.map((o) => (
          <div key={`t${o}`}>
            <div className="axis-tick" style={{ left: pct(o) }} />
            <div className="axis-tick-label" style={{ left: pct(o) }}>
              {o}
            </div>
          </div>
        ))}

        {/* Wide-gap hatching: the empty stretches, marked before the points so
            the marks stay on top. */}
        {metrics.wideGaps.map((g) => (
          <div
            key={`g${g.index}`}
            className="gap-shade"
            style={{
              left: pct(g.fromOrdinal),
              width: `${((g.toOrdinal - g.fromOrdinal) / span) * 100}%`,
            }}
            title={`${g.size} 轮空档（对话轮次 ${g.fromOrdinal}→${g.toOrdinal}）`}
          />
        ))}

        {/* Cluster shading. Same detection as the annotation column, so the two
            channels cannot contradict each other. */}
        {metrics.clusters.map((c) => (
          <div key={`c${c.turnStart}`}>
            <div
              className="cluster-shade"
              style={{
                left: `calc(${pct(c.ordinalStart)} - 9px)`,
                width: `calc(${((c.ordinalEnd - c.ordinalStart) / span) * 100}% + 18px)`,
                height: 36 + (metrics.points.filter((p) => c.numbers.includes(p.number)).length - 1) * 4,
              }}
            />
            <div
              className="cluster-shade-label"
              style={{ left: pct((c.ordinalStart + c.ordinalEnd) / 2) }}
            >
              ⚠ {c.numbers.length} 点挤在 turn {c.turnStart}–{c.turnEnd}
            </div>
          </div>
        ))}

        {metrics.splitOrdinal !== null && (
          <div className="axis-split" style={{ left: pct(metrics.splitOrdinal) }} />
        )}

        {metrics.points.map((p, i) => {
          // Vertical pile index among points sharing this exact ordinal. Two
          // items on one turn would otherwise be pixel-identical and the second
          // invisible — hiding the worst case behind the mark that looks fine.
          const pile = metrics.points.slice(0, i).filter((q) => q.ordinal === p.ordinal).length
          return (
            <span key={p.number}>
              {pile === 0 && <span className="axis-stem" style={{ left: pct(p.ordinal) }} />}
              <button
                type="button"
                className={`axis-point${selectedItem === p.number ? '' : ' dim'}`}
                style={{ left: pct(p.ordinal), top: 16 - pile * 13 }}
                title={`信息点 ${p.number} · turn ${p.turnIndex} · 对话轮次 ${p.ordinal}`}
                onClick={() => onPickItem(p.number, p.turnIndex)}
              >
                {circled(p.number)}
              </button>
            </span>
          )
        })}

        {/* Gap numbers so nobody has to count turns (design.md §3.3). */}
        {!compact && (
          <div className="gap-row">
            {metrics.gaps.map((g, i) => {
              const fromOrd = i === 0 ? 0 : metrics.points[i - 1]!.ordinal
              const toOrd = i === metrics.points.length ? span : metrics.points[i]!.ordinal
              return (
                <span
                  key={`gn${i}`}
                  className={`gap-num${i === metrics.maxGapIndex ? ' max' : ''}`}
                  style={{ left: pct((fromOrd + toOrd) / 2) }}
                  title={i === metrics.maxGapIndex ? '最大间隔' : undefined}
                >
                  {g}
                  {i === metrics.maxGapIndex ? '◀' : ''}
                </span>
              )
            })}
          </div>
        )}

        {/* form_group spans: bracket length == the group's turn span, which is
            the point — a wide bracket means the candidate must recall across
            half the audio to fill that table. One row per group so labels
            cannot collide.

            Only DECLARED groups get a bracket. Drawing one over the
            form_group=null bucket would assert that those points belong
            together, which is exactly what a null form_group denies. */}
        {!compact &&
          groups.groups
            .filter((g) => !g.ungrouped && g.numbers.length > 1)
            .map((g, gi) => {
              const startOrd = view.turns[g.turnStart]?.dialogueOrdinal ?? 0
              const endOrd = view.turns[g.turnEnd]?.dialogueOrdinal ?? startOrd
              return (
                <div
                  key={`fg${g.name ?? 'null'}-${g.itemForm}`}
                  className={`group-span${g.spanWarn ? ' warn' : ''}`}
                  style={{
                    left: pct(startOrd),
                    width: `max(14px, ${((endOrd - startOrd) / span) * 100}%)`,
                    top: 50 + gi * 22,
                  }}
                >
                  <span>
                    {ITEM_FORM_LABEL[g.itemForm]}
                    {g.name ? ` 组 ${g.name}` : ''} {g.numbers.length}点 跨{g.turnSpan}
                    {g.spanWarn ? ' ⚠ 跨度过大' : ''}
                  </span>
                </div>
              )
            })}

        {playingOrdinal != null && (
          <div className="playhead" style={{ left: pct(playingOrdinal) }} />
        )}
      </div>

      <div className="strip-metrics">
        <span className="metric">
          前段 <strong>{metrics.firstHalfCount}</strong> / 后段{' '}
          <strong>{metrics.secondHalfCount}</strong>{' '}
          {metrics.balanced ? (
            <span className="flag flag-good">均衡</span>
          ) : (
            <span className="flag flag-warn">失衡</span>
          )}
        </span>
        <span className="metric">
          最大间隔 <strong>{metrics.maxGap}</strong> / 最小 <strong>{metrics.minGap}</strong>
        </span>
        <span className="metric">
          间隔 CV <strong>{metrics.cv.toFixed(2)}</strong>{' '}
          {metrics.cvWarn && <span className="flag flag-warn">超阈值 {t.CV_WARN}</span>}
        </span>
        <span className="metric">
          均匀度 <strong>{metrics.uniformity}</strong>/100
          {!t.CALIBRATED && (
            <span className="flag flag-neutral" title="阈值尚无真题基线，需人工校准">
              参考值·阈值待校准
            </span>
          )}
        </span>
        {metrics.clusters.length > 0 && (
          <span className="flag flag-warn">检出 {metrics.clusters.length} 处扎堆</span>
        )}
      </div>

      {metrics.notes.length > 0 && (
        <div className="strip-notes">⚠ {metrics.notes.join('；')}</div>
      )}
    </div>
  )
}
