import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { api } from '@/api/endpoints'
import { ApiError } from '@/api/http'
import { scenarioMeta } from '@/config/scenarioMeta'
import { getThresholds } from '@/config/runtimeConfig'
import type { MaterialRecord } from '@/contracts/api'
import { buildFacts, compareCandidates } from '@/domain/compare'
import type { DistributionMetrics } from '@/domain/distribution'
import { computeDistribution } from '@/domain/distribution'
import { analyseFormGroups } from '@/domain/formGroups'
import { joinFromRecord } from '@/domain/joinArtifacts'
import { SEVERITY_LABEL } from '@/domain/types'
import { useBatchStore } from '@/stores/batchStore'
import { MaterialReader } from '../material-reader/MaterialReader'
import { DecisionBar } from './DecisionBar'
import { SelectDialog } from './SelectDialog'
import { UsabilityCompare } from './UsabilityCompare'

const LABELS = ['候选 A', '候选 B', '候选 C', '候选 D']

export function ComparePage() {
  const { scenarioKey } = useParams<{ scenarioKey: string }>()
  // ?a=&b= is how the results page hands over the two cards the user point-
  // selected. Absent (a direct link, a bookmark) it falls back to the first two.
  const [search] = useSearchParams()
  const thresholds = getThresholds()

  // Subscribe to the two stable references and derive with useMemo. A selector
  // that builds a new array on every call fails Zustand's snapshot caching and
  // loops forever ("The result of getSnapshot should be cached").
  const itemOrder = useBatchStore((s) => s.itemOrder)
  const materials = useBatchStore((s) => s.materials)
  const fromStore = useMemo(
    () =>
      itemOrder
        .map((id) => materials[id])
        .filter((m): m is MaterialRecord => m !== undefined && m.scenario_key === scenarioKey),
    [itemOrder, materials, scenarioKey],
  )
  const [records, setRecords] = useState<MaterialRecord[]>(fromStore)
  const [syncScroll, setSyncScroll] = useState(true)
  const [dialogFor, setDialogFor] = useState<MaterialRecord | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selectError, setSelectError] = useState<string | null>(null)
  const [jump, setJump] = useState<Record<string, { turnIndex: number; nonce: number }>>({})

  useEffect(() => {
    if (fromStore.length > 0) {
      setRecords(fromStore)
      return
    }
    void api
      .listMaterials({ scenario_key: scenarioKey })
      .then((res) => setRecords(res.materials.filter((m) => m.scenario_key === scenarioKey)))
      .catch(() => setRecords([]))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scenarioKey, fromStore.length])

  // Every material is comparable now, audit-rejected ones included: the client's
  // rule is that a flawed material is shown with its shortcomings stated, and
  // hiding it here would silently remove the very comparison a reviewer wants
  // ("is the flawed one still better than the alternative?").
  const candidates = records

  const views = useMemo(() => candidates.map(joinFromRecord), [candidates])
  const facts = useMemo(
    () =>
      views.map((v, i) =>
        buildFacts(
          LABELS[i] ?? `候选 ${i + 1}`,
          v,
          computeDistribution(v, thresholds),
          analyseFormGroups(v, thresholds),
        ),
      ),
    [views, thresholds],
  )

  const [pair, setPair] = useState<[number, number]>([0, 1])

  // Honour ?a=&b= once the records are in hand. Runs on id, not on index, so a
  // late-arriving material cannot shift the pair out from under the user.
  useEffect(() => {
    const a = candidates.findIndex((c) => c.material_id === search.get('a'))
    const b = candidates.findIndex((c) => c.material_id === search.get('b'))
    if (a >= 0 && b >= 0 && a !== b) setPair([a, b])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [records, search])

  const comparison = useMemo(() => {
    const a = facts[pair[0]]
    const b = facts[pair[1]]
    if (!a || !b) return null
    return compareCandidates(a, b, thresholds)
  }, [facts, pair, thresholds])

  const doSelect = useCallback(async (record: MaterialRecord) => {
    try {
      await api.selectMaterial(record.material_id)
      setSelectedId(record.material_id)
      setSelectError(null)
    } catch (err) {
      if (err instanceof ApiError && err.code === 'ALREADY_SELECTED') {
        setSelectedId(record.material_id)
        setSelectError(err.message)
      } else {
        // Notably SELECT_NOT_IMPLEMENTED: selectedId stays null so the "已选定,
        // 语音合成已触发" banner does NOT appear. Claiming a selection the
        // backend never recorded is the one failure mode worth guarding here.
        setSelectError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setDialogFor(null)
    }
  }, [])

  if (records.length === 0) {
    return (
      <div className="page">
        <div className="panel panel-pad">本场景暂无材料。</div>
      </div>
    )
  }

  const showPair = candidates.length >= 2 && comparison !== null

  return (
    <div className="page-wide">
      <div className="row" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>
          {scenarioMeta(scenarioKey ?? '').titleZh} — {candidates.length} 套候选
        </h2>
        <label style={{ fontSize: 12 }}>
          <input
            type="checkbox"
            checked={syncScroll}
            onChange={() => setSyncScroll((v) => !v)}
          />{' '}
          同步滚动
        </label>
        {candidates.length > 2 && (
          <span className="row" style={{ gap: 4 }}>
            <span className="muted" style={{ fontSize: 12 }}>
              切换对比：
            </span>
            {candidates.map((_, i) => (
              <button
                key={i}
                type="button"
                className={`btn btn-sm${pair.includes(i) ? ' btn-primary' : ''}`}
                onClick={() => setPair(([a]) => (a === i ? [a, i] : [a, i]))}
              >
                {LABELS[i]}
              </button>
            ))}
          </span>
        )}
      </div>

      {selectError && (
        <div className="banner banner-warn">
          <strong>选定结果</strong>
          <div>{selectError}</div>
        </div>
      )}

      {selectedId && (
        <div className="banner banner-good">
          <strong>已选定，语音合成已触发</strong>
          <div>
            未选中的候选已标注为弃用，不再占据主视图。
            <Link className="btn btn-sm" to={`/materials/${selectedId}`} style={{ marginLeft: 8 }}>
              打开选定材料（含音频）
            </Link>
          </div>
        </div>
      )}

      {candidates.length === 1 && (
        <div className="banner banner-warn">
          <strong>本场景 1/2 套就绪，等待中</strong>
          <div>凑不齐候选时不提供误导性的对比；如确认就用这一套，请显式选定。</div>
        </div>
      )}

      {showPair && (
        <div className="panel panel-pad" style={{ marginBottom: 10 }}>
          <h3>哪一套更好出题</h3>
          <div>{comparison.summary}</div>
          <ul style={{ margin: '6px 0 0 18px', padding: 0, fontSize: 12 }} className="muted">
            {comparison.reasons.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
          {/* Rows are the four questions a question-writer asks; each cell is
              that candidate's answer, in their vocabulary. Same source as each
              candidate's own distribution strip, so the two cannot disagree. */}
          <UsabilityCompare
            columns={pair
              .map((idx) => {
                const f = facts[idx]
                return f ? { label: f.label, metrics: f.distribution } : null
              })
              .filter((c): c is { label: string; metrics: DistributionMetrics } => c !== null)}
          />
          <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
            结论由规则推出，不经模型；同一份数据同时驱动上方结论与每套自己的分布预览。
          </div>
        </div>
      )}

      <div
        className="cmp-cols"
        style={{
          gridTemplateColumns: showPair ? '1fr 1fr' : '1fr',
        }}
      >
        {(showPair ? pair : [0]).map((idx, side) => {
          const f = facts[idx]
          const record = candidates[idx]
          const view = views[idx]
          if (!f || !record || !view) return null
          const discarded = selectedId !== null && selectedId !== record.material_id
          return (
            <div
              key={record.material_id}
              className="cmp-col"
              style={{ opacity: discarded ? 0.45 : 1 }}
            >
              {/* subgrid rows keep the two candidates' decision bar / reader /
                  action button on the same baseline even when one side has more
                  findings than the other */}
              <div className="panel panel-pad">
                <DecisionBar
                  facts={f}
                  scoreDiff={
                    comparison ? (side === 0 ? comparison.scoreDiff : -comparison.scoreDiff) : 0
                  }
                  scoreDiffSignificant={comparison?.scoreDiffSignificant ?? false}
                />
                {comparison && comparison.dimensionDeltas.length > 0 && (
                  <div className="dim-diff" style={{ marginTop: 8 }}>
                    <h3>
                      评分差别明显的方面
                      <span className="muted" style={{ fontWeight: 400 }}>
                        （另有 {comparison.hiddenDimensionCount} 项两套差不多，未列出）
                      </span>
                    </h3>
                    {comparison.dimensionDeltas.map((d) => {
                      const mine = side === 0 ? d.a : d.b
                      const other = side === 0 ? d.b : d.a
                      const max = Math.max(d.a, d.b, 1)
                      return (
                        <div key={d.key} style={{ fontSize: 12 }}>
                          <div className="row" style={{ justifyContent: 'space-between' }}>
                            <span>{d.label}</span>
                            <span className="mono">
                              {mine} {mine > other ? '◀' : mine < other ? '▶' : ''}
                            </span>
                          </div>
                          <div className="bar">
                            <i style={{ width: `${(mine / max) * 100}%` }} />
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
                {view.audit.findings.filter((x) => x.turn_index != null).length > 0 && (
                  <div style={{ marginTop: 8 }}>
                    <h3>需要看一眼的地方</h3>
                    {view.audit.findings
                      .filter((x) => x.turn_index != null)
                      .map((x, i) => (
                        <button
                          key={i}
                          type="button"
                          className="btn btn-sm"
                          style={{ marginRight: 4, marginBottom: 4 }}
                          title={x.rule}
                          onClick={() =>
                            setJump((prev) => ({
                              ...prev,
                              [record.material_id]: {
                                turnIndex: x.turn_index!,
                                nonce: Date.now(),
                              },
                            }))
                          }
                        >
                          {SEVERITY_LABEL[x.severity]} · turn {x.turn_index}
                        </button>
                      ))}
                  </div>
                )}
              </div>

              {/* Narrow annotation mode: numbered badges inline, full column in
                  the single-material view (design.md §11, ruled 2026-07-28). */}
              <MaterialReader
                view={view}
                height={520}
                narrow
                jumpToTurn={jump[record.material_id] ?? null}
              />

              <button
                type="button"
                className="btn btn-primary"
                disabled={selectedId !== null}
                onClick={() => setDialogFor(record)}
              >
                {discarded ? '已弃用' : `选定 ${f.label} → 合成语音`}
              </button>
            </div>
          )
        })}
      </div>

      <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>
        对比阶段无音频：语音在选定之后才合成，避免为被弃用的材料产生 Polly 费用。
      </div>

      {dialogFor && (
        <SelectDialog
          record={dialogFor}
          onCancel={() => setDialogFor(null)}
          onConfirm={() => void doSelect(dialogFor)}
        />
      )}
    </div>
  )
}
