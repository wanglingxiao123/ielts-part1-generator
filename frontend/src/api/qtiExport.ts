/**
 * `GET /api/material-qti/{id}`：把一套题目导出成 QTI 2.2.4 内容包。served by the WEB TIER.
 *
 * 不走 `request()` / `Transport`：那条路的返回值是 JSON，而这里要拿的是一个 zip 的字节。跟
 * `questionRevisions.ts` 一样直接 `fetch`，凭据同样是 HttpOnly 的会话 cookie，401 同样通知
 * `notifyUnauthorized`。留一个 `setQtiFetch` 接缝给 mock 模式与测试，形状同 `setAuthFetch`。
 *
 * 下载的触发方式是 blob → `<a download>`，而不是 `window.location = url`：后者在 4xx 时会把一页
 * JSON 错误当成文件保存下来，用户拿到一个打不开的「zip」而页面上什么都没说。走 fetch 才能把
 * 422 的原因列表读出来渲染成一句话。
 */
import { getConfig } from '@/config/runtimeConfig'
import { ApiError, CREDENTIALS, notifyUnauthorized } from './http'

export type QtiExportFormat = 'zip' | 'item' | 'summary'

export interface QtiExportSummary {
  material_id: string
  version_ordinal: number
  item_identifier: string
  filename: string
  questions: number
  accept_entries: number
  reject_entries: number
  /** 包内 review.txt 的每一行：需要人工确认的容错集决定。 */
  review: string[]
}

export interface QtiDownload {
  filename: string
  itemIdentifier: string
  /** 包内 review.txt 的条数；0 表示这套题的容错集没有任何待人工确认项。 */
  reviewCount: number
  bytes: number
}

export type QtiFetch = (path: string, init: RequestInit) => Promise<Response>

let qtiFetch: QtiFetch = (path, init) => fetch(`${getConfig().apiBaseUrl}${path}`, init)

export function setQtiFetch(fn: QtiFetch) {
  qtiFetch = fn
}

export function qtiExportPath(
  materialId: string,
  versionId: string | undefined,
  format: QtiExportFormat,
): string {
  const q = new URLSearchParams()
  if (versionId) q.set('version_id', versionId)
  if (format !== 'zip') q.set('format', format)
  const qs = q.toString()
  return `/material-qti/${encodeURIComponent(materialId)}${qs ? `?${qs}` : ''}`
}

interface ServerError {
  error?: { code?: string; message?: string; detail?: { reasons?: string[] } }
}

/**
 * 把一个非 2xx 的响应变成一句 `ApiError`。
 *
 * 422 的 `detail.reasons` 是转换器逐条列出的门禁失败原因（G1 上游审核 … G6 结构块题号）。它们
 * 本来是给命题人看的中文句子，所以直接拼进 message 的后半段，而不是只说「未通过门禁」——那样
 * 用户唯一能做的事是去问开发。
 */
async function toApiError(response: Response, fallback: string): Promise<ApiError> {
  if (response.status === 401) {
    notifyUnauthorized()
    return new ApiError(401, 'UNAUTHENTICATED', '登录状态已失效，请重新登录')
  }
  const body = (await response.json().catch(() => null)) as ServerError | null
  const code = body?.error?.code ?? 'QTI_EXPORT_FAILED'
  let message = body?.error?.message ?? fallback
  const reasons = body?.error?.detail?.reasons
  if (reasons && reasons.length > 0) {
    const shown = reasons.slice(0, 3).join('；')
    const more = reasons.length > 3 ? `；另有 ${reasons.length - 3} 项` : ''
    message = `${message} ${shown}${more}`
  }
  return new ApiError(response.status, code, message, body?.error?.detail)
}

/** RFC 6266 的 `attachment; filename="x.zip"`；解析不出来就用调用方给的兜底名。 */
export function filenameFromDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(header)
  return m?.[1] ? decodeURIComponent(m[1]) : fallback
}

export async function fetchQtiSummary(
  materialId: string,
  versionId?: string,
): Promise<QtiExportSummary> {
  const response = await qtiFetch(qtiExportPath(materialId, versionId, 'summary'), {
    method: 'GET',
    credentials: CREDENTIALS,
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) throw await toApiError(response, '读取导出概览失败')
  return (await response.json()) as QtiExportSummary
}

/**
 * 取回内容包的字节与元信息。不触发浏览器保存，方便测试；保存交给 `saveBlob`。
 */
export async function fetchQtiPackage(
  materialId: string,
  versionId?: string,
  format: Exclude<QtiExportFormat, 'summary'> = 'zip',
): Promise<QtiDownload & { blob: Blob }> {
  const response = await qtiFetch(qtiExportPath(materialId, versionId, format), {
    method: 'GET',
    credentials: CREDENTIALS,
  })
  if (!response.ok) throw await toApiError(response, '导出 QTI 包失败')
  const blob = await response.blob()
  const fallback = `ielts-${materialId}.${format === 'item' ? 'xml' : 'zip'}`
  return {
    blob,
    bytes: blob.size,
    filename: filenameFromDisposition(response.headers.get('Content-Disposition'), fallback),
    itemIdentifier: response.headers.get('X-QTI-Item-Identifier') ?? '',
    reviewCount: Number(response.headers.get('X-QTI-Review-Count') ?? '0') || 0,
  }
}

/** 浏览器端「另存为」。单独一个函数，好让组件测试把它 stub 掉。 */
export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
  // 下一轮事件循环再释放：Safari 在 click 同步返回后才开始读 blob。
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export async function downloadQtiPackage(
  materialId: string,
  versionId?: string,
  format: Exclude<QtiExportFormat, 'summary'> = 'zip',
  save: (blob: Blob, filename: string) => void = saveBlob,
): Promise<QtiDownload> {
  const { blob, ...meta } = await fetchQtiPackage(materialId, versionId, format)
  save(blob, meta.filename)
  return meta
}
