/**
 * 历史面板的纯逻辑：搜索、状态筛选、日期分组、场景标签、套数文案。
 *
 * 这些规则每一条都有一个只在边界上才会露出来的错法，而边界在浏览器里点不出来：
 *
 *   · 搜索走中文场景名而不是 key —— 客户举的例子就是输「酒店」，按 key 匹配零结果；
 *   · 「今天 / 昨天」按本地日历日而不是 24 小时差 —— 凌晨生成的批次早上必须还是「今天」；
 *   · chip 计数按搜索后的集合算 —— 否则输入之后 chip 说 12、点下去只有 1；
 *   · 套数显示已到达数 —— 请求 6 到 4 时写「6 套」是在说一个点进去不存在的东西。
 */
import { describe, expect, it } from 'vitest'
import type { BatchHistoryEntry } from '@/contracts/api'
import {
  countByStatus,
  filterBatches,
  groupByDate,
  normalizeStatus,
  scenarioTags,
  setCountLabel,
  STATUS_FILTERS,
  timeOfDay,
} from './batchHistory'

/** 一条历史记录。`created_at` 是 unix **秒**，和后端一致。 */
function entry(over: Partial<BatchHistoryEntry> = {}): BatchHistoryEntry {
  return {
    batch_id: 'web-1-1',
    created_at: Date.now() / 1000,
    completed_at: Date.now() / 1000,
    status: 'pending_selection',
    read_only: false,
    interrupted: false,
    state: 'complete',
    requested_total: 2,
    arrived: 2,
    scenarios: [{ scenario_key: 'booking-hotel', count: 2 }],
    materials: [],
    ...over,
  }
}

const secondsOf = (d: Date) => d.getTime() / 1000

describe('搜索', () => {
  it('按中文场景名匹配，不是按 key', () => {
    // 客户的例子：输入「酒店」只显示含酒店场景的批次。按 key 匹配的话这里会是 0 条。
    const batches = [
      entry({ batch_id: 'a', scenarios: [{ scenario_key: 'booking-hotel', count: 1 }] }),
      entry({ batch_id: 'b', scenarios: [{ scenario_key: 'employment-vacancy', count: 1 }] }),
    ]
    const hit = filterBatches(batches, { query: '酒店', status: 'all' })
    expect(hit.map((b) => b.batch_id)).toEqual(['a'])
  })

  it('多场景的批次里命中任意一个就算命中', () => {
    const batches = [
      entry({
        batch_id: 'mixed',
        scenarios: [
          { scenario_key: 'employment-vacancy', count: 1 },
          { scenario_key: 'booking-hotel', count: 1 },
        ],
      }),
    ]
    expect(filterBatches(batches, { query: '酒店', status: 'all' })).toHaveLength(1)
  })

  it('也能按大类名搜到', () => {
    // 客户嘴里分类词和场景词是混着用的；输「预订」搜不到任何酒店预订的批次会显得很怪。
    const batches = [entry({ scenarios: [{ scenario_key: 'booking-hotel', count: 1 }] })]
    expect(filterBatches(batches, { query: '预订', status: 'all' })).toHaveLength(1)
  })

  it('也能按 batch ID 搜到', () => {
    // 面板上显示了 batch ID，显示出来又搜不到会被当成搜索坏了。
    const batches = [entry({ batch_id: 'web-999-3' }), entry({ batch_id: 'web-111-1' })]
    const hit = filterBatches(batches, { query: '999', status: 'all' })
    expect(hit.map((b) => b.batch_id)).toEqual(['web-999-3'])
  })

  it('空查询不过滤，而不是匹配零条', () => {
    // 搜索框清空之后必须回到完整列表。
    const batches = [entry({ batch_id: 'a' }), entry({ batch_id: 'b' })]
    expect(filterBatches(batches, { query: '', status: 'all' })).toHaveLength(2)
    expect(filterBatches(batches, { query: '   ', status: 'all' })).toHaveLength(2)
  })

  it('搜不到的词给空列表', () => {
    const batches = [entry()]
    expect(filterBatches(batches, { query: '不存在的场景', status: 'all' })).toEqual([])
  })

  it('自定义场景按「自定义」搜得到，而不是按 custom', () => {
    const batches = [entry({ scenarios: [{ scenario_key: 'custom', count: 1 }] })]
    expect(filterBatches(batches, { query: '自定义', status: 'all' })).toHaveLength(1)
  })
})

describe('状态筛选', () => {
  const batches = [
    entry({ batch_id: 'p', status: 'pending_selection' }),
    entry({ batch_id: 's', status: 'submitted' }),
  ]

  it('三个 chip 就是客户给的三个，顺序也一样', () => {
    expect(STATUS_FILTERS.map((c) => c.label)).toEqual(['全部', '待选稿', '已提交'])
  })

  it('每个状态各筛出自己那条', () => {
    for (const [status, id] of [
      ['pending_selection', 'p'],
      ['submitted', 's'],
    ] as const) {
      const hit = filterBatches(batches, { query: '', status })
      expect(hit.map((b) => b.batch_id)).toEqual([id])
    }
  })

  it('全部不筛', () => {
    expect(filterBatches(batches, { query: '', status: 'all' })).toHaveLength(2)
  })

  it('缓存下来的 archived 按已提交处理，两处都算得上', () => {
    // 「已归档」删掉了，但浏览器可能还持有改动之前的响应。不归一的话它两个 chip 都进不去、
    // 只在「全部」里露一面，chip 计数和「全部」于是对不上，看起来像筛选坏了。
    const stale = [entry({ batch_id: 'old', status: 'archived' as never })]
    expect(normalizeStatus('archived')).toBe('submitted')
    expect(filterBatches(stale, { query: '', status: 'submitted' })).toHaveLength(1)
    expect(countByStatus(stale)).toEqual({ all: 1, pending_selection: 0, submitted: 1 })
  })

  it('搜索与状态是「与」的关系', () => {
    const mixed = [
      entry({
        batch_id: 'hotel-submitted',
        status: 'submitted',
        scenarios: [{ scenario_key: 'booking-hotel', count: 1 }],
      }),
      entry({
        batch_id: 'hotel-pending',
        status: 'pending_selection',
        scenarios: [{ scenario_key: 'booking-hotel', count: 1 }],
      }),
      entry({
        batch_id: 'job-submitted',
        status: 'submitted',
        scenarios: [{ scenario_key: 'employment-vacancy', count: 1 }],
      }),
    ]
    const hit = filterBatches(mixed, { query: '酒店', status: 'submitted' })
    expect(hit.map((b) => b.batch_id)).toEqual(['hotel-submitted'])
  })

  it('计数按传进来的集合算，所以调用方可以先搜再数', () => {
    // 面板正是这么用的：先按搜索过一遍，再数各状态——否则输入「酒店」之后 chip 还显示全库的数量。
    const searched = filterBatches(
      [
        entry({ status: 'submitted', scenarios: [{ scenario_key: 'booking-hotel', count: 1 }] }),
        entry({
          status: 'pending_selection',
          scenarios: [{ scenario_key: 'booking-hotel', count: 1 }],
        }),
        entry({
          status: 'submitted',
          scenarios: [{ scenario_key: 'employment-vacancy', count: 1 }],
        }),
      ],
      { query: '酒店', status: 'all' },
    )
    expect(countByStatus(searched)).toEqual({
      all: 2,
      pending_selection: 1,
      submitted: 1,
    })
  })
})

describe('日期分组', () => {
  const now = new Date(2026, 6, 30, 9, 0, 0) // 2026-07-30 09:00 本地时间

  it('今天 / 昨天 / 具体日期', () => {
    const groups = groupByDate(
      [
        entry({ batch_id: 't', created_at: secondsOf(new Date(2026, 6, 30, 8, 30)) }),
        entry({ batch_id: 'y', created_at: secondsOf(new Date(2026, 6, 29, 22, 0)) }),
        entry({ batch_id: 'o', created_at: secondsOf(new Date(2026, 6, 24, 10, 0)) }),
      ],
      now,
    )
    expect(groups.map((g) => g.label)).toEqual(['今天', '昨天', '7月24日'])
  })

  it('凌晨生成的批次早上看仍然是「今天」', () => {
    // 这是这个函数存在的全部理由：按「距今 24 小时」算，凌晨一点的批次到早上九点会变成「昨天」。
    const groups = groupByDate(
      [entry({ created_at: secondsOf(new Date(2026, 6, 30, 1, 0)) })],
      now,
    )
    expect(groups[0]!.label).toBe('今天')
  })

  it('刚过午夜的批次立刻变成「今天」，前一刻的是「昨天」', () => {
    const justAfter = new Date(2026, 6, 30, 0, 0, 30)
    const justBefore = new Date(2026, 6, 29, 23, 59, 30)
    const groups = groupByDate(
      [
        entry({ batch_id: 'after', created_at: secondsOf(justAfter) }),
        entry({ batch_id: 'before', created_at: secondsOf(justBefore) }),
      ],
      now,
    )
    expect(groups.map((g) => [g.label, g.batches.map((b) => b.batch_id)])).toEqual([
      ['今天', ['after']],
      ['昨天', ['before']],
    ])
  })

  it('同一天的多条合成一组', () => {
    const groups = groupByDate(
      [
        entry({ batch_id: 'a', created_at: secondsOf(new Date(2026, 6, 30, 11, 0)) }),
        entry({ batch_id: 'b', created_at: secondsOf(new Date(2026, 6, 30, 8, 0)) }),
      ],
      now,
    )
    expect(groups).toHaveLength(1)
    expect(groups[0]!.batches.map((b) => b.batch_id)).toEqual(['a', 'b'])
  })

  it('不重排，输入顺序原样保留', () => {
    // 「数据按时间倒序」由后端的 `_newest_first` 负责。这里再排一遍就是同一条要求实现两次。
    const groups = groupByDate(
      [
        entry({ batch_id: 'older', created_at: secondsOf(new Date(2026, 6, 30, 8, 0)) }),
        entry({ batch_id: 'newer', created_at: secondsOf(new Date(2026, 6, 30, 11, 0)) }),
      ],
      now,
    )
    expect(groups[0]!.batches.map((b) => b.batch_id)).toEqual(['older', 'newer'])
  })

  it('空列表给空分组', () => {
    expect(groupByDate([], now)).toEqual([])
  })

  it('时间显示成 HH:MM', () => {
    expect(timeOfDay(secondsOf(new Date(2026, 6, 30, 9, 5)))).toBe('09:05')
    expect(timeOfDay(secondsOf(new Date(2026, 6, 30, 21, 40)))).toBe('21:40')
  })
})

describe('场景标签', () => {
  it('emoji + 中文场景名 + 数量', () => {
    const tags = scenarioTags(entry({ scenarios: [{ scenario_key: 'booking-hotel', count: 2 }] }))
    expect(tags).toHaveLength(1)
    expect(tags[0]!.titleZh).toBe('酒店预订')
    expect(tags[0]!.count).toBe(2)
    expect(tags[0]!.icon).not.toBe('')
  })

  it('自定义场景给一个中文词，而不是回落成显示 key', () => {
    // 面板上出现一个 `custom` 只会让人以为哪里错了。
    const tags = scenarioTags(entry({ scenarios: [{ scenario_key: 'custom', count: 1 }] }))
    expect(tags[0]!.titleZh).toBe('自定义场景')
  })

  it('顺序照后端记录，也就是用户勾选的顺序', () => {
    const tags = scenarioTags(
      entry({
        scenarios: [
          { scenario_key: 'employment-vacancy', count: 1 },
          { scenario_key: 'booking-hotel', count: 1 },
        ],
      }),
    )
    expect(tags.map((t) => t.key)).toEqual(['employment-vacancy', 'booking-hotel'])
  })
})

describe('套数文案', () => {
  it('齐了就只说套数', () => {
    expect(setCountLabel(entry({ arrived: 6, requested_total: 6 }))).toBe('6 套')
  })

  it('缺套时把差额说出来', () => {
    // 请求 6、到了 4 却写「6 套」，是在说一个点进去不存在的东西。
    expect(setCountLabel(entry({ arrived: 4, requested_total: 6 }))).toBe('4/6 套')
  })

  it('一套都没到也照实说', () => {
    expect(setCountLabel(entry({ arrived: 0, requested_total: 3 }))).toBe('0/3 套')
  })
})
