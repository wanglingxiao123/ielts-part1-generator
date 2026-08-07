/**
 * `finishedAt`：批次进入终态的时刻，进度页用它冻结计时。
 *
 * 这条线之前一行测试都没有，这也是它坏了没人发现的原因：进度页的 `setInterval` 依赖数组是空的，
 * 定时器永不停，28/28 全部完成之后「已用」还在一秒一秒往上加。没有报错、没有告警，只是那一行数字
 * 一直在动，看起来像还有活没干完。
 */
import { beforeEach, describe, expect, it } from 'vitest'

import type { SseEvent } from '@/contracts/api'

import { useBatchStore } from './batchStore'

// 具体那一支，不是 `SseEvent` 联合：下面几处要 `{...done(1,'partial'), request_status: …}`，
// 而展开一个联合类型后 TS 只认所有分支的公共字段。
function done(seq: number, status: 'done' | 'partial'): Extract<SseEvent, { event: 'batch_done' }> {
  return {
    event: 'batch_done',
    seq,
    status,
    completed: status === 'done' ? 1 : 0,
    failed: status === 'done' ? 0 : 1,
    audit_rejected: 0,
  }
}

function initOneSlot() {
  useBatchStore.getState().initBatch({
    batchId: 'web-1',
    total: 1,
    items: [
      {
        material_id: 'm1',
        scenario_key: 'booking-hotel',
        index: 0,
        status: 'pending',
        stage: 'queued',
        attempt: 1,
      },
    ],
  })
}

describe('finishedAt', () => {
  beforeEach(() => {
    initOneSlot()
  })

  it('新批次没有完成时刻', () => {
    expect(useBatchStore.getState().finishedAt).toBeNull()
  })

  it('batch_done 记下完成时刻', () => {
    useBatchStore.getState().applyEvent(done(1, 'done'))

    const { finishedAt, status } = useBatchStore.getState()
    expect(status).toBe('done')
    expect(typeof finishedAt).toBe('number')
  })

  it('部分完成也算终态', () => {
    // `partial` 同样是终态：剩下的不会再来了，秒表没有理由继续走。
    useBatchStore.getState().applyEvent(done(1, 'partial'))

    expect(useBatchStore.getState().finishedAt).not.toBeNull()
  })

  it('完成之后的事件不会改写完成时刻', () => {
    useBatchStore.getState().applyEvent(done(1, 'done'))
    const first = useBatchStore.getState().finishedAt

    useBatchStore.getState().applyEvent({ event: 'ping', seq: 2 })

    expect(useBatchStore.getState().finishedAt).toBe(first)
  })

  it('开新批次时清空上一批的完成时刻', () => {
    // 不清就会沿用上一批的时刻，新批次一开始就显示一个来自上一批的耗时。
    useBatchStore.getState().applyEvent(done(1, 'done'))
    expect(useBatchStore.getState().finishedAt).not.toBeNull()

    initOneSlot()

    expect(useBatchStore.getState().finishedAt).toBeNull()
  })
})

/**
 * 精确 N 套请求（`generate_sets`）的交付结论。
 *
 * 这一组守的是「断点不是失败」。一次 invocation 的时钟用完时，后端把已通过的材料存进 S3、
 * 回一个 `request_status: 'incomplete'` 加上非空 `resumable_slots`，下一次接着做。它和
 * 「有卡片没做出来」是两件事，页面上要说完全不同的话——所以这几个字段单独存，不折进 `status`。
 */
describe('generate_sets 的交付结论', () => {
  beforeEach(() => {
    initOneSlot()
  })

  it('新批次上这些字段是空的，不是 0', () => {
    // 0 会被读成「一套都没交」。普通 `generate` 批次不带这些字段，必须区分得开。
    const s = useBatchStore.getState()
    expect(s.requestStatus).toBeNull()
    expect(s.resumableSlots).toEqual([])
    expect(s.requestedCount).toBeNull()
    expect(s.deliveredCount).toBeNull()
  })

  it('checkpoint：收下 incomplete 与可续跑卡位，而 status 仍按卡片说话', () => {
    useBatchStore.getState().applyEvent({
      ...done(1, 'partial'),
      request_status: 'incomplete',
      requested: 3,
      delivered: 1,
      resumable_slots: ['slot-2', 'slot-3'],
    })

    const s = useBatchStore.getState()
    expect(s.requestStatus).toBe('incomplete')
    expect(s.resumableSlots).toEqual(['slot-2', 'slot-3'])
    expect(s.requestedCount).toBe(3)
    expect(s.deliveredCount).toBe(1)
    // 两句话同时成立：卡片层面 partial，请求层面「没跑完但存了断点」。
    expect(s.status).toBe('partial')
  })

  it('交付数按后端的计划收，不由本地数卡片', () => {
    // 本地只有 1 张卡（initOneSlot），后端说要 3 套交了 3 套——以后端为准。
    useBatchStore.getState().applyEvent({
      ...done(1, 'done'),
      request_status: 'succeeded',
      requested: 3,
      delivered: 3,
    })

    const s = useBatchStore.getState()
    expect(s.requestStatus).toBe('succeeded')
    expect(s.deliveredCount).toBe(3)
    expect(s.resumableSlots).toEqual([])
  })

  it('system_failure 照原样收下，不改写成 incomplete', () => {
    // 成因（存储拒写、审核器缺失）前端看不见，所以这个判断只能由 web 层给，不能本地推。
    useBatchStore.getState().applyEvent({
      ...done(1, 'partial'),
      request_status: 'system_failure',
      requested: 2,
      delivered: 0,
      resumable_slots: [],
    })

    expect(useBatchStore.getState().requestStatus).toBe('system_failure')
  })

  it('普通 generate 批次不会凭空得到一个交付结论', () => {
    useBatchStore.getState().applyEvent(done(1, 'done'))

    const s = useBatchStore.getState()
    expect(s.requestStatus).toBeNull()
    expect(s.requestedCount).toBeNull()
  })

  it('开新批次时清空上一批的交付结论', () => {
    useBatchStore.getState().applyEvent({
      ...done(1, 'partial'),
      request_status: 'incomplete',
      resumable_slots: ['slot-2'],
    })

    initOneSlot()

    const s = useBatchStore.getState()
    expect(s.requestStatus).toBeNull()
    expect(s.resumableSlots).toEqual([])
  })
})
