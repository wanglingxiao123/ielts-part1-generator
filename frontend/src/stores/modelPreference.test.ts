import { beforeEach, expect, it, vi } from 'vitest'

const { listModels } = vi.hoisted(() => ({ listModels: vi.fn() }))
vi.mock('@/api/endpoints', () => ({ api: { listModels } }))

const kimi = 'moonshotai.kimi-k2.5'
const legacy = 'openai.gpt-5.6-luna'

beforeEach(() => {
  vi.resetModules()
  listModels.mockReset()
  window.localStorage.clear()
})

it('replaces a saved legacy model with the China deployment Kimi default', async () => {
  window.localStorage.setItem('ielts:selected-model', legacy)
  listModels.mockResolvedValue({
    models: [{ id: kimi, label: 'Kimi K2.5' }], default_model_id: kimi,
  })
  const preference = await import('./modelPreference')
  preference.loadModels()
  await vi.waitFor(() => expect(preference.selectedModelId()).toBe(kimi))
  expect(window.localStorage.getItem('ielts:selected-model')).toBe(kimi)
  preference.selectModel(legacy)
  expect(preference.selectedModelId()).toBe(kimi)
})

it('preserves a saved model only when explicitly included in the catalogue', async () => {
  window.localStorage.setItem('ielts:selected-model', legacy)
  listModels.mockResolvedValue({
    models: [{ id: kimi, label: 'Kimi K2.5' }, { id: legacy, label: 'GPT-5.6' }],
    default_model_id: kimi,
  })
  const preference = await import('./modelPreference')
  preference.loadModels()
  await vi.waitFor(() => expect(preference.selectedModelId()).toBe(legacy))
  preference.selectModel(kimi)
  expect(preference.selectedModelId()).toBe(kimi)
  expect(window.localStorage.getItem('ielts:selected-model')).toBe(kimi)
})
