/**
 * 结果页的**版位**（slot）：每个场景要几张卡、每张卡此刻是骨架还是真卡。
 *
 * 客户的要求是「提交后立刻看到结果页的结构」，所以卡位必须在**任何一个 material
 * 事件到达之前**就存在。版位从哪来：
 *
 * 1. **用户提交时选的每场景数量**（`requested`）。这是权威来源，`POST /api/batches`
 *    的响应里 `items` 就是按它铺开的，前端在 `initBatch` 时原样记下。
 * 2. 刷新回来、深链进来时 `requested` 是空的，于是退回**按 `items` 归组**——快照
 *    同样按场景 + 第 N 套列出每个已规划的版位（agentcore 的 `snapshot()`、
 *    mock 的 `snapshotOf()` 都是）。
 *
 * 一个版位只会被填一次：材料按 `(scenario_key, index)` 对号入座，所以同一套材料
 * 到达时是**替换**它的骨架，不会在旁边多长出一张卡。
 *
 * ## 为什么「自动重试中」不是一个状态
 *
 * 后端对评价环节判不了（NOT_ASSESSABLE）的版位会**静默重跑**：只发 `refilling`
 * 这类 stage 事件，不发 `material_failed`（见 backend/orchestration/batch.py
 * 的 `_run_slot`）。那种情况下这个版位的 item 仍然是 running，于是这里仍然给出
 * 骨架——用户看到的就是「还在生成」，正如客户要的「用户不该察觉到重试」。
 *
 * 真正的 `material_failed` 是**终态**：后端不会再补这一套（`_run_slot` 只补
 * 「成功但判不了」的，模型不可达、校验器崩了这类失败是留给运维看的）。所以错误
 * 版位不写「自动重试中」——那是一句不会发生的承诺。它说「生成异常」，补生成的入口
 * 在页面顶部的整批提示里，一个动作补齐所有缺的套。
 */
import type { BatchItemSnapshot, MaterialRecord } from '@/contracts/api'

/** 用户提交时选的：场景 + 该场景要几套。顺序即卡片分组顺序。 */
export interface RequestedScenario {
  scenarioKey: string
  count: number
}

export type SlotState = 'skeleton' | 'material' | 'error'

export interface ResultSlot {
  /** 稳定 key：同一个版位在骨架期和真卡期用同一个 key，React 才会做替换而非重建。 */
  key: string
  scenarioKey: string
  /** 第 N 套里的 N-1。 */
  index: number
  state: SlotState
  /** state === 'material' 时必定有值。 */
  materialId: string | null
}

export interface ResultGroup {
  scenarioKey: string
  slots: ResultSlot[]
  /** 已到达真卡数。 */
  arrived: number
}

interface Input {
  /** 用户选的每场景数量；空数组时退回按 items 归组。 */
  requested: readonly RequestedScenario[]
  /** 已规划 / 进行中 / 已失败的版位。 */
  items: readonly BatchItemSnapshot[]
  /** 已到达的材料，key 为 material_id。 */
  materials: Readonly<Record<string, MaterialRecord>>
  /**
   * 整批已进入终态（`done` / `partial`）。
   *
   * 到了终态还空着的版位就不会再有材料了——时间预算把它跳过了，或者它自己失败了
   * 而失败事件没到。这时必须停掉「生成中…」的 shimmer：一个永远转下去的骨架比
   * 说清「这套没出来」糟得多。
   */
  batchFinished: boolean
}

/**
 * 从 items 反推每场景要几套：取该场景出现过的最大 `index` + 1，而不是 item 条数。
 * 条数会因为同一场景的某个版位还没被快照列出而偏小，最大 index 不会。
 */
function shapeFromItems(items: readonly BatchItemSnapshot[]): RequestedScenario[] {
  const order: string[] = []
  const maxIndex = new Map<string, number>()
  for (const item of items) {
    if (!item.scenario_key) continue // 只有 progress 事件建起来的占位，场景未知
    if (!maxIndex.has(item.scenario_key)) order.push(item.scenario_key)
    maxIndex.set(item.scenario_key, Math.max(maxIndex.get(item.scenario_key) ?? 0, item.index))
  }
  return order.map((scenarioKey) => ({
    scenarioKey,
    count: (maxIndex.get(scenarioKey) ?? 0) + 1,
  }))
}

/**
 * 分组用的场景键。
 *
 * 规划时自定义场景叫 `custom`，材料回来时后端已经把它换成 `custom-<sha1(文本)[:8]>`（为了同一段
 * 文本落在同一个 S3 前缀）。两个键当成两个场景，界面上就出现了两行——「✍️自定义场景 0/3」下面
 * 一个空的，再来一个「📝custom-6cf6e9b3 未分类 3/3」装着真材料。归一化到 `custom` 即可合并。
 */
export function groupKeyOf(scenarioKey: string): string {
  return scenarioKey.startsWith('custom-') ? 'custom' : scenarioKey
}

export function buildResultGroups(input: Input): ResultGroup[] {
  const shape = input.requested.length > 0 ? input.requested : shapeFromItems(input.items)

  // (场景, 第 N 套) → 已到达的材料 / 已终态失败的版位。
  const arrivedAt = new Map<string, MaterialRecord>()
  for (const record of Object.values(input.materials)) {
    arrivedAt.set(`${groupKeyOf(record.scenario_key)}#${record.index}`, record)
  }
  const failedAt = new Set<string>()
  for (const item of input.items) {
    if (item.status === 'failed' && item.scenario_key) {
      failedAt.add(`${item.scenario_key}#${item.index}`)
    }
  }

  const groups: ResultGroup[] = []
  const covered = new Set<string>()

  for (const { scenarioKey, count } of shape) {
    const slots: ResultSlot[] = []
    let arrived = 0
    for (let index = 0; index < Math.max(1, count); index += 1) {
      // 归一化两侧：规划侧给的是 `custom`，到达侧可能是 `custom-<hash>`。
      const at = `${groupKeyOf(scenarioKey)}#${index}`
      covered.add(at)
      const record = arrivedAt.get(at)
      if (record) {
        arrived += 1
        slots.push({
          key: at,
          scenarioKey,
          index,
          state: 'material',
          materialId: record.material_id,
        })
      } else {
        slots.push({
          key: at,
          scenarioKey,
          index,
          state: failedAt.has(at) || input.batchFinished ? 'error' : 'skeleton',
          materialId: null,
        })
      }
    }
    groups.push({ scenarioKey, slots, arrived })
  }

  // 后端交回了计划外的材料（场景对不上、或第 N 套超出计划）时也要显示出来：
  // 丢掉一套生成好的材料比多画一张卡糟糕得多。
  for (const record of Object.values(input.materials)) {
    // 必须和上面 `covered.add` 用同一个归一化，否则自定义场景的每一套都会被判成「计划外」
    // 再画一次：规划侧记的是 `custom#0`，这里若按 `custom-<hash>#0` 去查就永远查不到，
    // 6 套渲染成 12 套，计数也变成 24/18。
    const at = `${groupKeyOf(record.scenario_key)}#${record.index}`
    if (covered.has(at)) continue
    covered.add(at)
    const slot: ResultSlot = {
      key: at,
      scenarioKey: record.scenario_key,
      index: record.index,
      state: 'material',
      materialId: record.material_id,
    }
    const existing = groups.find((g) => groupKeyOf(g.scenarioKey) === groupKeyOf(record.scenario_key))
    if (existing) {
      existing.slots.push(slot)
      existing.slots.sort((a, b) => a.index - b.index)
      existing.arrived += 1
    } else {
      groups.push({ scenarioKey: record.scenario_key, slots: [slot], arrived: 1 })
    }
  }

  return groups
}

/** 「每场景至少选 1 套」这条规则要的输入：场景 → 已到达材料 id。 */
export function arrivedByScenario(groups: readonly ResultGroup[]): Map<string, string[]> {
  const map = new Map<string, string[]>()
  for (const group of groups) {
    const ids = group.slots
      .filter((s) => s.state === 'material' && s.materialId !== null)
      .map((s) => s.materialId!)
    map.set(group.scenarioKey, ids)
  }
  return map
}
