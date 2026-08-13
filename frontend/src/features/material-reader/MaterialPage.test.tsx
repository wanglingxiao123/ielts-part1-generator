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
import type {
  AudioStatusResponse,
  MaterialQuestionsResponse,
  MaterialRecord,
} from '@/contracts/api'
import type { QuestionRevisionRecord } from '@/contracts/questionVersions'
import type { MaterialComment } from '@/contracts/comments'
import type { QuestionPackageVersion } from '@/contracts/questionVersions'
import { buildRecord, QUESTION_PACKAGE } from '@/mocks/fixtures'
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
const previewVersionCalls: Array<string | undefined> = []
const audioVersionCalls: Array<string | undefined> = []

/**
 * 题目端点。默认「暂无题目」，因为那是绝大多数材料一生中的常态——把有题当默认会让「没题时这一页
 * 长什么样」永远测不到。调用参数被记下来：这一页刻意在页签被打开之前不去取题目。
 */
let questions: MaterialQuestionsResponse = {
  material_id: MATERIAL_ID,
  questions: null,
  slot: null,
  request_status: null,
}
const questionCalls: Array<[string, string | undefined]> = []
let commentCalls = 0
let revisionRequest: QuestionRevisionRecord | null = null
let versionOverride: QuestionPackageVersion[] | null = null
let materialComments: MaterialComment[] = []

vi.mock('@/api/endpoints', () => ({
  api: {
    getMaterial: () => Promise.resolve(record),
    previewAudio: (id: string, versionId?: string) => {
      previewCalls.push(id)
      previewVersionCalls.push(versionId)
      // 真后端会把 job 建起来，下一次轮询就看得到；这里照做。
      audio = { status: 'queued', progress: { done: 0, total: 43 } }
      return Promise.resolve({ material_id: id, audio_job_id: 'job-1', repeat: false })
    },
    getAudio: (_id: string, versionId?: string) => {
      audioVersionCalls.push(versionId)
      return Promise.resolve(audio)
    },
    materialQuestions: (id: string, batchId?: string) => {
      questionCalls.push([id, batchId])
      return Promise.resolve(questions)
    },
    materialComments: (id: string) => {
      commentCalls += 1
      return Promise.resolve({ material_id: id, comments: materialComments })
    },
    createMaterialComment: (id: string) =>
      Promise.resolve({ material_id: id, comments: [] }),
    deleteMaterialComment: (id: string) =>
      Promise.resolve({ material_id: id, comments: [] }),
    materialQuestionVersions: (id: string) =>
      Promise.resolve({
        material_id: id,
        active_version_id:
          versionOverride?.find((version) => version.is_active)?.id ?? 'original',
        versions: versionOverride ?? (questions.questions
          ? [{
              id: 'original',
              created_at: '2026-08-11T00:00:00Z',
              based_on_version_id: null,
              source_comment_ids: [],
              status: 'original',
              package: questions.questions,
              is_active: true,
              ordinal: 1,
            }]
          : []),
        revision_request: revisionRequest,
      }),
    adoptQuestionVersion: (id: string, versionId: string) =>
      Promise.resolve({ material_id: id, active_version_id: versionId }),
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
  previewVersionCalls.length = 0
  audioVersionCalls.length = 0
  audio = { status: 'not_requested', progress: { done: 0, total: 0 } }
  questionCalls.length = 0
  commentCalls = 0
  revisionRequest = null
  versionOverride = null
  materialComments = []
  questions = { material_id: MATERIAL_ID, questions: null, slot: null, request_status: null }
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

  it('新版材料不显示原始材料的质量提示', async () => {
    record = {
      ...baseRecord,
      degraded: true,
      audit_rejection: { message: '原始材料盲审未通过' },
      validation_findings: ['dialogue words outside 450-750: 812'],
    }
    questions = {
      material_id: MATERIAL_ID,
      questions: QUESTION_PACKAGE,
      slot: null,
      request_status: null,
    }
    versionOverride = [{
      id: 'material-v2',
      created_at: '2026-08-13T00:00:00Z',
      based_on_version_id: 'original',
      source_comment_ids: ['comment-1'],
      status: 'ready',
      package: QUESTION_PACKAGE,
      material: baseRecord.material,
      blueprint: baseRecord.blueprint,
      operation: 'revise_material',
      is_active: true,
      ordinal: 2,
    }]

    renderPage()
    await screen.findByRole('combobox', { name: '材料与题目版本' })

    expect(screen.queryByText('未经修改环节')).not.toBeInTheDocument()
    expect(screen.queryByText('这一套有明显缺陷')).not.toBeInTheDocument()
    expect(screen.queryByText('结构校验意见')).not.toBeInTheDocument()
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

/* ── 标题下的两个页签 ────────────────────────────────────────────────────── */

describe('MaterialPage 页签', () => {
  it('版本选择同时切换材料、Turn 批注和音频归属', async () => {
    const revisedMaterial = structuredClone(baseRecord.material)
    revisedMaterial.listening_material_parts[0]!.script.turns[1]!.text =
      'VERSION TWO MATERIAL'
    versionOverride = [
      {
        id: 'original',
        created_at: '',
        based_on_version_id: null,
        source_comment_ids: [],
        status: 'original',
        package: QUESTION_PACKAGE,
        is_active: true,
        ordinal: 1,
      },
      {
        id: 'version-2',
        created_at: '2026-08-12T12:00:00Z',
        based_on_version_id: 'original',
        source_comment_ids: ['comment-v2'],
        status: 'ready',
        operation: 'revise_material',
        material: revisedMaterial,
        blueprint: baseRecord.blueprint,
        package: QUESTION_PACKAGE,
        audio: { status: 'needs_synthesis', version_key: 'version-2' },
        is_active: false,
        ordinal: 2,
      },
    ]
    materialComments = [
      {
        id: 'comment-original',
        created_at: '2026-08-12T10:00:00Z',
        anchor: { type: 'turn', index: 1 },
        severity: 'minor',
        text: 'original comment',
      },
      {
        id: 'comment-v2',
        created_at: '2026-08-12T12:00:00Z',
        anchor: { type: 'turn', index: 1 },
        severity: 'minor',
        text: 'version two comment',
        version_id: 'version-2',
      },
    ]

    renderPage()
    expect(await screen.findByText('V1 · 当前采用')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /批注 \(1\)/ })).toBeInTheDocument()
    audioVersionCalls.length = 0

    await userEvent.selectOptions(
      screen.getByRole('combobox', { name: '材料与题目版本' }),
      'version-2',
    )

    await waitFor(() =>
      expect(document.querySelector('.material-version-state')).toHaveTextContent(
        'V2 · 历史版本',
      ),
    )
    expect(screen.getByText('VERSION TWO MATERIAL')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /批注 \(1\)/ }))
    expect(screen.getByText('version two comment')).toBeInTheDocument()
    expect(screen.queryByText('original comment')).not.toBeInTheDocument()
    expect(screen.getByText('需要生成新版录音')).toBeInTheDocument()
    expect(audioVersionCalls).toEqual(['version-2'])
  })

  it('新版录音就绪后不再显示需要生成的静态版本状态', async () => {
    audio = {
      status: 'ready',
      progress: { done: 1, total: 1 },
      manifest: {
        material_id: MATERIAL_ID,
        generated_at: '2026-08-12T12:00:00Z',
        engine: 'polly',
        format: 'mp3',
        sample_rate_hz: 24_000,
        voice_map: { speaker1: 'Amy', speaker2: 'Arthur', speaker3: 'Brian' },
        total_duration_ms: 0,
        url_expires_at: '2026-08-12T13:00:00Z',
        segments: [],
      },
    }
    versionOverride = [
      {
        id: 'original',
        created_at: '',
        based_on_version_id: null,
        source_comment_ids: [],
        status: 'original',
        package: QUESTION_PACKAGE,
        is_active: true,
        ordinal: 1,
      },
      {
        id: 'version-2',
        created_at: '2026-08-12T12:00:00Z',
        based_on_version_id: 'original',
        source_comment_ids: [],
        status: 'ready',
        operation: 'revise_material',
        material: baseRecord.material,
        blueprint: baseRecord.blueprint,
        package: QUESTION_PACKAGE,
        audio: { status: 'needs_synthesis', version_key: 'version-2' },
        is_active: false,
        ordinal: 2,
      },
    ]

    renderPage()

    await userEvent.selectOptions(
      await screen.findByRole('combobox', { name: '材料与题目版本' }),
      'version-2',
    )
    await waitFor(() => expect(audioVersionCalls).toContain('version-2'))
    expect(screen.queryByText('需要生成新版录音')).not.toBeInTheDocument()
  })

  it('选择对话 Turn 后展开批注面板并显示锚点', async () => {
    renderPage()
    const toggle = await screen.findByRole('button', { name: /批注 \(0\)/ })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')

    await userEvent.click(document.querySelector('[data-turn="4"]') as HTMLElement)

    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('位置：Turn 4')).toBeInTheDocument()
  })

  it('标题下有 [对话原文] 与 [题目预览]，默认停在对话原文', async () => {
    renderPage()
    const script = await screen.findByRole('tab', { name: '对话原文' })
    const preview = screen.getByRole('tab', { name: '题目预览' })
    expect(script).toHaveAttribute('aria-selected', 'true')
    expect(preview).toHaveAttribute('aria-selected', 'false')
    // 「对话原文 —— 保持现有内容和行为不变」：进来时看到的仍然是原来那一页。
    expect(document.querySelector('.exam-points')).not.toBeNull()
  })

  /**
   * 打开页签之前不发请求。多数材料还没有题，而这一页最常见的用法是读原文——进页面就问一次注定
   * `questions: null` 的请求，只是给每次打开都加一次往返。
   */
  it('题目只在页签被打开之后才取，并带上 batch_id 以便解释「为什么没有」', async () => {
    renderPage()
    await screen.findByRole('tab', { name: '题目预览' })
    expect(questionCalls).toEqual([])

    await userEvent.click(screen.getByRole('tab', { name: '题目预览' }))
    await waitFor(() => expect(questionCalls.length).toBeGreaterThan(0))
    // batch_id 买到的是「为什么没有题」；没有它后端根本不去读 slot。
    expect(questionCalls[0]).toEqual([MATERIAL_ID, 'b1'])
  })

  it('切到题目预览时收起原文那一整套，切回来又完整回来', async () => {
    renderPage()
    await screen.findByRole('tab', { name: '题目预览' })
    await userEvent.click(screen.getByRole('tab', { name: '题目预览' }))
    await waitFor(() => expect(document.querySelector('.exam-points')).toBeNull())
    await userEvent.click(screen.getByRole('tab', { name: '对话原文' }))
    await waitFor(() => expect(document.querySelector('.exam-points')).not.toBeNull())
  })

  it('音频播放器留在页签之外：听音频与看题面是同时进行的动作', async () => {
    renderPage()
    await screen.findByRole('button', { name: /生成音频/ })
    await userEvent.click(screen.getByRole('tab', { name: '题目预览' }))
    // 藏进某一个页签会让另一个页签里的人失去它。
    expect(screen.getByRole('button', { name: /生成音频/ })).toBeInTheDocument()
  })

  it('有题时画出真实版式，答案默认隐藏', async () => {
    questions = {
      material_id: MATERIAL_ID,
      questions: QUESTION_PACKAGE,
      slot: null,
      request_status: null,
    }
    renderPage()
    await screen.findByRole('tab', { name: '题目预览' })
    await userEvent.click(screen.getByRole('tab', { name: '题目预览' }))
    await waitFor(() => expect(document.querySelector('.qp-table')).not.toBeNull())
    expect(document.querySelectorAll('.qp-form').length).toBe(1)
    expect(screen.getByRole('checkbox', { name: /显示答案和证据/ })).not.toBeChecked()
    expect(document.querySelector('.qp-reveals')).toBeNull()
  })

  it.each(['no_change', 'replan_questions'] as const)(
    '%s 终态会立即刷新批注结算状态',
    async (status) => {
      questions = {
        material_id: MATERIAL_ID,
        questions: QUESTION_PACKAGE,
        slot: null,
        request_status: null,
      }
      revisionRequest = {
        request_id: `request-${status}`,
        status,
        base_version_id: 'original',
      }
      renderPage()
      await screen.findByRole('tab', { name: '题目预览' })
      await userEvent.click(screen.getByRole('tab', { name: '题目预览' }))

      await waitFor(() => expect(commentCalls).toBeGreaterThanOrEqual(2))
    },
  )

  /**
   * 从题解跳回原文。先切回 [对话原文] 再跳，否则跳转落在一个没挂载的阅读器上——点了没反应，
   * 而这个按钮的全部意义就是「让我看这句话在哪」。
   */
  it('点原文轮次会切回对话原文页签并滚到那一句', async () => {
    questions = {
      material_id: MATERIAL_ID,
      questions: QUESTION_PACKAGE,
      slot: null,
      request_status: null,
    }
    renderPage()
    await screen.findByRole('tab', { name: '题目预览' })
    await userEvent.click(screen.getByRole('tab', { name: '题目预览' }))
    await userEvent.click(screen.getByRole('checkbox', { name: /显示答案和证据/ }))
    await waitFor(() => expect(document.querySelector('.qp-turn')).not.toBeNull())
    await userEvent.click(document.querySelectorAll('.qp-turn')[0] as HTMLElement)
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: '对话原文' })).toHaveAttribute(
        'aria-selected',
        'true',
      ),
    )
    expect(document.querySelector('.exam-points')).not.toBeNull()
  })
})

/* ── 没有题目的几种处境 ──────────────────────────────────────────────────── */

describe('MaterialPage 题目预览的异常状态', () => {
  async function openPreview() {
    renderPage()
    await screen.findByRole('tab', { name: '题目预览' })
    await userEvent.click(screen.getByRole('tab', { name: '题目预览' }))
  }

  const slotBase = {
    slot_id: 'slot-1',
    scenario: 'accommodation-rental',
    material_id: MATERIAL_ID,
    created_at: 1_770_000_000,
    resumable: true,
    checkpointed: false,
    system_fault: false,
    last_failure: null,
    attempts: { questions: 1 },
  }

  it('从没出过题时是一句中性的说明，不是一条报错', async () => {
    await openPreview()
    expect(await screen.findByText('暂无题目')).toBeInTheDocument()
    // 中性语气走 info，不用 banner-bad：没有题是常态，画成红色会让人以为出了故障。
    expect(document.querySelector('.banner-bad')).toBeNull()
  })

  it('还在出题时说「正在生成中」，并且不给「重新查看」按钮', async () => {
    questions = {
      material_id: MATERIAL_ID,
      questions: null,
      slot: { ...slotBase, state: 'questions_pending' },
      request_status: 'running',
    }
    await openPreview()
    expect(await screen.findByText(/题目正在生成中/)).toBeInTheDocument()
    // 它会自己变好，所以不该出现一个让人反复点的按钮。
    expect(screen.queryByRole('button', { name: '重新查看' })).toBeNull()
  })

  it('停在 checkpoint 时说清材料已存住、要等下一次运行', async () => {
    questions = {
      material_id: MATERIAL_ID,
      questions: null,
      slot: {
        ...slotBase,
        state: 'questions_pending',
        checkpointed: true,
        last_failure: { stage: 'questions', reason: 'time_budget' },
      },
      request_status: 'incomplete',
    }
    await openPreview()
    expect(await screen.findByText(/出题已暂停/)).toBeInTheDocument()
    expect(document.body.textContent).toContain('不会重新生成材料')
  })

  it('名额用尽与系统故障都画成红色，且说法不同', async () => {
    questions = {
      material_id: MATERIAL_ID,
      questions: null,
      slot: { ...slotBase, state: 'exhausted', resumable: false },
      request_status: 'incomplete',
    }
    await openPreview()
    expect(await screen.findByText(/候选材料已用尽/)).toBeInTheDocument()
    expect(document.querySelector('.banner-bad')).not.toBeNull()
  })

  it('系统故障说明这不是材料质量问题', async () => {
    questions = {
      material_id: MATERIAL_ID,
      questions: null,
      slot: { ...slotBase, state: 'questions_pending', system_fault: true, resumable: false },
      request_status: 'system_failure',
    }
    await openPreview()
    expect(await screen.findByText(/系统故障中断/)).toBeInTheDocument()
    expect(document.body.textContent).toContain('不是材料质量问题')
  })
})
