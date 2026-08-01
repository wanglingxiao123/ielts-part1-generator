/**
 * 历史批次面板的纯逻辑：按日期分组、按场景名搜索、按状态筛选。
 *
 * 全部是纯函数，一个 React 依赖都没有。理由不是洁癖：客户点名要的三件事——「输入『酒店』只显示
 * 含酒店场景的批次」、「今天 / 昨天 / 具体日期」、「状态 chip 点击切换」——每一件都有一个说不清
 * 的边界（跨日、多场景、自定义场景），而这些边界在组件里只能靠点击验证，在这里可以单测。
 *
 * ## 搜索按中文场景名，不按 key
 *
 * 用户输入的是「酒店」。后端存的是 `booking-hotel`。所以搜索必须走 `scenarioMeta()` 拿到的
 * `titleZh`，而不是 key ——按 key 匹配的话「酒店」一个结果都搜不到，而这正是客户举的例子。
 * batch ID 也一起搜，因为面板上显示了它，显示出来又搜不到会被当成搜索坏了。
 *
 * ## 状态的中文名在这里，不在后端
 *
 * 后端返回 `pending_selection | submitted` 两个机器 token（web/batch_history.py），文案在这一层。
 * 这样客户改一个词不用动后端，也不用让后端持有中文文案。
 */
import type {
  BatchHistoryEntry,
  BatchHistoryStatus,
} from '@/contracts/api'
import { scenarioMeta } from '@/config/scenarioMeta'
import { CUSTOM_SCENARIO_KEY } from '@/config/scenarioTypes'

/* ── 状态 ────────────────────────────────────────────────────────────────── */

/** 筛选 chip 的取值。`all` 不是一个后端状态，只是「不筛」。 */
export type StatusFilter = 'all' | BatchHistoryStatus

/** chip 的顺序与文案，客户给的：全部 / 待选稿 / 已提交。 */
export const STATUS_FILTERS: ReadonlyArray<{ value: StatusFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'pending_selection', label: '待选稿' },
  { value: 'submitted', label: '已提交' },
]

export const STATUS_LABEL: Record<BatchHistoryStatus, string> = {
  pending_selection: '待选稿',
  submitted: '已提交',
}

/**
 * 徽章配色，客户指定：待选稿=绿色 / 已提交=蓝色。
 *
 * 返回 CSS 类名后缀而不是颜色值，颜色留在 styles.css 里跟其它徽章一起——在这里写 `#16a34a`
 * 会让主题色出现第二个来源。
 */
export const STATUS_TONE: Record<BatchHistoryStatus, 'good' | 'info'> = {
  pending_selection: 'good',
  submitted: 'info',
}

/**
 * 把响应里的状态收敛到当前的两个取值。
 *
 * `archived` 是删掉的第三个状态。后端不再产出它，但浏览器可能还持有改动之前的缓存响应，而一个
 * 落到映射表外的取值会让徽章渲染成 `undefined`。按客户的要求归到「已提交」——那些批次确实已经
 * 不能再选稿了，只读的结论一致，只是理由不同（`read_only` 仍由后端单独给出）。
 */
export function normalizeStatus(status: string): BatchHistoryStatus {
  return status === 'pending_selection' ? 'pending_selection' : 'submitted'
}

/* ── 搜索 ────────────────────────────────────────────────────────────────── */

/**
 * 一个批次可被搜索的文本：中文场景名 + 大类名 + batch ID。
 *
 * 大类名（如「预订类」）一起进来，因为客户的分类词和场景词在他嘴里是混着用的，输「预订」找不到
 * 任何酒店预订的批次会显得很怪。
 */
function haystack(batch: BatchHistoryEntry): string {
  const parts = [batch.batch_id]
  for (const entry of batch.scenarios) {
    if (entry.scenario_key === CUSTOM_SCENARIO_KEY || entry.scenario_key === 'custom') {
      parts.push('自定义场景')
      continue
    }
    const meta = scenarioMeta(entry.scenario_key)
    parts.push(meta.titleZh, meta.categoryZh, entry.scenario_key)
  }
  return parts.join(' ').toLowerCase()
}

/**
 * 搜索 + 状态筛选。两者是「与」的关系。
 *
 * 空查询不做任何过滤（不是「匹配零条」）：搜索框清空后必须回到完整列表。
 */
export function filterBatches(
  batches: readonly BatchHistoryEntry[],
  { query, status }: { query: string; status: StatusFilter },
): BatchHistoryEntry[] {
  const needle = query.trim().toLowerCase()
  return batches.filter((batch) => {
    // normalizeStatus 而不是直接比：筛「已提交」时一条缓存下来的 `archived` 也该出现在结果里，
    // 因为徽章上写的就是「已提交」。不归一的话它两个 chip 都进不去，只在「全部」里露一面。
    if (status !== 'all' && normalizeStatus(batch.status) !== status) return false
    if (needle.length === 0) return true
    return haystack(batch).includes(needle)
  })
}

/** 每个 chip 旁边的计数，按未经状态筛选、但已经过搜索的集合算。 */
export function countByStatus(
  batches: readonly BatchHistoryEntry[],
): Record<StatusFilter, number> {
  const out: Record<StatusFilter, number> = {
    all: batches.length,
    pending_selection: 0,
    submitted: 0,
  }
  // 经过 normalizeStatus：一条缓存下来的 `archived` 否则会写进一个映射表里没有的键，chip 计数
  // 于是少一个，而「全部」仍然把它算进去——两个数字对不上，看起来像筛选坏了。
  for (const batch of batches) out[normalizeStatus(batch.status)] += 1
  return out
}

/* ── 日期分组 ────────────────────────────────────────────────────────────── */

export interface DateGroup {
  /** 稳定 key（`YYYY-MM-DD`），不是显示用的。 */
  key: string
  /** 「今天」/「昨天」/「7月28日」。 */
  label: string
  batches: BatchHistoryEntry[]
}

/**
 * 「今天 / 昨天 / 具体日期」。
 *
 * 按**本地日历日**分，不按「距今 24 小时」——凌晨一点生成的批次，早上九点看必须是「今天」，
 * 而按小时差算会说「昨天」。这个差别是这个函数存在的全部理由，也是它唯一值得单测的地方。
 *
 * 输入假定已按时间倒序（后端 `_newest_first` 保证），所以这里只分组、不重排：重排会把
 * 「数据按时间倒序」这条要求变成两处各自实现一遍。
 */
export function groupByDate(
  batches: readonly BatchHistoryEntry[],
  now: Date = new Date(),
): DateGroup[] {
  const today = dayKey(now)
  const yesterday = dayKey(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1))
  const groups: DateGroup[] = []
  for (const batch of batches) {
    // 后端存的是 unix 秒（`time.time()`），乘 1000 才是 JS 的毫秒。
    const at = new Date(batch.created_at * 1000)
    const key = dayKey(at)
    const label = key === today ? '今天' : key === yesterday ? '昨天' : monthDay(at)
    const last = groups[groups.length - 1]
    if (last && last.key === key) last.batches.push(batch)
    else groups.push({ key, label, batches: [batch] })
  }
  return groups
}

function dayKey(at: Date): string {
  return `${at.getFullYear()}-${String(at.getMonth() + 1).padStart(2, '0')}-${String(
    at.getDate(),
  ).padStart(2, '0')}`
}

function monthDay(at: Date): string {
  return `${at.getMonth() + 1}月${at.getDate()}日`
}

/** 列表里每条的时间，`HH:MM`。日期已经由分组标题说了，这里不重复。 */
export function timeOfDay(createdAt: number): string {
  const at = new Date(createdAt * 1000)
  return `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`
}

/* ── 一条记录上的场景标签 ─────────────────────────────────────────────────── */

export interface ScenarioTag {
  key: string
  icon: string
  titleZh: string
  count: number
}

/**
 * 「emoji + 中文场景名」，按后端记录的顺序。
 *
 * 自定义场景在 `scenarioMeta` 里查不到（它不在 catalogue 里），所以单独给一个词和图标，而不是
 * 让它回落成显示 key —— 面板上出现一个 `custom` 只会让人以为哪里错了。
 */
export function scenarioTags(batch: BatchHistoryEntry): ScenarioTag[] {
  return batch.scenarios.map((entry) => {
    if (entry.scenario_key === CUSTOM_SCENARIO_KEY || entry.scenario_key === 'custom') {
      return { key: entry.scenario_key, icon: '✏️', titleZh: '自定义场景', count: entry.count }
    }
    const meta = scenarioMeta(entry.scenario_key)
    return { key: entry.scenario_key, icon: meta.icon, titleZh: meta.titleZh, count: entry.count }
  })
}

/**
 * 套数标签上的数字：**已到达**的套数，不是请求的套数。
 *
 * 客户写的是「套数（如『6 套』）」。请求了 6 套、到了 4 套时说「6 套」是在说一个不存在的东西——
 * 点进去只有 4 张卡。所以正常情况显示到达数，缺套时显示「4/6 套」，把差额说出来。
 */
export function setCountLabel(batch: BatchHistoryEntry): string {
  if (batch.arrived < batch.requested_total) {
    return `${batch.arrived}/${batch.requested_total} 套`
  }
  return `${batch.arrived} 套`
}
