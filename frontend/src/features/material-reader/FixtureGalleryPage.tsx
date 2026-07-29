/**
 * DEVELOPMENT HARNESS — not a product page.
 *
 * Side-by-side balanced vs clustered, on the SAME script with the SAME ten
 * points — only turn_index differs. This is the review gate from implement.md
 * phase 4: a stranger must be able to point at the clustered one within three
 * seconds without reading any code.
 *
 * Routed at /dev/fixtures under VITE_MOCK=1 only, and not in the nav: it shows
 * hand-built fixtures, which a reviewer will read as their own generated
 * material. Reviewers reach a preview via 场景选择 → 批次 → 阅读/对比.
 * See App.tsx.
 */
import { useMemo, useState } from 'react'
import { getThresholds } from '@/config/runtimeConfig'
import { computeDistribution } from '@/domain/distribution'
import { analyseFormGroups } from '@/domain/formGroups'
import { joinFromRecord } from '@/domain/joinArtifacts'
import type { ViewMaterial } from '@/domain/types'
import { buildRecord, type FixtureKind } from '@/mocks/fixtures'
import { UsabilityCompare } from '../compare/UsabilityCompare'
import { MaterialReader } from './MaterialReader'
import { QuestionTypePanel } from './QuestionTypePanel'
import { DistributionStrip } from './DistributionStrip'

const KINDS: Array<{ kind: FixtureKind; label: string; note: string }> = [
  { kind: 'balanced', label: '均衡（blueprint_valid）', note: '10 点铺满全篇' },
  { kind: 'clustered', label: '扎堆（clustered 变体）', note: '⑥⑦⑧ 挤在 turn 27–29' },
  { kind: 'failed', label: '评价判为不达标', note: 'critical 缺陷；仍可选用' },
  {
    kind: 'anchorMismatch',
    label: '锚点失配',
    note: '信息点 3 的 evidence 不在 turn 14；恰好只在 turn 10 出现 → 静默挪正',
  },
  {
    kind: 'anchorCaseDiffers',
    label: '锚点仅大小写不同',
    note: '后端 casefold 后认为合法；前端必须一致，不得报失配',
  },
  {
    kind: 'anchorUnresolvable',
    label: '锚点无法确定',
    note: '信息点 3 的 evidence 脚本里不存在 → 这一条旁注不显示，另九条照常',
  },
]

/**
 * 定位状况（DEV ONLY）。
 *
 * 产品页面上不存在这一块，也不该存在：用户看到的是成品。这里存在的理由只有一个——
 * 一条无法确定的锚点是我们自己的 bug，如果所有方向都咽下去，就再没人会发现它。
 * 另一条路径是控制台（domain/anchors.ts 的 `reportAnchorProblems`）。
 */
function AnchorDevPanel({ view, note }: { view: ViewMaterial; note: string }) {
  const clean = view.anchorRepairs.length === 0 && view.anchorOmissions.length === 0
  return (
    <div className="panel panel-pad" style={{ marginBottom: 8, fontSize: 12 }}>
      <div className="row">
        <strong>定位（DEV ONLY）</strong>
        <span className="muted">{note}</span>
      </div>
      {clean ? (
        <div className="muted">十个信息点都落在带着自己 evidence 的那一轮上，未作任何调整。</div>
      ) : (
        <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
          {view.anchorRepairs.map((r) => (
            <li key={`r${r.itemNumber}`}>
              第 {r.itemNumber} 题：blueprint 写 turn {r.declaredTurnIndex}，evidence 实际只在
              turn {r.turnIndex} 出现 → 已静默挪正（用户不可见）
            </li>
          ))}
          {view.anchorOmissions.map((o) => (
            <li key={`o${o.itemNumber}`}>
              第 {o.itemNumber} 题：
              {o.reason === 'not-found'
                ? 'evidence 在任何对话轮里都找不到'
                : `evidence 命中 turn ${o.matches.join('、')}，任选一个都是猜`}
              {' → '}这一条旁注不显示（用户不可见）；blueprint 仍是{' '}
              {view.blueprint.items.length} 个点
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

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
        <h2 style={{ margin: 0 }}>开发用固定样例</h2>
        {/* Says out loud that this is not the client's material — the previous
            title (夹具对照) meant nothing outside our own test vocabulary. */}
        <span className="flag flag-warn">DEV ONLY · 非生成结果</span>
        <span className="muted" style={{ fontSize: 12 }}>
          手工构造的样例，用于验证渲染；同一份脚本、同样十个信息点，只有 turn_index 不同
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
            <h3>出题就绪度对照</h3>
            <UsabilityCompare
              columns={[
                { label: '均衡', metrics: balanced.metrics },
                { label: '扎堆', metrics: clustered.metrics },
              ]}
            />
            {/* Dev-only escape hatch: the raw numbers stay reachable for
                debugging a threshold, but collapsed so the page still reads as
                a conclusion first. */}
            <details style={{ marginTop: 8 }}>
              <summary className="muted" style={{ fontSize: 11, cursor: 'pointer' }}>
                原始度量（调试用）
              </summary>
              <table className="qt-table" style={{ marginTop: 6 }}>
                <thead>
                  <tr>
                    <th />
                    <th>均衡</th>
                    <th>扎堆</th>
                  </tr>
                </thead>
                <tbody>
                  {(
                    [
                      ['gaps', (m: typeof balanced.metrics) => m.gaps.join(', ')],
                      ['maxGap', (m: typeof balanced.metrics) => String(m.maxGap)],
                      ['cv', (m: typeof balanced.metrics) => m.cv.toFixed(3)],
                      ['uniformity', (m: typeof balanced.metrics) => String(m.uniformity)],
                    ] as const
                  ).map(([label, fn]) => (
                    <tr key={label}>
                      <td className="mono">{label}</td>
                      <td className="mono">{fn(balanced.metrics)}</td>
                      <td className="mono">{fn(clustered.metrics)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
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
          {/* 定位状况：**开发者**通道，只存在于这一页（VITE_MOCK 才路由得到）。
              阅读页对这些事一个字都不说——静默挪正的用户不需要知道，挪不了的那一条直接不显示，
              客户的底线是「用户看到的永远是成品」。但一条挪不了的锚点说明我们自己的流水线
              产出了自相矛盾的构件，所以它必须在某处能被看见：这一块，加上
              domain/anchors.ts 里的 console.warn。 */}
          <AnchorDevPanel view={fullOne.view} note={fullOne.note} />
          <MaterialReader view={fullOne.view} height={700} />
        </>
      )}
    </div>
  )
}
