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
import type { AudioStatusResponse, MaterialRecord } from '@/contracts/api'
import { buildRecord } from '@/mocks/fixtures'
import { MaterialPage } from './MaterialPage'

// 生产形状的 material_id（`YYYYMMDD-<scenario_key>-<8 hex>`）。用假形状会让这一页看起来能用
// 任何 id 工作，而后端的候选注册表只认这一种。
const MATERIAL_ID = '20260729-accommodation-rental-11aa22bb'
const baseRecord = buildRecord('clustered', {
  materialId: MATERIAL_ID,
  batchId: 'b1',
  scenarioKey: 'accommodation-rental',
  index: 0,
})
/** 每个测试自己决定这一套带不带校验意见；beforeEach 复位成不带。 */
let record: MaterialRecord = baseRecord

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
  record = baseRecord
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
    // 只有能力项。盲评结论（听不出来 / 听着有歧义）不在这里——那是「能不能出题」，
    // 不是「这套材料有什么」，而且红块会被读成「材料不能用」。
    expect(text).not.toContain('听不出来')
    expect(text).not.toContain('听着有歧义')
    expect(panel.querySelectorAll('.ep-num').length).toBeGreaterThan(0)
    expect(panel.querySelector('.ep-block.bad')).toBeNull()
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

/* ── 校验意见（校验从门卫改成质检报告之后新出现的一类材料） ─────────────────── */

describe('MaterialPage 结构校验意见', () => {
  const FINDINGS = [
    'blueprint.items[4].turn_index 20 does not carry its evidence (found at turn 21)',
    'dialogue words outside 450-750: 812 (over the 600-650 target by 187 words)',
    'blueprint must mark at least 3 confirmed items; found 1',
  ]

  it('says nothing at all when the material validated cleanly', async () => {
    renderPage()
    await waitFor(() => expect(document.querySelector('.exam-points')).not.toBeNull())
    // 没有意见就不该有面板：一个写着「0 条」的空面板只会让人以为漏了什么。
    expect(document.querySelector('.vn-list')).toBeNull()
    expect(document.body.textContent).not.toContain('结构校验意见')
  })

  it('states the findings as places to look, in the 命题人 vocabulary', async () => {
    record = { ...baseRecord, validation_findings: FINDINGS }
    renderPage()
    const list = await waitFor(() => {
      const found = document.querySelector('.vn-list')
      if (!found) throw new Error('校验意见面板尚未渲染')
      return found
    })
    expect(list.querySelectorAll('li')).toHaveLength(3)

    const text = list.textContent!
    // 校验器的英文阈值原文一律不上页面——这正是上一轮把「提示（不影响采用）」删掉的理由。
    for (const raw of ['blueprint.items', 'turn_index', 'dialogue words outside', 'confirmed items']) {
      expect(text, raw).not.toContain(raw)
    }
    // 措辞是「看这里」，不是「这里坏了 / 你去修」：校验器自己会判错真题。
    expect(document.body.textContent).toContain('材料本身完整可用')
    for (const blaming of ['缺陷', '不合格', '请修改', '错误']) {
      expect(text, blaming).not.toContain(blaming)
    }
    // 带题号的那一条给出可跳转的点号——离开原文这条意见没有意义。
    expect(list.querySelectorAll('.ep-num').length).toBeGreaterThan(0)
  })

  it('keeps an unrecognised finding verbatim rather than dropping it', async () => {
    record = { ...baseRecord, validation_findings: ['some brand new rule nobody has mapped yet'] }
    renderPage()
    const list = await waitFor(() => {
      const found = document.querySelector('.vn-list')
      if (!found) throw new Error('校验意见面板尚未渲染')
      return found
    })
    // 翻不动的线索仍然是线索。悄悄丢掉会让人以为材料是干净的。
    expect(list.textContent).toContain('some brand new rule nobody has mapped yet')
  })

  it('still offers 生成音频: a material with findings is fully operable', async () => {
    record = { ...baseRecord, validation_findings: FINDINGS }
    renderPage()
    // 「前端展示了的材料，后端必须保留其可操作状态」——带校验意见不等于降级。
    const button = await screen.findByRole('button', { name: /生成音频/ })
    await userEvent.click(button)
    expect(previewCalls).toEqual([MATERIAL_ID])
  })
})

/**
 * 出路。这一页过去是**单向**的：从结果页的「阅读全文」进来，页面上没有任何回去的入口，只剩浏览器
 * 后退键——而它是全宽布局、跟结果页长得不像，读起来像是离开了那个批次。
 */
describe('返回批次', () => {
  it('顶部有回到这一批的入口', async () => {
    record = baseRecord
    renderPage()
    const back = await screen.findByRole('link', { name: /返回批次/ })
    // batchId 取自材料自己，不靠 store：看历史批次的材料时 store 装的是当前活批次，
    // 用它会把用户送回另一批。
    expect(back.getAttribute('href')).toBe('/batches/b1')
  })

  it('「对比本场景」带上 batch，否则对比页取不到材料', async () => {
    record = baseRecord
    renderPage()
    const compare = await screen.findByRole('link', { name: /对比本场景/ })
    // 对比页对历史批次只能靠 `?batch=` 取材料（真实后端没有按场景列材料的路由）。
    expect(compare.getAttribute('href')).toBe('/compare/accommodation-rental?batch=b1')
  })
})
