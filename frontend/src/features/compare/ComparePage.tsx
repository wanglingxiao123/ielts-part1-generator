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
  const storeBatchId = useBatchStore((s) => s.batchId)
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
  /** 打开确认框时这一套是否已经有音频——决定确认框说不说「产生费用」。null = 还在查。 */
  const [dialogHasAudio, setDialogHasAudio] = useState<boolean | null>(null)
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

  /**
   * 打开确认框，并先问一下这一套有没有音频。
   *
   * 只影响文案：已试听过的材料在选定时一次 Polly 都不调，把它说成「产生费用」是劝人别做一件其实
   * 免费的事。查询失败就按「还没有音频」处理——多说一句费用比漏说一句安全。
   */
  const openSelectDialog = useCallback(async (record: MaterialRecord) => {
    setDialogFor(record)
    setDialogHasAudio(null)
    try {
      const status = await api.getAudio(record.material_id)
      setDialogHasAudio(status.status === 'ready')
    } catch {
      setDialogHasAudio(false)
    }
  }, [])

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

  // 优先用材料自己带的 batch_id：从历史批次点进来时 store 装的是当前活批次，用它会把用户送回
  // 另一批。一套材料都没有时（下面那个分支）只剩 store 可用，而那一屏最需要一个出路。
  const backBatchId = records.find((r) => r.batch_id)?.batch_id ?? storeBatchId

  if (records.length === 0) {
    return (
      <div className="page">
        <div className="panel panel-pad">
          <div>本场景暂无材料。</div>
          {backBatchId && (
            <Link className="btn btn-sm" to={`/batches/${backBatchId}`} style={{ marginTop: 10 }}>
              ← 返回批次
            </Link>
          )}
        </div>
      </div>
    )
  }

  const showPair = candidates.length >= 2 && comparison !== null

  return (
    <div className="page-wide">
      <div className="row" style={{ marginBottom: 8 }}>
        {/* 返回批次。这一页过去是**单向**的：用户从结果页点进来，页面上没有任何回去的入口，
            只剩浏览器的后退键——而这一页是全宽布局、跟结果页长得不像，读起来像是离开了那个批次。
            batchId 取自材料自己（`MaterialRecord.batch_id`），不靠 store：从历史批次点进来时
            store 里装的是当前活批次，用它会把用户送回**另一批**。 */}
        {backBatchId && (
          <Link className="btn btn-sm" to={`/batches/${backBatchId}`}>
            ← 返回批次
          </Link>
        )}
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
                /* 点一个候选：换掉**右**边那一栏，左边留着。
                 *
                 * 原来写的是 `([a]) => (a === i ? [a, i] : [a, i])`——两个分支返回同一个值，
                 * 所以那个三元判断根本没在判断任何东西。它的实际行为是「永远换右栏」，于是
                 * 点左栏自己那个按钮会把右栏也变成它，两栏同一套材料自己跟自己比。
                 * 现在点左栏那个按钮是原地不动（它已经在对比里了），点右栏那个换右栏。 */
                onClick={() =>
                  setPair(([a, b]) => (a === i ? [a, b] : b === i ? [a, b] : [a, i]))
                }
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
          {/* 「语音合成已触发」对已试听过的材料不成立——那一套的音频早就在了，选定沿用它。 */}
          <strong>{dialogHasAudio ? '已选定，沿用已生成的语音' : '已选定，语音合成已触发'}</strong>
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
              /* 这一栏当前是哪套材料。给测试用：「两栏永远不是同一套」这条不变式没法靠文字断言——
                 每栏内部到处都出现「候选 A」这几个字（结论句、选定按钮、切换按钮）。 */
              data-material={record.material_id}
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
                onClick={() => void openSelectDialog(record)}
              >
                {discarded ? '已弃用' : `选定 ${f.label}`}
              </button>
            </div>
          )
        })}
      </div>

      {/* 对比页本身仍然不放播放器（两条音轨并排听不出什么），但「语音在选定之后才合成」已经不对了
          ——阅读页现在可以先生成音频再决定，而那份音频在选定时会被沿用。 */}
      <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>
        想先听一遍再决定的，在单套材料的阅读页点「生成音频」；那份音频会跟着这一套，选定时不重新合成。
      </div>

      {dialogFor && (
        <SelectDialog
          record={dialogFor}
          alreadySynthesised={dialogHasAudio}
          onCancel={() => setDialogFor(null)}
          onConfirm={() => void doSelect(dialogFor)}
        />
      )}
    </div>
  )
}
