/**
 * 审核队列：用户在结果页勾选并「提交审核」的材料。
 *
 * 这是原「隔离区」那个页签的去处。隔离区已经不存在了——每套材料都会返回、都可选，
 * 没有一个「用户不该看的抽屉」。第三个页签因此改成审核队列：**用户自己送进来的**
 * 材料，而不是系统扣下的材料。
 *
 * 为什么在前端存：后端目前没有记录「已提交审核」这个状态的地方。`action: select`
 * 只做「选定这一套 → 合成语音 → 丢弃同组其余」，没有队列。所以这里持久化到
 * localStorage，并且页面会说清楚这份队列是本机的——把它渲染成一个后端已经知道的
 * 事实，会让用户以为换台电脑也能看到。
 */
import { create } from 'zustand'

export interface QueuedMaterial {
  materialId: string
  batchId: string
  scenarioKey: string
  /** 第 N 套里的 N-1，用来还原「第 1 套」这样的标签。 */
  index: number
  submittedAt: number
  /** 提交时卡片上那一行简述，队列页不必重新 join 全部构件就能显示。 */
  summary: string
  /**
   * 这里原来有 `shortcomingCount`，用来在队列页写「提交时已知 N 处缺陷」。评价文字随
   * 客户的要求整体搬去阅读页之后，卡片不再算这个数，也没有第二个来源，所以字段一并删掉，
   * 而不是留一个永远为 0 的死字段。质量建议在 /materials/:id 上，队列页每一行都有入口。
   *
   * 老的 localStorage 记录里可能还带着这个键：`load()` 只按 materialId 过滤、不做
   * 白名单，所以多出来的键会被原样读进来又原样写回，不影响任何显示。
   */
}

/** 版本前缀，和 batchStore 一致：结构变了就丢弃而不是崩。 */
const LS_KEY = 'bcielts.v1.reviewQueue'

function load(): QueuedMaterial[] {
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as QueuedMaterial[]
    if (!Array.isArray(parsed)) return []
    return parsed.filter((x) => typeof x?.materialId === 'string')
  } catch {
    return []
  }
}

function save(items: QueuedMaterial[]): void {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(items))
  } catch {
    /* quota / private mode — the queue is a convenience, not a record */
  }
}

interface ReviewQueueState {
  items: QueuedMaterial[]
  /** 重复提交同一套不会产生第二条：以 materialId 去重。 */
  submit: (items: QueuedMaterial[]) => void
  /** 整批撤回。撤回是批次级的动作——见 ReviewQueuePage 里为什么不再逐条撤。 */
  removeBatch: (batchId: string) => void
  clear: () => void
}

export const useReviewQueue = create<ReviewQueueState>((set) => ({
  items: load(),

  submit: (incoming) =>
    set((s) => {
      const byId = new Map(s.items.map((i) => [i.materialId, i]))
      for (const item of incoming) byId.set(item.materialId, item)
      const items = [...byId.values()].sort((a, b) => b.submittedAt - a.submittedAt)
      save(items)
      return { items }
    }),

  removeBatch: (batchId) =>
    set((s) => {
      const items = s.items.filter((i) => i.batchId !== batchId)
      save(items)
      return { items }
    }),

  clear: () =>
    set(() => {
      save([])
      return { items: [] }
    }),
}))
