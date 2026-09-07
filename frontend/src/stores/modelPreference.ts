import { useSyncExternalStore } from 'react'
import { api } from '@/api/endpoints'

const KEY = 'ielts:selected-model'
type Model = { id: string; label: string }
type State = {
  models: Model[]
  selectedModelId: string
  defaultModelId: string
  loading: boolean
  warning: string | null
}

let state: State = { models: [], selectedModelId: '', defaultModelId: '', loading: false, warning: null }
let loaded = false
const listeners = new Set<() => void>()
const emit = () => listeners.forEach((listener) => listener())
const setState = (next: Partial<State>) => {
  state = { ...state, ...next }
  emit()
}

export function loadModels() {
  if (loaded) return
  loaded = true
  setState({ loading: true })
  void api.listModels().then((catalogue) => {
    const saved = window.localStorage.getItem(KEY) ?? ''
    const ids = new Set(catalogue.models.map((model) => model.id))
    const selected = ids.has(saved) ? saved : catalogue.default_model_id
    window.localStorage.setItem(KEY, selected)
    setState({
      models: catalogue.models,
      selectedModelId: selected,
      defaultModelId: catalogue.default_model_id,
      warning: catalogue.warning ?? null,
      loading: false,
    })
  }).catch(() => setState({
    loading: false,
    warning: '模型列表暂时不可用，将使用部署默认模型。',
  }))
}

export function selectModel(modelId: string) {
  if (!state.models.some((model) => model.id === modelId)) return
  window.localStorage.setItem(KEY, modelId)
  setState({ selectedModelId: modelId })
}

export function selectedModelId() {
  return state.selectedModelId || state.defaultModelId || undefined
}

export function useModelPreference() {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    () => state,
  )
}
