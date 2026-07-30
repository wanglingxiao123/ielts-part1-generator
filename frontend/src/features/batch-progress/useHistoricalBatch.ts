/**
 * 加载一个**历史**批次的材料，供结果页的内容区渲染。
 *
 * ## 为什么不能只靠 batchStore
 *
 * `batchStore` 装的是「正在跑的这一批」：它由 SSE 事件填充，刷新之后是空的。历史批次没有 SSE 可
 * 接——它已经跑完了，事件流早就关了——所以它的材料只能来自 `/api/batch-history/{id}`，那是 web 层
 * 写进 S3 的记录（`web/batch_history.py`）。
 *
 * 两条路径因此并存，而且刻意不合并：**当前批次走 store，历史批次走这个 hook**。把历史塞进
 * batchStore 会让 `applyEvent` 的去重游标、`seenSeqs`、连接状态这些只对活批次有意义的东西开始对
 * 一个死批次生效，而那些字段没有一个能对历史批次给出正确答案。
 *
 * ## 只读从后端来
 *
 * `readOnly` 直接用响应里的 `read_only`，不在前端按状态重算。已提交与已归档只读的**理由**不同
 * （一个是决定已经做了，一个是决定再也做不了），前端重算一遍就是第二个会算错的地方。
 */
import { useEffect, useState } from 'react'
import { api } from '@/api/endpoints'
import type { BatchHistoryDetail, MaterialRecord } from '@/contracts/api'
import type { RequestedScenario } from '@/domain/resultSlots'

export interface HistoricalBatch {
  batchId: string
  readOnly: boolean
  interrupted: boolean
  /** 已归档 / 已提交 / 待选稿。用来在内容区顶部说清这是哪一种。 */
  status: BatchHistoryDetail['status']
  submittedMaterialIds: string[]
  /** 卡位形状，和 store 的 `requested` 同构，所以 buildResultGroups 不必知道数据从哪来。 */
  requested: RequestedScenario[]
  materials: Record<string, MaterialRecord>
  materialOrder: string[]
  requestedTotal: number
}

export interface HistoricalBatchState {
  batch: HistoricalBatch | null
  loading: boolean
  error: string | null
}

/**
 * `enabled` 为 false 时什么都不做并清空结果。
 *
 * 页面用它来表达「这一批是当前活批次，走 store，不要取历史」。做成参数而不是让调用方条件式地
 * 调用 hook，因为后者违反 hook 规则。
 */
export function useHistoricalBatch(
  batchId: string | undefined,
  enabled: boolean,
): HistoricalBatchState {
  const [state, setState] = useState<HistoricalBatchState>({
    batch: null,
    loading: false,
    error: null,
  })

  useEffect(() => {
    if (!batchId || !enabled) {
      setState({ batch: null, loading: false, error: null })
      return
    }
    let cancelled = false
    setState({ batch: null, loading: true, error: null })
    void api
      .batchHistoryDetail(batchId)
      .then((detail) => {
        if (!cancelled) setState({ batch: toHistorical(detail), loading: false, error: null })
      })
      .catch((err) => {
        if (!cancelled) {
          setState({
            batch: null,
            loading: false,
            error: err instanceof Error ? err.message : String(err),
          })
        }
      })
    return () => {
      cancelled = true
    }
  }, [batchId, enabled])

  return state
}

function toHistorical(detail: BatchHistoryDetail): HistoricalBatch {
  const materials: Record<string, MaterialRecord> = {}
  const materialOrder: string[] = []
  for (const entry of detail.materials) {
    // 一条记录可能只有摘要没有构件（写 sidecar 的那次失败了）。那种材料渲染不出卡片，所以跳过——
    // 但它在 `requested` 里仍然占一个卡位，于是会显示成一个缺口，而不是被悄悄抹掉。
    if (!entry.material || !entry.blueprint || !entry.audit) continue
    const record: MaterialRecord = {
      material_id: entry.material_id,
      batch_id: detail.batch_id,
      scenario_key: entry.scenario_key,
      index: entry.index ?? 0,
      status: 'done',
      verdict: entry.verdict as MaterialRecord['verdict'],
      audit_rejection: entry.audit_rejection ?? null,
      degraded: entry.degraded ?? false,
      validation_findings: entry.validation_findings ?? [],
      material: entry.material,
      blueprint: entry.blueprint,
      audit: entry.audit,
      cross_check: entry.cross_check!,
      created_at: new Date((detail.created_at || 0) * 1000).toISOString(),
    }
    materials[record.material_id] = record
    materialOrder.push(record.material_id)
  }
  return {
    batchId: detail.batch_id,
    readOnly: detail.read_only,
    interrupted: detail.interrupted,
    status: detail.status,
    submittedMaterialIds: detail.submitted_material_ids ?? [],
    requested: detail.scenarios.map((s) => ({ scenarioKey: s.scenario_key, count: s.count })),
    materials,
    materialOrder,
    requestedTotal: detail.requested_total,
  }
}
