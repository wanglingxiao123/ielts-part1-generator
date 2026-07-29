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
import { useMemo } from 'react'
import type { DistributionMetrics } from '@/domain/distribution'
import type { FormGroupAnalysis } from '@/domain/formGroups'
import { circled, ITEM_FORM_LABEL, type ViewMaterial } from '@/domain/types'
import { assessUsability, READINESS_FLAG, READINESS_LABEL } from '@/domain/usability'

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
  // Same metrics object the marks above are drawn from, so the picture and the
  // sentence beneath it cannot disagree.
  const verdict = useMemo(() => assessUsability(metrics), [metrics])
  const span = Math.max(1, metrics.dialogueTurnCount - 1)
  const pct = (ordinal: number) => `${(ordinal / span) * 100}%`

  const ticks: number[] = []
  for (let o = 0; o <= span; o += TICK_STEP) ticks.push(o)

  return (
    <div className="strip">
      <div className="strip-title">
        <span>信息点分布</span>
        <span className="muted" style={{ fontWeight: 400 }}>
          横轴＝对话进行到第几轮（不含旁白）· 点挨在一起，就是原文里真的挨在一起
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
            title={`这 ${g.size} 轮里没有可考的信息点`}
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
                title={`第 ${p.number} 题的信息在 turn ${p.turnIndex}，点击跳到原文`}
                onClick={() => onPickItem(p.number, p.turnIndex)}
              >
                {circled(p.number)}
              </button>
            </span>
          )
        })}

        {/* Length of the EMPTY stretches only, so nobody has to count turns
            (design.md §3.3). Every gap used to be numbered, which put a row of
            bare digits (3 4 2 2 8 …) under the axis with nothing saying what
            they counted. A number over shading reads as "this stretch is N
            turns long with no point in it" — the only gap a question-writer
            acts on. */}
        {!compact && (
          <div className="gap-row">
            {metrics.wideGaps.map((g) => (
              <span
                key={`gn${g.index}`}
                className="gap-num"
                style={{ left: pct((g.fromOrdinal + g.toOrdinal) / 2) }}
              >
                {g.size} 轮无考点
              </span>
            ))}
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
                    {g.name ? ` ${g.name}` : ''}：{g.numbers.map((n) => circled(n)).join('')}
                    {g.spanWarn ? ' ⚠ 跨度太宽，考生要跨半篇回忆' : ''}
                  </span>
                </div>
              )
            })}

        {playingOrdinal != null && (
          <div className="playhead" style={{ left: pct(playingOrdinal) }} />
        )}
      </div>

      {/* 结论，不是指标。同一份 metrics 既画上面的点位、也写这里的判断。 */}
      <div className="strip-verdict">
        <div className="verdict-headline">
          <span className={`flag ${READINESS_FLAG[verdict.level]}`}>
            {READINESS_LABEL[verdict.level]}
          </span>
          <span>{verdict.headline}</span>
        </div>
        <ul className="verdict-checks">
          {verdict.checks.map((c) => (
            <li key={c.key} className={c.level === 'ready' ? 'ok' : 'todo'}>
              <span className="verdict-mark">{c.level === 'ready' ? '✓' : '!'}</span>
              <span className="verdict-label">{c.label}</span>
              <span className="verdict-detail">{c.detail}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
