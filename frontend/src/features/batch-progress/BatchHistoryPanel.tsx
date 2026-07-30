/**
 * 「生成结果」Tab 左侧的历史批次面板。
 *
 * 客户要的：固定宽度（约 260px）、可折叠成一条窄图标条、顶部标题 + 总数量、搜索框按场景名过滤、
 * 四个状态 chip、列表按日期分组、当前选中项用蓝色左竖条 + 蓝色背景标记、按时间倒序、可滚动。
 *
 * ## 这个面板为什么能存在
 *
 * 它读的是 `/api/batch-history`，也就是 web 层写进 S3 的批次记录（`web/batch_history.py`）。在这
 * 之前批次只活在 `frontend/src/api/agentcore.ts` 的一个 `Map` 里，刷新即失——所以「历史」这件事
 * 过去不是没做，是没有数据。面板里的每一行都对应一个 S3 对象。
 *
 * ## 折叠状态存在 localStorage
 *
 * 因为它是一个偏好，不是一次会话里的临时状态：把面板收起来的人下次进来不想再收一次。存 key 带
 * 版本前缀，和 batchStore / reviewQueueStore 一致。
 *
 * ## 面板不做的三件事
 *
 * 客户点名不要：独立页面/Tab、删除历史、批次间对比。所以这里没有路由、没有删除按钮、没有多选。
 * 「对比」在场景组头上已经有了（同场景两套并排），那是另一件事。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '@/api/endpoints'
import type { BatchHistoryEntry } from '@/contracts/api'
import {
  countByStatus,
  filterBatches,
  groupByDate,
  scenarioTags,
  setCountLabel,
  STATUS_FILTERS,
  STATUS_LABEL,
  STATUS_TONE,
  timeOfDay,
  type StatusFilter,
} from '@/domain/batchHistory'

/** 折叠偏好。版本前缀：结构变了就当没存过，而不是崩。 */
const COLLAPSE_KEY = 'bcielts.v1.historyPanel.collapsed'

function loadCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSE_KEY) === '1'
  } catch {
    return false
  }
}

function saveCollapsed(value: boolean) {
  try {
    localStorage.setItem(COLLAPSE_KEY, value ? '1' : '0')
  } catch {
    /* private mode —— 折叠状态是便利，不是要求 */
  }
}

export interface BatchHistoryPanelProps {
  /** 当前正在看的批次。用于蓝色左竖条 + 高亮。 */
  activeBatchId: string | undefined
  /** 点某一条。由页面决定是切路由还是换内容区。 */
  onSelect: (batchId: string) => void
  /**
   * 让页面能在批次跑完后要求刷新列表。
   *
   * 一个数字而不是回调：面板自己不知道批次什么时候结束，而页面知道（它拿着 SSE）。递增这个值就重取
   * 一次，比把 `refetch` 传出去再由页面在正确时机调用要难用错。
   */
  reloadToken?: number
}

export function BatchHistoryPanel({
  activeBatchId,
  onSelect,
  reloadToken = 0,
}: BatchHistoryPanelProps) {
  const [collapsed, setCollapsed] = useState(loadCollapsed)
  const [batches, setBatches] = useState<BatchHistoryEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<StatusFilter>('all')

  useEffect(() => {
    let cancelled = false
    void api
      .batchHistory()
      .then((body) => {
        if (!cancelled) {
          setBatches(body.batches)
          setError(null)
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [reloadToken])

  const toggle = useCallback(() => {
    setCollapsed((prev) => {
      saveCollapsed(!prev)
      return !prev
    })
  }, [])

  // 搜索先于状态筛选：chip 上的计数要反映「在当前搜索结果里各状态有几条」，否则输入「酒店」之后
  // chip 还显示全库的数量，点下去却只有一条。
  const searched = useMemo(
    () => filterBatches(batches ?? [], { query, status: 'all' }),
    [batches, query],
  )
  const counts = useMemo(() => countByStatus(searched), [searched])
  const visible = useMemo(
    () => filterBatches(searched, { query: '', status }),
    [searched, status],
  )
  const groups = useMemo(() => groupByDate(visible), [visible])

  if (collapsed) {
    return (
      <aside className="hist-rail">
        <button
          type="button"
          className="hist-rail-btn"
          onClick={toggle}
          title="展开历史批次"
          aria-label="展开历史批次面板"
          aria-expanded={false}
        >
          <span aria-hidden="true">🕓</span>
        </button>
        {/* 收起后仍然说得出有多少批：一条只有图标的竖条会让人忘了这里有东西。 */}
        {batches !== null && <span className="hist-rail-count">{batches.length}</span>}
      </aside>
    )
  }

  return (
    <aside className="hist-panel" aria-label="历史批次">
      <div className="hist-head">
        <span className="hist-title">历史批次</span>
        {batches !== null && <span className="hist-total">{batches.length} 批</span>}
        <span className="spacer" />
        <button
          type="button"
          className="hist-collapse"
          onClick={toggle}
          title="收起面板"
          aria-label="收起历史批次面板"
          aria-expanded
        >
          «
        </button>
      </div>

      <input
        className="hist-search"
        type="search"
        value={query}
        placeholder="搜索场景名，如「酒店」"
        aria-label="按场景名搜索历史批次"
        onChange={(e) => setQuery(e.target.value)}
      />

      <div className="hist-chips" role="group" aria-label="按状态筛选">
        {STATUS_FILTERS.map((chip) => (
          <button
            key={chip.value}
            type="button"
            className={`hist-chip${status === chip.value ? ' on' : ''}`}
            aria-pressed={status === chip.value}
            onClick={() => setStatus(chip.value)}
          >
            {chip.label}
            <span className="hist-chip-n">{counts[chip.value]}</span>
          </button>
        ))}
      </div>

      <div className="hist-list">
        {error && (
          <div className="hist-empty">
            <strong>历史记录读取失败</strong>
            <div className="muted">{error}</div>
          </div>
        )}
        {!error && batches === null && <div className="hist-empty muted">加载中…</div>}
        {!error && batches !== null && visible.length === 0 && (
          <div className="hist-empty muted">
            {batches.length === 0
              ? '还没有历史批次。生成一批之后会出现在这里。'
              : '没有匹配的批次。'}
          </div>
        )}
        {groups.map((group) => (
          <div className="hist-group" key={group.key}>
            <div className="hist-group-label">{group.label}</div>
            {group.batches.map((batch) => (
              <HistoryRow
                key={batch.batch_id}
                batch={batch}
                active={batch.batch_id === activeBatchId}
                onSelect={onSelect}
              />
            ))}
          </div>
        ))}
      </div>
    </aside>
  )
}

function HistoryRow({
  batch,
  active,
  onSelect,
}: {
  batch: BatchHistoryEntry
  active: boolean
  onSelect: (batchId: string) => void
}) {
  const tags = scenarioTags(batch)
  return (
    <button
      type="button"
      className={`hist-row${active ? ' active' : ''}`}
      data-batch={batch.batch_id}
      aria-current={active ? 'true' : undefined}
      onClick={() => onSelect(batch.batch_id)}
    >
      <div className="hist-row-top">
        <span className="hist-row-id mono">{batch.batch_id}</span>
        <span className="hist-row-time">{timeOfDay(batch.created_at)}</span>
      </div>
      <div className="hist-row-tags">
        {tags.map((tag) => (
          <span className="hist-tag" key={tag.key}>
            <span aria-hidden="true">{tag.icon}</span>
            {tag.titleZh}
            {tag.count > 1 && <span className="hist-tag-n">×{tag.count}</span>}
          </span>
        ))}
      </div>
      <div className="hist-row-foot">
        <span className="hist-sets">{setCountLabel(batch)}</span>
        <span className={`hist-badge ${STATUS_TONE[batch.status]}`}>
          {STATUS_LABEL[batch.status]}
        </span>
        {/* 「任务中途没了」是这一批**永远**不会补齐的意思，所以说出来。不说的话组头的「4/6」
            看起来像还在生成。 */}
        {batch.interrupted && (
          <span className="hist-badge warn" title="生成任务中断，缺的部分不会再补齐">
            已中断
          </span>
        )}
      </div>
    </button>
  )
}
