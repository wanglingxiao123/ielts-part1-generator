/**
 * Decision bar (design.md §4.1). Row order IS the signal priority — and it is
 * deliberately not score-first: total score is a single weighted scalar, and a
 * 3-4 point difference is not a decision, it just looks like one.
 */
import { useMemo } from 'react'
import type { CandidateFacts } from '@/domain/compare'
import { circled, SEVERITY_FLAG, SEVERITY_LABEL } from '@/domain/types'
import { assessUsability, READINESS_FLAG, READINESS_LABEL } from '@/domain/usability'

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
  const d = facts.distribution
  const m = facts.view.audit.metrics
  const usability = useMemo(() => assessUsability(d), [d])
  const wideGroups = facts.groups.groups.filter((g) => !g.ungrouped && g.canFormQuestion)

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

      {/* 1: a point the blind auditor could not hear at all — no question can be
          written from it, so this row leads. */}
      <Row rank={1} label="听不出来的点">
        {facts.unrecoverable > 0 ? (
          <span className="flag flag-bad">{facts.unrecoverable} 个，写不成题</span>
        ) : (
          <span className="flag flag-good">无</span>
        )}
      </Row>
      <Row rank={2} label="计划外的可考细节">
        {facts.unintendedTarget > 0 ? (
          <span className="flag flag-warn">{facts.unintendedTarget} 处，可能出现第二个说得通的答案</span>
        ) : (
          <span className="flag flag-good">无</span>
        )}
      </Row>
      <Row rank={3} label="出题就绪度">
        <span className={`flag ${READINESS_FLAG[usability.level]}`}>
          {READINESS_LABEL[usability.level]}
        </span>
        {usability.problemCount > 0 && (
          <span className="muted" style={{ fontSize: 11 }}>
            {' '}
            {usability.problemCount} 处待改
          </span>
        )}
      </Row>
      <Row rank={3} label="前后两组题量">
        <span>
          {d.firstHalfCount} / {d.secondHalfCount}
        </span>{' '}
        {d.balanced ? '✓' : <span className="flag flag-warn">相差过大</span>}
      </Row>
      <Row rank={4} label="能否成表格/表单题">
        {facts.groups.hasViableQuestionGroup ? (
          <span className="flag flag-good">
            可以
            {/* Only declared groups: the form_group=null bucket was never claimed
                to belong together, so its span is not a grouping property. */}
            {wideGroups.length > 0 && (
              <>
                （
                {wideGroups
                  .map((g) => `${g.name} 组 ${g.numbers.map((n) => circled(n)).join('')}`)
                  .join('、')}
                ）
              </>
            )}
          </span>
        ) : (
          <span className="flag flag-bad">不行，没有 3 个以上同类点</span>
        )}
        {facts.groups.groups.some((g) => g.spanWarn) && (
          <span className="flag flag-warn">有分组跨度太宽</span>
        )}
      </Row>
      <Row rank={5} label="评价指出的问题">
        {facts.defects.critical + facts.defects.major + facts.defects.minor === 0 ? (
          <span className="flag flag-good">无</span>
        ) : (
          <span style={{ fontSize: 11 }}>
            {(['critical', 'major', 'minor'] as const)
              .filter((sev) => facts.defects[sev] > 0)
              .map((sev) => (
                <span key={sev} className={`flag ${SEVERITY_FLAG[sev]}`} style={{ marginRight: 4 }}>
                  {SEVERITY_LABEL[sev]} {facts.defects[sev]}
                </span>
              ))}
          </span>
        )}
      </Row>
      <Row rank={7} label="篇幅">
        <span>
          {m.dialogue_words} 词 · {m.dialogue_turns} 轮
        </span>
      </Row>

      {/* Thumbnail distribution bar: density per 10th of the script. */}
      <div className="spark" title="缩略分布：每格是全篇十二分之一里的信息点数">
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
