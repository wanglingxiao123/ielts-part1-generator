/**
 * 取这套材料的题目包，并在题目「可能正在路上」时轮询。
 *
 * 与 `useAudioStatus` 同一个形状（自己管定时器、自己退避），不用 react-query：这一页的另一个轮询
 * 已经是这么写的，两套机制并存只会让「为什么这个停了那个没停」变成一个要查两处的问题。
 *
 * **轮询条件是「后端说这件事还会变」，不是「现在没有题」。** 出题在材料之后，一次 invocation 常常
 * 在材料做完之后就被时钟停住——那种情况下题目要等**下一次** invocation，可能是几十分钟以后，浏览器
 * 在这里每 10 秒问一次毫无意义。所以只有 slot 自己报 `state` 还在推进（`material_pending` /
 * `material_done` / `questions_pending`）且没有 checkpoint、请求状态还是 `running` 时才继续问。
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '@/api/endpoints'
import { userMessage } from '@/api/http'
import type { MaterialQuestionsResponse } from '@/contracts/api'

export interface MaterialQuestionsState {
  /** 首次请求还没回来。与「回来了但没有题」是两件事，页面对它们说不同的话。 */
  loading: boolean
  data: MaterialQuestionsResponse | null
  /** 读题目本身失败（存储没配、S3 拒绝）。不是「暂无题目」。 */
  error: string | null
  /** 后端认为这套材料的题目还在生成过程中，页面据此画「出题中」而不是「暂无题目」。 */
  inFlight: boolean
  reload: () => void
}

/** 出题还没走完、还会自己往前动的那几个 slot 状态。 */
const ADVANCING = new Set(['material_pending', 'material_done', 'questions_pending'])

/**
 * 这套材料的题目现在是否还在推进。
 *
 * 判据全部来自后端写下的字段，没有一条是从「有没有题」反推的：checkpoint 意味着这一次已经停了，
 * `resumable` 为假意味着这个 slot 不会再动，`request_status` 不是 `running` 意味着这次请求结束了。
 */
export function isInFlight(res: MaterialQuestionsResponse | null): boolean {
  if (!res || res.questions) return false
  const slot = res.slot
  if (!slot) return false
  if (slot.checkpointed || slot.system_fault || !slot.resumable) return false
  if (res.request_status && res.request_status !== 'running') return false
  return ADVANCING.has(slot.state)
}

export function useMaterialQuestions(
  materialId: string,
  batchId: string | undefined,
  enabled: boolean,
): MaterialQuestionsState {
  const [data, setData] = useState<MaterialQuestionsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [reloadKey, setReloadKey] = useState(0)

  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    if (!enabled || !materialId) return
    let stopped = false
    let attempts = 0
    let timer: number | null = null

    const tick = async () => {
      try {
        const res = await api.materialQuestions(materialId, batchId)
        if (stopped) return
        setData(res)
        setError(null)
        setLoading(false)
        // 有题了就不再问：交付过的包不会变（`_questions/` 只写一次终态）。
        if (!isInFlight(res)) return
      } catch (err) {
        if (stopped) return
        setError(userMessage(err, '题目暂时读取不到，请稍后重试'))
        setLoading(false)
        // 失败之后仍然重试，但只重试有限次——读不到题不该变成一个永久后台请求。
        if (attempts >= 5) return
      }
      attempts += 1
      timer = window.setTimeout(() => void tick(), attempts < 10 ? 4000 : 15_000)
    }
    void tick()
    return () => {
      stopped = true
      if (timer !== null) window.clearTimeout(timer)
    }
  }, [materialId, batchId, enabled, reloadKey])

  return { loading, data, error, inFlight: isInFlight(data), reload }
}
