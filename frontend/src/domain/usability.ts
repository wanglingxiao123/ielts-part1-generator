/**
 * 「这份材料能不能写出 10 道好题」——审阅结论层。
 *
 * 上游 domain/distribution.ts 算的是实现层度量（间隔、CV、均匀度）。命题人不需要
 * 系数，只需要结论：顺序对不对、有没有挤在一起、哪一段没题可出。本模块把同一组
 * 度量翻译成命题人能直接执行的判断，术语一律沿用《Part1 选材命制规范》：
 *
 *   §4B-2 线性顺序性   → outOfOrder
 *   §4B-2 一循环一考点 → clusters
 *   §4A   前后均衡     → firstHalfCount / secondHalfCount / balanced
 *   §3    全篇覆盖     → wideGaps
 *
 * 不新增任何阈值：每一条结论都只读 DistributionMetrics 已有字段，因此分布预览图与
 * 文字结论不可能互相矛盾。
 */
import type { DistributionMetrics } from './distribution'
import { circled } from './types'

/** 出题就绪度。ready 可直接写题；needsWork 能写但会影响作答；blocked 必须先改。 */
export type Readiness = 'ready' | 'needsWork' | 'blocked'

export interface UsabilityCheck {
  key: 'order' | 'pace' | 'coverage' | 'groups' | 'anchor'
  /** 命题人的说法，不出现指标名。 */
  label: string
  level: Readiness
  /** 结论本身，带上足以据此动手改的位置信息。 */
  detail: string
}

export interface UsabilityVerdict {
  level: Readiness
  /** 一句话结论，出现在分布预览图正下方。 */
  headline: string
  checks: UsabilityCheck[]
  /** 必须先改 + 建议先改的条目数。 */
  problemCount: number
}

const worst = (a: Readiness, b: Readiness): Readiness => {
  if (a === 'blocked' || b === 'blocked') return 'blocked'
  if (a === 'needsWork' || b === 'needsWork') return 'needsWork'
  return 'ready'
}

/** 「⑥⑦⑧」——命题人看题号，不看数组下标。 */
const nums = (ns: number[]) => ns.map((n) => circled(n)).join('')

function orderCheck(m: DistributionMetrics): UsabilityCheck {
  if (m.outOfOrder.length === 0) {
    return {
      key: 'order',
      label: '题号顺序',
      level: 'ready',
      detail: '信息点按题号先后出现，没有回跳，可按 1→10 顺序出题。',
    }
  }
  return {
    key: 'order',
    label: '题号顺序',
    level: 'blocked',
    detail:
      m.outOfOrder
        .map(
          (o) =>
            `第 ${o.spokenSecond} 题的信息（turn ${o.turnSecond}）` +
            `排在第 ${o.spokenFirst} 题（turn ${o.turnFirst}）后面`,
        )
        .join('；') + '。题号回跳，考生会跟不上，出题前须调整先后。',
  }
}

function paceCheck(m: DistributionMetrics): UsabilityCheck {
  if (m.clusters.length > 0) {
    return {
      key: 'pace',
      label: '记录节奏',
      level: 'needsWork',
      detail:
        m.clusters
          .map((c) => `${nums(c.numbers)} 挤在 turn ${c.turnStart}–${c.turnEnd}`)
          .join('；') + '。连着给，考生来不及记，建议拆到各自的问答循环里。',
    }
  }
  // cvWarn 只在没有具体扎堆时才说话，避免与上一条重复或打架。阈值尚未用真题校准，
  // 所以这里只提示「值得通读一遍」，不下「不均匀」的断言。
  if (m.cvWarn) {
    return {
      key: 'pace',
      label: '记录节奏',
      level: 'needsWork',
      detail: '没有连着出现的扎堆，但点与点的疏密差别较大，建议通读一遍确认记录时间够用。',
    }
  }
  return {
    key: 'pace',
    label: '记录节奏',
    level: 'ready',
    detail: '每个信息点各占一个问答循环，考生有时间写下答案。',
  }
}

function coverageCheck(m: DistributionMetrics): UsabilityCheck {
  const last = m.points.length
  const phrases = m.wideGaps.map((g) => {
    if (g.index === 0) return `开场后连着 ${g.size} 轮才出现第一个信息点`
    if (g.index === last) return `最后一个信息点之后还有 ${g.size} 轮对话没有考点`
    const before = m.points[g.index - 1]
    const after = m.points[g.index]
    return `${before ? circled(before.number) : ''}${after ? ` 与 ${circled(after.number)}` : ''} 之间空了 ${g.size} 轮`
  })
  if (phrases.length === 0) {
    return {
      key: 'coverage',
      label: '全篇覆盖',
      level: 'ready',
      detail: '考点铺满全篇，没有大段无题可出的空白。',
    }
  }
  return {
    key: 'coverage',
    label: '全篇覆盖',
    level: 'needsWork',
    detail: phrases.join('；') + '。这些段落出不了题，可考虑补一个可考细节或压缩闲聊。',
  }
}

function groupsCheck(m: DistributionMetrics): UsabilityCheck {
  const detail = `第 1 组 ${m.firstHalfCount} 题 / 第 2 组 ${m.secondHalfCount} 题（按第 ${m.splitAfter} 题分组）`
  return m.balanced
    ? { key: 'groups', label: '前后两组题量', level: 'ready', detail: `${detail}，两组都够出题。` }
    : {
        key: 'groups',
        label: '前后两组题量',
        level: 'needsWork',
        detail: `${detail}，两组相差过大，读题时间会对不上。`,
      }
}

export function assessUsability(m: DistributionMetrics): UsabilityVerdict {
  const checks: UsabilityCheck[] = [
    orderCheck(m),
    paceCheck(m),
    coverageCheck(m),
    groupsCheck(m),
  ]

  // 只在出问题时才占一行：「10 个点都定位到了」是废话，「有 2 个定位不到」才是消息。
  if (m.unplacedNumbers.length > 0) {
    checks.push({
      key: 'anchor',
      label: '信息点定位',
      level: 'blocked',
      detail: `${nums(m.unplacedNumbers)} 找不到对应台词，这几个点无法据以出题（也未计入上面的判断）。`,
    })
  }

  const level = checks.map((c) => c.level).reduce(worst, 'ready')
  const problems = checks.filter((c) => c.level !== 'ready')
  const blocked = problems.filter((c) => c.level === 'blocked')
  const usable = m.points.length

  let headline: string
  if (level === 'ready') {
    headline = `可以直接出题：${usable} 个信息点顺序正确、分布到位。`
  } else if (level === 'blocked') {
    headline = `暂不能直接出题：${blocked.length} 处须先改（另有 ${problems.length - blocked.length} 处建议改）。`
  } else {
    headline = `能出题，但有 ${problems.length} 处会影响考生作答，建议先改。`
  }

  return { level, headline, checks, problemCount: problems.length }
}

/** flag 类名，让四处使用点的红黄绿保持一致。 */
export const READINESS_FLAG: Record<Readiness, string> = {
  ready: 'flag-good',
  needsWork: 'flag-warn',
  blocked: 'flag-bad',
}

export const READINESS_LABEL: Record<Readiness, string> = {
  ready: '可直接出题',
  needsWork: '建议先改',
  blocked: '须先改',
}
