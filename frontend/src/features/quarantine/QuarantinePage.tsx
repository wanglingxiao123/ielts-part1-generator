import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '@/api/endpoints'
import type { MaterialRecord } from '@/contracts/api'

/**
 * Quarantine (prd R7). FAIL and NOT_ASSESSABLE both land here (design.md §11),
 * with the reason distinguished. Kept out of the compare view but always
 * inspectable.
 */
export function QuarantinePage() {
  const [records, setRecords] = useState<MaterialRecord[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    void api
      .listMaterials({ status: 'quarantine' })
      .then((res) => setRecords(res.materials))
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
  }, [])

  const byScenario = useMemo(() => {
    const map = new Map<string, MaterialRecord[]>()
    for (const r of records) {
      const list = map.get(r.scenario_key) ?? []
      list.push(r)
      map.set(r.scenario_key, list)
    }
    return map
  }, [records])

  return (
    <div className="page">
      <h2>隔离区</h2>
      <div className="muted" style={{ marginBottom: 10, fontSize: 12 }}>
        FAIL 与 NOT_ASSESSABLE 均进入隔离区，不出现在对比视图；原因标注区分两者。
        状态流转（approved / rejected / production）本期仅预留操作位。
      </div>

      {error && (
        <div className="banner banner-bad">
          <strong>加载失败</strong>
          <div>{error}</div>
        </div>
      )}

      {records.length === 0 && !error && (
        <div className="panel panel-pad muted">隔离区为空。</div>
      )}

      {[...byScenario.entries()].map(([scenarioKey, list]) => (
        <div key={scenarioKey} className="panel" style={{ marginBottom: 12 }}>
          <div className="panel-pad" style={{ borderBottom: '1px solid var(--line-2)' }}>
            <div className="row">
              <strong>{scenarioKey}</strong>
              <span className="flag flag-bad">{list.length} 套被隔离</span>
              {list.length >= 2 && (
                <>
                  <span className="flag flag-warn">本场景无可选材料</span>
                  <Link className="btn btn-sm" to="/">
                    重新生成本场景
                  </Link>
                </>
              )}
            </div>
          </div>
          {list.map((r) => (
            <div key={r.material_id} className="q-row">
              <span className="flag flag-bad">{r.verdict}</span>
              <span className="mono muted" style={{ fontSize: 11 }}>
                {r.material_id}
              </span>
              <span style={{ flex: 1, fontSize: 12 }}>
                <strong>{r.quarantine_reason?.code ?? '—'}</strong>{' '}
                {r.quarantine_reason?.message ?? '未提供隔离原因'}
                <div className="muted">
                  总分 {r.audit.score.total} ·{' '}
                  {r.audit.findings.filter((f) => f.severity === 'critical').length} critical ·{' '}
                  {r.audit.findings.filter((f) => f.severity === 'major').length} major
                </div>
              </span>
              <Link className="btn btn-sm" to={`/materials/${r.material_id}`}>
                查看
              </Link>
              <button type="button" className="btn btn-sm" disabled title="本期不实现状态流转">
                退回
              </button>
              <button type="button" className="btn btn-sm" disabled title="本期不实现状态流转">
                通过
              </button>
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}
