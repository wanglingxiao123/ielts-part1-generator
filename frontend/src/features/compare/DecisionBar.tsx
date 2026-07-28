/**
 * Decision bar (design.md §4.1). Row order IS the signal priority — and it is
 * deliberately not score-first: total score is a single weighted scalar, and a
 * 3-4 point difference is not a decision, it just looks like one.
 */
import { getThresholds } from '@/config/runtimeConfig'
import type { CandidateFacts } from '@/domain/compare'

function Row({
  rank,
  label,
  children,
}: {
  rank: number
  label: string
  children: React.ReactNode
}) {
  return (
    <div className="row-line row">
      <span className="k" style={{ flex: 1 }}>
        <span className="rank">{rank}</span>
        {label}
      </span>
      <span>{children}</span>
    </div>
  )
}

export function DecisionBar({
  facts,
  scoreDiff,
  scoreDiffSignificant,
}: {
  facts: CandidateFacts
  scoreDiff: number
  scoreDiffSignificant: boolean
}) {
  const t = getThresholds()
  const d = facts.distribution
  const m = facts.view.audit.metrics

  return (
    <div className="decision-bar">
      <div className="row" style={{ marginBottom: 6 }}>
        <strong>{facts.label}</strong>
        <span className="flag flag-neutral">{facts.view.verdict}</span>
        <span>
          <strong className="mono">{facts.total}</strong> 分
          {scoreDiff !== 0 && (
            <span className="muted">
              {' '}
              ({scoreDiff > 0 ? '+' : ''}
              {scoreDiff}
              {!scoreDiffSignificant && ' 不显著'})
            </span>
          )}
        </span>
        {facts.view.degraded && <span className="flag flag-warn">degraded</span>}
      </div>

      <Row rank={1} label="不可回收点（盲测）">
        <strong className="mono">{facts.unrecoverable}</strong>{' '}
        {facts.unrecoverable > 0 ? (
          <span className="flag flag-bad">致命</span>
        ) : (
          <span className="flag flag-good">无</span>
        )}
      </Row>
      <Row rank={2} label="意外考点">
        <strong className="mono">{facts.unintendedTarget}</strong>{' '}
        {facts.unintendedTarget > 0 && <span className="flag flag-warn">易歧义</span>}
      </Row>
      <Row rank={3} label="分布均匀度 / CV / 最大间隔">
        <strong className="mono">{d.uniformity}</strong>{' '}
        <span className="mono muted">
          (CV {d.cv.toFixed(2)}
          {d.cvWarn ? ` >${t.CV_WARN}` : ''})
        </span>{' '}
        最大 <strong className="mono">{d.maxGap}</strong>
        {d.cvWarn && <span className="flag flag-warn">⚠</span>}
      </Row>
      <Row rank={3} label="前后段点数">
        <span className="mono">
          {d.firstHalfCount} / {d.secondHalfCount}
        </span>{' '}
        {d.balanced ? '✓' : <span className="flag flag-warn">失衡</span>}
      </Row>
      <Row rank={4} label="分组可行性">
        <span style={{ fontSize: 11 }}>
          {/* Only declared groups: the form_group=null bucket's span is not a
              grouping property, so printing it here made a decision signal out
              of noise. */}
          {facts.groups.groups
            .filter((g) => !g.ungrouped && g.numbers.length > 1)
            .map((g) => `${g.name} ${g.numbers.length}点跨${g.turnSpan}${g.spanWarn ? '⚠' : ''}`)
            .join(' · ')}
          {facts.groups.groups.some((g) => g.ungrouped) &&
            ` · 未分组 ${facts.groups.groups
              .filter((g) => g.ungrouped)
              .reduce((n, g) => n + g.numbers.length, 0)} 点`}
          {' · 多选 '}
          {facts.groups.multipleChoiceCount}
        </span>
        {!facts.groups.hasViableQuestionGroup && <span className="flag flag-bad">无可成题组</span>}
      </Row>
      <Row rank={5} label="缺陷计数">
        <span className="mono">
          {facts.defects.critical} critical
          {facts.defects.critical > 0 && ' ⚠'} / {facts.defects.major} major
          {facts.defects.major > 0 && ' ⚠'} / {facts.defects.minor} minor
        </span>
      </Row>
      <Row rank={7} label="篇幅">
        <span className="mono">
          {m.dialogue_words} 词 / {m.dialogue_turns} 轮
        </span>
      </Row>

      {/* Thumbnail distribution bar: density per 10th of the script. */}
      <div className="spark" title="缩略分布：每格为全篇 1/10 内的点数">
        {Array.from({ length: 12 }, (_, i) => {
          const lo = (i / 12) * d.dialogueTurnCount
          const hi = ((i + 1) / 12) * d.dialogueTurnCount
          const n = d.points.filter((p) => p.ordinal >= lo && p.ordinal < hi).length
          return (
            <i
              key={i}
              className={n >= 3 ? 'hot' : undefined}
              style={{ height: `${Math.min(100, n * 34)}%` }}
            />
          )
        })}
      </div>
    </div>
  )
}
