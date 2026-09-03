/**
 * `api/qtiExport.ts`：路径、文件名、错误变成中文一句话。
 *
 * 最要紧的一条是 422：服务端把门禁失败原因放在 `detail.reasons`，这里必须把它们带到 `message`
 * 里——`userMessage()` 只渲染 `ApiError.message`，原因留在 detail 里就等于没告诉用户。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './http'
import {
  downloadQtiPackage,
  fetchQtiSummary,
  filenameFromDisposition,
  qtiExportPath,
  setQtiFetch,
} from './qtiExport'

const MATERIAL = '20260808-booking-hotel-45425df4'

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

afterEach(() => {
  vi.restoreAllMocks()
  setQtiFetch((path, init) => fetch(path, init))
})

describe('qtiExportPath', () => {
  it('omits the query for the default zip of the adopted version', () => {
    expect(qtiExportPath(MATERIAL, undefined, 'zip')).toBe(`/material-qti/${MATERIAL}`)
  })

  it('carries version and format when given', () => {
    expect(qtiExportPath(MATERIAL, 'v-2', 'summary')).toBe(
      `/material-qti/${MATERIAL}?version_id=v-2&format=summary`,
    )
  })

  it('does not send version_id=original — the server treats absence as the adopted version, and the caller maps original to undefined', () => {
    expect(qtiExportPath(MATERIAL, undefined, 'item')).toBe(`/material-qti/${MATERIAL}?format=item`)
  })
})

describe('filenameFromDisposition', () => {
  it('reads a quoted filename', () => {
    expect(filenameFromDisposition('attachment; filename="ielts-x-v1.zip"', 'f')).toBe(
      'ielts-x-v1.zip',
    )
  })
  it('reads an unquoted and an RFC 5987 filename', () => {
    expect(filenameFromDisposition('attachment; filename=a.zip', 'f')).toBe('a.zip')
    expect(filenameFromDisposition("attachment; filename*=UTF-8''b%20c.zip", 'f')).toBe('b c.zip')
  })
  it('falls back when the header is missing', () => {
    expect(filenameFromDisposition(null, 'fallback.zip')).toBe('fallback.zip')
  })
})

describe('downloadQtiPackage', () => {
  it('hands the blob to the saver under the server-chosen filename and reports the review count', async () => {
    const seen: string[] = []
    setQtiFetch((path) => {
      seen.push(path)
      return Promise.resolve(
        // 直接给字节：jsdom 的 Blob 没有 stream()，undici 的 Response 读不了它。
        new Response(new Uint8Array([0x50, 0x4b, 3, 4]), {
          status: 200,
          headers: {
            'Content-Type': 'application/zip',
            'Content-Disposition': `attachment; filename="ielts-${MATERIAL}-v2.zip"`,
            'X-QTI-Review-Count': '7',
            'X-QTI-Item-Identifier': `ielts-${MATERIAL}-v2`,
          },
        }),
      )
    })
    const saved: Array<{ name: string; size: number }> = []
    const result = await downloadQtiPackage(MATERIAL, 'v-2', 'zip', (blob, name) =>
      saved.push({ name, size: blob.size }),
    )
    expect(seen).toEqual([`/material-qti/${MATERIAL}?version_id=v-2`])
    expect(saved).toEqual([{ name: `ielts-${MATERIAL}-v2.zip`, size: 4 }])
    expect(result).toMatchObject({
      filename: `ielts-${MATERIAL}-v2.zip`,
      itemIdentifier: `ielts-${MATERIAL}-v2`,
      reviewCount: 7,
      bytes: 4,
    })
  })

  it('turns a 422 into an ApiError whose message carries the gate reasons', async () => {
    setQtiFetch(() =>
      Promise.resolve(
        jsonResponse(
          {
            error: {
              code: 'QTI_EXPORT_REJECTED',
              message: '这套题目未通过导出门禁，不能生成 QTI 包。',
              detail: {
                reasons: ['G2: Q3 两侧答案不一致', 'G3: 题号不是从 1 连续无重复', 'G5: x', 'G5: y'],
              },
            },
          },
          422,
        ),
      ),
    )
    const saver = vi.fn()
    await expect(downloadQtiPackage(MATERIAL, undefined, 'zip', saver)).rejects.toMatchObject({
      status: 422,
      code: 'QTI_EXPORT_REJECTED',
      message:
        '这套题目未通过导出门禁，不能生成 QTI 包。 G2: Q3 两侧答案不一致；G3: 题号不是从 1 连续无重复；G5: x；另有 1 项',
    })
    expect(saver).not.toHaveBeenCalled()
  })

  it('does not save a JSON error body as if it were a zip', async () => {
    setQtiFetch(() =>
      Promise.resolve(
        jsonResponse({ error: { code: 'QUESTIONS_NOT_FOUND', message: '这套材料还没有可导出的题目。' } }, 404),
      ),
    )
    const saver = vi.fn()
    const err = await downloadQtiPackage(MATERIAL, undefined, 'zip', saver).catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).message).toBe('这套材料还没有可导出的题目。')
    expect(saver).not.toHaveBeenCalled()
  })
})

describe('fetchQtiSummary', () => {
  it('returns the summary JSON', async () => {
    setQtiFetch((path) => {
      expect(path).toBe(`/material-qti/${MATERIAL}?format=summary`)
      return Promise.resolve(
        jsonResponse({
          material_id: MATERIAL,
          version_ordinal: 1,
          item_identifier: `ielts-${MATERIAL}-v1`,
          filename: `ielts-${MATERIAL}-v1.zip`,
          questions: 10,
          accept_entries: 20,
          reject_entries: 11,
          review: ['Q3: …'],
        }),
      )
    })
    const summary = await fetchQtiSummary(MATERIAL)
    expect(summary.questions).toBe(10)
    expect(summary.review).toHaveLength(1)
  })
})
