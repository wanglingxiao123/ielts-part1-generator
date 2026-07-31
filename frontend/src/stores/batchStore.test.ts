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

function done(seq: number, status: 'done' | 'partial'): SseEvent {
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
