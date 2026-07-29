/**
 * 阅读页：生成音频按钮，以及这一页**不再**展示的东西。
 *
 * 「不再展示」的那几条断言和正向断言一样重要。删掉的都是客户点名读不懂或用不上的东西——评价方的
 * finding 列表、校验器的英文原始提示、盲评的计数——它们的共同点是：随手加回来毫无阻力，而加回来
 * 页面就退回客户看到的那一版。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { AudioStatusResponse } from '@/contracts/api'
import { buildRecord } from '@/mocks/fixtures'
import { MaterialPage } from './MaterialPage'

const MATERIAL_ID = 'mat-reader-1'
const record = buildRecord('clustered', {
  materialId: MATERIAL_ID,
  batchId: 'b1',
  scenarioKey: 'accommodation-rental',
  index: 0,
})

/** 音频状态由测试驱动，`previewAudio` 的调用次数被计下来。 */
let audio: AudioStatusResponse = { status: 'not_requested', progress: { done: 0, total: 0 } }
const previewCalls: string[] = []

vi.mock('@/api/endpoints', () => ({
  api: {
    getMaterial: () => Promise.resolve(record),
    previewAudio: (id: string) => {
      previewCalls.push(id)
      // 真后端会把 job 建起来，下一次轮询就看得到；这里照做。
      audio = { status: 'queued', progress: { done: 0, total: 43 } }
      return Promise.resolve({ material_id: id, audio_job_id: 'job-1', repeat: false })
    },
    getAudio: () => Promise.resolve(audio),
  },
}))

function renderPage() {
  return render(
    <MemoryRouter initialEntries={[`/materials/${MATERIAL_ID}`]}>
      <Routes>
        <Route path="/materials/:materialId" element={<MaterialPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  previewCalls.length = 0
  audio = { status: 'not_requested', progress: { done: 0, total: 0 } }
})

describe('MaterialPage 音频', () => {
  /**
   * 客户的原话：「改成一个 button，让用户自行决定是否生成音频呢？点击后就可以在这个页面直接生成
   * 音频」。所以按钮必须在音频不存在时就在页面上，而不是等选定之后才出现。
   */
  it('offers 生成音频 before any audio exists, and shows progress after the click', async () => {
    renderPage()
    const button = await screen.findByRole('button', { name: /生成音频/ })
    expect(previewCalls).toEqual([])

    await userEvent.click(button)
    expect(previewCalls).toEqual([MATERIAL_ID])
    await waitFor(() => expect(screen.getByText('正在生成音频')).toBeInTheDocument())
    // 按钮已经不在了：合成中再点一次只会让人以为第一次没生效。
    expect(screen.queryByRole('button', { name: /生成音频/ })).toBeNull()
  })

  /**
   * 按钮走的是 preview，不是 select。这一条钉的是端点，因为两者在页面上长得一样，而
   * `select` 会丢弃同场景的另一套——一个只想先听听的人会因此永久失去备选。
   */
  it('generates through the preview endpoint, never through select', async () => {
    const { api } = await import('@/api/endpoints')
    expect('selectMaterial' in api).toBe(false)
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /生成音频/ }))
    expect(previewCalls).toEqual([MATERIAL_ID])
  })

  it('says the audio follows the material, so nobody expects a second generation', async () => {
    renderPage()
    expect(await screen.findByText(/选用时不会重新生成/)).toBeInTheDocument()
  })

  /** 那段「/invocations 仅支持 generate 与 list_scenarios」的话早就不成立了。 */
  it('no longer claims the synthesis endpoints are missing', async () => {
    renderPage()
    await screen.findByRole('button', { name: /生成音频/ })
    const body = document.body.textContent!
    for (const stale of ['尚未就绪', 'list_scenarios', 'audio-storage', '非真实语音', '本地占位音']) {
      expect(body).not.toContain(stale)
    }
  })
})

describe('MaterialPage 考点小结', () => {
  it('names the exam points in the spec vocabulary, with jumpable point numbers', async () => {
    renderPage()
    const panel = await waitFor(() => {
      const found = document.querySelector('.exam-points')
      if (!found) throw new Error('考点小结面板尚未渲染')
      return found
    })
    const text = panel.textContent!
    expect(text).toContain('考点小结')
    for (const label of ['拼读', '先说后改', '同义替换', '有复述确认']) {
      expect(text).toContain(label)
    }
    // clustered fixture 的盲评漏了第 5 题，说成命题人的话 + 可跳转的点号。
    expect(text).toContain('听不出来')
    expect(panel.querySelectorAll('.ep-num').length).toBeGreaterThan(0)
    expect(panel.querySelector('.ep-block.bad')).not.toBeNull()
  })

  it('drops the panels a reviewer cannot act on', async () => {
    renderPage()
    await waitFor(() => expect(document.querySelector('.exam-points')).not.toBeNull())
    const body = document.body.textContent!
    for (const removed of [
      // 评价方的内部质检记录。必须改的那几处已并入考点小结并可跳转。
      '评价指出的问题',
      '无缺陷记录',
      // 校验器原始输出，英文 + 阈值口径，而且按定义不影响采用。
      '提示（不影响采用）',
      'dialogue words outside',
      // 盲评的计数口径。留下的是它唯一可行动的产出（哪几个点听不出来）。
      '盲读复核',
      '计划',
      '听出',
    ]) {
      expect(body, removed).not.toContain(removed)
    }
    // 篇幅数字留着——它是「够不够 600-650 词」这个判断的原始依据，只是不再用校验器的英文说。
    expect(body).toContain('篇幅')
  })
})
