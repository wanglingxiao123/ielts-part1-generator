/**
 * 「生成结果」页签点进来之后。
 *
 * 这一页存在的理由是一个客户实际遇到的问题：页签原来在 `store.batchId` 为空时是灰的，而那是**本页
 * 会话**的批次、刷新即空。于是 S3 里躺着十几个历史批次，用户打开页面却看不到「生成结果」，得先勾一个
 * 场景、提交一次生成才能看见以前的东西。
 *
 * 三个分支都要覆盖，因为它们错的方式各不相同：
 *   * 有活批次 → 必须**一个请求都不发**（那一批可能还没写完 S3 索引）
 *   * 无活批次但有历史 → 必须跳到**最近**那一批
 *   * 什么都没有 → 必须是一句「去哪儿开始」，不能是空白，也不能把「读不到」说成「没有」
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useParams } from 'react-router-dom'
import { useBatchStore } from '@/stores/batchStore'
import { LatestBatchRoute } from './LatestBatchRoute'

/** 每次 `batchHistory()` 的调用次数——「有活批次时不发请求」这条只能靠它断言。 */
let calls = 0
let answer: { batches: Array<{ batch_id: string; created_at: number }> } | Error = { batches: [] }

vi.mock('@/api/endpoints', () => ({
  api: {
    batchHistory: () => {
      calls += 1
      return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer)
    },
  },
}))

/** 落地页 + 目标页，这样「跳转到哪一批」可以从渲染结果读出来，而不是去 mock Navigate。 */
function renderRoute() {
  return render(
    <MemoryRouter initialEntries={['/batches']}>
      <Routes>
        <Route path="/batches" element={<LatestBatchRoute />} />
        <Route path="/batches/:batchId" element={<Landed />} />
        <Route path="/" element={<div>场景选择页</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

/** 目标页的替身。渲染出 batchId，这样「跳去了哪一批」是可断言的。 */
function Landed() {
  const { batchId } = useParams<{ batchId: string }>()
  return <div data-testid="landed">{batchId}</div>
}

beforeEach(() => {
  calls = 0
  answer = { batches: [] }
  useBatchStore.getState().reset()
})

afterEach(() => {
  useBatchStore.getState().reset()
})

describe('有活批次时', () => {
  it('直接用它，一个请求都不发', async () => {
    // 活批次可能还没写完 S3 索引，而它恰恰是用户此刻最想看的那一批。
    useBatchStore.setState({ batchId: 'web-live-1' })
    renderRoute()

    await waitFor(() => expect(screen.getByTestId('landed')).toHaveTextContent('web-live-1'))
    expect(calls, '有活批次却仍去查了历史').toBe(0)
  })
})

describe('没有活批次时', () => {
  it('跳到最近那一批', async () => {
    // 后端按 created_at 倒序返回（`web/batch_store.py` 的 `_newest_first`），所以第一项就是最近的。
    // 这里刻意把 created_at 排成乱序，来确认前端用的是**位置**而不是自己重排——重排就是第二份
    // 排序实现，会和后端漂移。
    answer = {
      batches: [
        { batch_id: 'web-newest', created_at: 100 },
        { batch_id: 'web-older', created_at: 900 },
      ],
    }
    renderRoute()

    // 用的是列表的第一项，不是自己按 created_at 重排的结果。
    await waitFor(() => expect(screen.getByTestId('landed')).toHaveTextContent('web-newest'))
    expect(calls).toBe(1)
  })

  it('一个批次都没有时给一句「去哪儿开始」', async () => {
    answer = { batches: [] }
    renderRoute()

    await waitFor(() => expect(screen.getByText('还没有生成过材料')).toBeInTheDocument())
    // 空状态必须给出下一步，否则用户在这一页无路可走。
    expect(screen.getByRole('link', { name: /去场景选择/ })).toBeInTheDocument()
    // 不能渲染成一个空的批次页。
    expect(screen.queryByTestId('landed')).not.toBeInTheDocument()
  })

  it('读不到历史时说的是读取失败，不是「没有生成过」', async () => {
    // 这两件事说成同一句话，会让一次 S3 故障看起来像「你还没生成过材料」——而那句话会让用户
    // 以为自己的东西丢了。
    answer = new Error('S3 unreachable')
    renderRoute()

    await waitFor(() =>
      expect(screen.getByText(/历史记录暂时读取不到|读取不到/)).toBeInTheDocument(),
    )
    expect(screen.queryByText('还没有生成过材料')).not.toBeInTheDocument()
  })

  it('加载期间说加载中', () => {
    answer = { batches: [{ batch_id: 'web-1', created_at: 1 }] }
    renderRoute()
    // 第一帧：请求还没回来。这一屏不能是空白，也不能是「还没有生成过材料」。
    expect(screen.getByText(/正在读取历史批次/)).toBeInTheDocument()
    expect(screen.queryByText('还没有生成过材料')).not.toBeInTheDocument()
  })
})
