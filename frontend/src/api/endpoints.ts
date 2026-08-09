/** design.md §8.1 endpoints, one function each. */
import type {
  AudioStatusResponse,
  BatchHistoryDetail,
  BatchHistoryResponse,
  BatchListResponse,
  BatchSnapshot,
  CreateBatchRequest,
  CreateBatchResponse,
  MaterialListResponse,
  MaterialQuestionsResponse,
  MaterialRecord,
  PreviewAudioResponse,
  SelectMaterialResponse,
} from '@/contracts/api'
import { request } from './http'
import type {
  CreateMaterialComment,
  MaterialCommentsDocument,
} from '@/contracts/comments'

export const api = {
  createBatch: (body: CreateBatchRequest) =>
    request<CreateBatchResponse>({ method: 'POST', path: '/batches', body }),

  getBatch: (batchId: string) =>
    request<BatchSnapshot>({ method: 'GET', path: `/batches/${batchId}` }),

  listBatches: (params?: { status?: string; limit?: number; cursor?: string }) => {
    const q = new URLSearchParams()
    if (params?.status) q.set('status', params.status)
    if (params?.limit) q.set('limit', String(params.limit))
    if (params?.cursor) q.set('cursor', params.cursor)
    const qs = q.toString()
    return request<BatchListResponse>({ method: 'GET', path: `/batches${qs ? `?${qs}` : ''}` })
  },

  retryBatch: (batchId: string, body: { material_ids?: string[]; scenario_keys?: string[] }) =>
    request<{ batch_id: string }>({ method: 'POST', path: `/batches/${batchId}/retry`, body }),

  /**
   * 历史批次列表。served by the WEB TIER, not the Runtime.
   *
   * 与 `listBatches` 的区别不是分页参数：那一个读的是本页会话里的批次（`sessions` Map，刷新即失），
   * 这一个读的是 S3 里的批次记录（`web/batch_history.py`），是历史面板唯一可信的来源。两个都留着，
   * 因为 `listBatches` 是 §8 契约的一部分，而 §8 从来没有历史这个概念。
   */
  batchHistory: () => request<BatchHistoryResponse>({ method: 'GET', path: '/batch-history' }),

  /** 一个历史批次的完整材料。点开某条历史记录时调。 */
  batchHistoryDetail: (batchId: string) =>
    request<BatchHistoryDetail>({ method: 'GET', path: `/batch-history/${batchId}` }),

  /**
   * 记录「已提交」。这是后端原来没有的那个状态转移。
   *
   * 刻意不是 `selectMaterial`：那个会认领候选组、丢弃同场景的另一套、并且付 Polly 的钱。提交审核
   * 是审阅者说「这几套是我的选择」，不该销毁任何东西也不该花钱。见 web/batch_history.py。
   */
  submitBatch: (batchId: string, materialIds: string[]) =>
    request<BatchHistoryDetail>({
      method: 'POST',
      path: `/batch-history/${batchId}/submit`,
      body: { material_ids: materialIds },
    }),

  /**
   * 撤回「已提交」。给 materialIds 就只撤这几套，批次仍是已提交但清单变短；不给就整批撤回。
   *
   * 队列页的撤回按钮原来只删本机队列那一条，后端状态没动——审阅者把全部撤完，看到的是一个空队列
   * 配一个仍写着「已提交」的批次，而且没有任何操作能清掉它。
   */
  withdrawBatch: (batchId: string, materialIds?: string[]) =>
    request<BatchHistoryDetail>({
      method: 'POST',
      path: `/batch-history/${batchId}/withdraw`,
      body: materialIds ? { material_ids: materialIds } : {},
    }),

  getMaterial: (materialId: string) =>
    request<MaterialRecord>({ method: 'GET', path: `/materials/${materialId}` }),

  listMaterials: (params?: { status?: string; scenario_key?: string; limit?: number }) => {
    const q = new URLSearchParams()
    if (params?.status) q.set('status', params.status)
    if (params?.scenario_key) q.set('scenario_key', params.scenario_key)
    if (params?.limit) q.set('limit', String(params.limit))
    const qs = q.toString()
    return request<MaterialListResponse>({ method: 'GET', path: `/materials${qs ? `?${qs}` : ''}` })
  },

  /** MUST be idempotent server-side: a repeat must not double-bill Polly. */
  selectMaterial: (materialId: string) =>
    request<SelectMaterialResponse>({
      method: 'POST',
      path: `/materials/${materialId}/select`,
      body: {},
    }),

  /**
   * 生成音频以便试听，不选定这一套。
   *
   * MUST NOT go through `selectMaterial`: 那个端点会认领候选组并丢弃同场景的另一套，只想先听
   * 一遍的人会因此永久失去备选。合成结果与选定共用同一份 clip，所以之后真的选定不会重复计费。
   * 同样必须幂等：重复 POST 返回同一个 job。
   */
  previewAudio: (materialId: string) =>
    request<PreviewAudioResponse>({
      method: 'POST',
      path: `/materials/${materialId}/audio`,
      body: {},
    }),

  getAudio: (materialId: string) =>
    request<AudioStatusResponse>({ method: 'GET', path: `/materials/${materialId}/audio` }),

  /**
   * 这套材料已交付的题目包，给「题目预览」页签。served by the WEB TIER.
   *
   * `questions: null` 是 200，不是 404——「还没出题」是材料一生里的常态（出题在材料之后，随时可能
   * 被时钟停在半路），把它做成错误会让浏览器的错误分支变成主路径。所以调用方判 `null`，不判异常。
   *
   * 带上 `batchId` 换到的是**为什么没有题**：后端只在题目缺席时才去读 slot 状态，所以有题的常见
   * 路径一次多余的 S3 读都不付。不带也能用，只是「暂无题目」就只能说这四个字。
   */
  materialQuestions: (materialId: string, batchId?: string) =>
    request<MaterialQuestionsResponse>({
      method: 'GET',
      path: `/material-questions/${materialId}${batchId ? `?batch_id=${encodeURIComponent(batchId)}` : ''}`,
    }),

  materialComments: (materialId: string) =>
    request<MaterialCommentsDocument>({
      method: 'GET',
      path: `/material-comments/${encodeURIComponent(materialId)}`,
    }),

  createMaterialComment: (materialId: string, body: CreateMaterialComment) =>
    request<MaterialCommentsDocument>({
      method: 'POST',
      path: `/material-comments/${encodeURIComponent(materialId)}`,
      body,
    }),

  deleteMaterialComment: (materialId: string, commentId: string) =>
    request<MaterialCommentsDocument>({
      method: 'DELETE',
      path: `/material-comments/${encodeURIComponent(materialId)}/${encodeURIComponent(commentId)}`,
    }),
}
