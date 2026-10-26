import { afterEach, expect, it, vi } from 'vitest'
import { adminPrompts } from './adminPrompts'
import { setTransport, useRealTransport } from './http'

afterEach(() => {
  vi.unstubAllGlobals()
  useRealTransport()
})

it('uses web REST paths, session credentials, CAS bodies, and encoded trial IDs', async () => {
  const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response('{}', { status: 200 })))
  vi.stubGlobal('fetch', fetchMock)
  const adapter = vi.fn()
  setTransport(adapter)
  const controller = new AbortController()
  await adminPrompts.get(controller.signal)
  await adminPrompts.save({ role: 'business text' }, null)
  await adminPrompts.publish('active-etag', 'draft-etag')
  await adminPrompts.rollback('old-release', 'active-etag')
  await adminPrompts.test({ prompt_id: 'role', input: 'sample', model_id: 'model', draft_etag: 'draft-etag' })
  await adminPrompts.result('test/id', controller.signal)
  expect(adapter).not.toHaveBeenCalled()
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    '/api/admin/prompts', '/api/admin/prompts/draft', '/api/admin/prompts/publish',
    '/api/admin/prompts/rollback', '/api/admin/prompts/test', '/api/admin/prompts/test/test%2Fid',
  ])
  expect(fetchMock.mock.calls[0]![1]).toMatchObject({ credentials: 'same-origin', signal: controller.signal })
  expect(JSON.parse(fetchMock.mock.calls[1]![1].body)).toEqual({ values: { role: 'business text' }, expected_etag: null })
  expect(JSON.parse(fetchMock.mock.calls[2]![1].body)).toEqual({ expected_etag: 'active-etag', draft_etag: 'draft-etag' })
  expect(JSON.parse(fetchMock.mock.calls[3]![1].body)).toEqual({ release_id: 'old-release', expected_etag: 'active-etag' })
  expect(JSON.parse(fetchMock.mock.calls[4]![1].body)).toEqual({ prompt_id: 'role', input: 'sample', model_id: 'model', draft_etag: 'draft-etag' })
})

it('preserves a conflict status for the editor to handle', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
    JSON.stringify({ error: { code: 'CONFLICT', message: '配置已更新' } }), { status: 409 },
  )))
  await expect(adminPrompts.save({}, 'stale')).rejects.toMatchObject({
    name: 'ApiError', status: 409, code: 'CONFLICT', message: '配置已更新',
  })
})

it('preserves HTTP status even when the server returns a different error envelope', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
    JSON.stringify({ detail: 'stale etag' }), { status: 409 },
  )))
  await expect(adminPrompts.publish('stale', 'draft')).rejects.toMatchObject({ status: 409 })
})

it('surfaces server-side administrator rejection', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
    JSON.stringify({ error: { code: 'FORBIDDEN', message: '仅管理员可访问' } }), { status: 403 },
  )))
  await expect(adminPrompts.get()).rejects.toMatchObject({ status: 403, code: 'FORBIDDEN' })
})
