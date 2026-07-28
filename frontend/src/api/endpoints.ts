/** design.md §8.1 endpoints, one function each. */
import type {
  AudioStatusResponse,
  BatchListResponse,
  BatchSnapshot,
  CreateBatchRequest,
  CreateBatchResponse,
  MaterialListResponse,
  MaterialRecord,
  SelectMaterialResponse,
} from '@/contracts/api'
import { request } from './http'

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

  getAudio: (materialId: string) =>
    request<AudioStatusResponse>({ method: 'GET', path: `/materials/${materialId}/audio` }),
}
