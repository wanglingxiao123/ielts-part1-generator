/**
 * Side-by-side balanced vs clustered, on the SAME script with the SAME ten
 * points — only turn_index differs. This is the review gate from implement.md
 * phase 4: a stranger must be able to point at the clustered one within three
 * seconds without reading any code.
 */
import { useMemo, useState } from 'react'
import { getThresholds } from '@/config/runtimeConfig'
import { computeDistribution } from '@/domain/distribution'
import { analyseFormGroups } from '@/domain/formGroups'
import { joinFromRecord } from '@/domain/joinArtifacts'
import { buildRecord, type FixtureKind } from '@/mocks/fixtures'
import { MaterialReader } from './MaterialReader'
import { QuestionTypePanel } from './QuestionTypePanel'
import { DistributionStrip } from './DistributionStrip'

const KINDS: Array<{ kind: FixtureKind; label: string; note: string }> = [
  { kind: 'balanced', label: '均衡（blueprint_valid）', note: '10 点铺满全篇' },
  { kind: 'clustered', label: '扎堆（clustered 变体）', note: '⑥⑦⑧ 挤在 turn 27–29' },
  { kind: 'failed', label: 'FAIL（隔离）', note: 'critical 缺陷' },
  { kind: 'anchorMismatch', label: '锚点失配', note: '信息点 3 的 evidence 不在 turn 14' },
]

export function FixtureGalleryPage() {
  const [mode, setMode] = useState<'strips' | 'full'>('strips')
  const [full, setFull] = useState<FixtureKind>('clustered')
  const thresholds = getThresholds()

  const built = useMemo(
    () =>
      KINDS.map((k) => {
        const view = joinFromRecord(
          buildRecord(k.kind, {
            materialId: `fx-${k.kind}`,
            batchId: 'fx',
            scenarioKey: 'accommodation-rental',
            index: 0,
          }),
        )
        return {
          ...k,
          view,
          metrics: computeDistribution(view, thresholds),
          groups: analyseFormGroups(view, thresholds),
        }
      }),
    [thresholds],
  )

  const balanced = built[0]!
  const clustered = built[1]!
  const fullOne = built.find((b) => b.kind === full)!

  return (
    <div className="page-wide">
      <div className="row" style={{ marginBottom: 10 }}>
        <h2 style={{ margin: 0 }}>夹具对照</h2>
        <span className="muted" style={{ fontSize: 12 }}>
          同一份脚本、同样十个信息点，只有 turn_index 不同
        </span>
        <div className="spacer" style={{ flex: 1 }} />
        <button
          type="button"
          className={`btn btn-sm${mode === 'strips' ? ' btn-primary' : ''}`}
          onClick={() => setMode('strips')}
        >
          分布并排
        </button>
        <button
          type="button"
          className={`btn btn-sm${mode === 'full' ? ' btn-primary' : ''}`}
          onClick={() => setMode('full')}
        >
          完整阅读态
        </button>
      </div>

      {mode === 'strips' ? (
        <>
          <div className="split-2">
            {[balanced, clustered].map((b) => (
              <div key={b.kind} className="stack">
                <div className="panel panel-pad" style={{ paddingBottom: 6 }}>
                  <div className="row">
                    <strong>{b.label}</strong>
                    <span className="muted" style={{ fontSize: 12 }}>
                      {b.note}
                    </span>
                  </div>
                </div>
                <DistributionStrip
                  view={b.view}
                  metrics={b.metrics}
                  groups={b.groups}
                  selectedItem={null}
                  onPickItem={() => {}}
                />
                <QuestionTypePanel analysis={b.groups} />
              </div>
            ))}
          </div>

          <div className="panel panel-pad" style={{ marginTop: 12 }}>
            <h3>指标对照（domain/distribution.ts 的确定性输出）</h3>
            <table className="qt-table">
              <thead>
                <tr>
                  <th>指标</th>
                  <th>均衡</th>
                  <th>扎堆</th>
                </tr>
              </thead>
              <tbody>
                {(
                  [
                    ['间隔序列', (m: typeof balanced.metrics) => m.gaps.join(', ')],
                    ['最大间隔', (m: typeof balanced.metrics) => String(m.maxGap)],
                    ['间隔 CV', (m: typeof balanced.metrics) => m.cv.toFixed(3)],
                    ['均匀度', (m: typeof balanced.metrics) => `${m.uniformity}/100`],
                    [
                      '检出扎堆',
                      (m: typeof balanced.metrics) =>
                        m.clusters.length === 0
                          ? '无'
                          : m.clusters
                              .map((c) => `${c.numbers.join('/')} @ turn ${c.turnStart}–${c.turnEnd}`)
                              .join('; '),
                    ],
                    [
                      '前后段点数',
                      (m: typeof balanced.metrics) => `${m.firstHalfCount} / ${m.secondHalfCount}`,
                    ],
                  ] as const
                ).map(([label, fn]) => (
                  <tr key={label}>
                    <td>{label}</td>
                    <td className="mono">{fn(balanced.metrics)}</td>
                    <td className="mono">{fn(clustered.metrics)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <>
          <div className="row" style={{ marginBottom: 8 }}>
            {KINDS.map((k) => (
              <button
                key={k.kind}
                type="button"
                className={`btn btn-sm${full === k.kind ? ' btn-primary' : ''}`}
                onClick={() => setFull(k.kind)}
              >
                {k.label}
              </button>
            ))}
          </div>
          <MaterialReader view={fullOne.view} height={700} />
        </>
      )}
    </div>
  )
}
