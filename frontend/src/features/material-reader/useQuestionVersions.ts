import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '@/api/endpoints'
import { userMessage } from '@/api/http'
import {
  streamMaterialRevision,
  streamQuestionReplan,
  streamQuestionRevision,
} from '@/api/questionRevisions'
import type { MaterialComment } from '@/contracts/comments'
import type {
  MaterialQuestionVersionsResponse,
  MaterialRevisionReason,
  QuestionPackageVersion,
  QuestionRevisionRecord,
  QuestionRevisionEvent,
  QuestionRevisionStage,
  QuestionRevisionTerminalEvent,
} from '@/contracts/questionVersions'

export type RevisionResult =
  | {
      kind: 'revised'
      versionId: string
      baselineAdvisories: string[]
    }
  | { kind: 'needs_material'; reasons: MaterialRevisionReason[] }
  | { kind: 'no_change'; reasons: MaterialRevisionReason[] }
  | { kind: 'needs_replan'; reasons: MaterialRevisionReason[] }
  | { kind: 'failed'; message: string; blockers: string[] }
  | null

const REVISION_RECOVERY_DELAYS_MS = [0, 250, 750] as const
type RevisionStarter = (
  onEvent: (event: QuestionRevisionEvent) => void,
  signal: AbortSignal,
) => Promise<QuestionRevisionTerminalEvent>

export function useQuestionVersions(materialId: string, enabled: boolean) {
  const [versions, setVersions] = useState<QuestionPackageVersion[]>([])
  const [activeVersionId, setActiveVersionId] = useState('')
  const [selectedVersionId, setSelectedVersionId] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [adopting, setAdopting] = useState(false)
  const [revisionStage, setRevisionStage] = useState<QuestionRevisionStage | null>(null)
  const [revisionResult, setRevisionResult] = useState<RevisionResult>(null)
  const [revisionRequest, setRevisionRequest] =
    useState<MaterialQuestionVersionsResponse['revision_request']>(null)
  const abortRef = useRef<AbortController | null>(null)
  const revisionAttemptRef = useRef(0)
  const revisionInFlightRef = useRef(false)

  const load = useCallback(async (preferredVersionId?: string) => {
    setLoading(true)
    setError(null)
    try {
      const response = await api.materialQuestionVersions(materialId)
      setVersions(response.versions)
      const nextActiveId = response.active_version_id ?? ''
      setActiveVersionId(nextActiveId)
      setSelectedVersionId((current) => {
        const preferred = preferredVersionId ?? current
        return response.versions.some((version) => version.id === preferred)
          ? preferred
          : nextActiveId
      })
      const request: QuestionRevisionRecord | null =
        response.revision_request ??
        (response.running_request
          ? { ...response.running_request, status: 'running' }
          : null)
      setRevisionRequest(request)
      const dismissed = window.localStorage.getItem(
        `question-revision-dismissed:${materialId}`,
      )
      if (request?.status === 'running') {
        setRevisionStage(request.stage ?? 'queued')
        setRevisionResult(null)
      } else {
        setRevisionStage(null)
        if (request && request.request_id !== dismissed) {
          if (request.status === 'completed' && request.version_id) {
            setRevisionResult({
              kind: 'revised',
              versionId: request.version_id,
              baselineAdvisories: request.baseline_advisories ?? [],
            })
          } else if (request.status === 'needs_material_revision') {
            setRevisionResult({
              kind: 'needs_material',
              reasons: request.reasons ?? [],
            })
          } else if (request.status === 'no_change') {
            setRevisionResult({ kind: 'no_change', reasons: request.reasons ?? [] })
          } else if (request.status === 'replan_questions') {
            setRevisionResult({ kind: 'needs_replan', reasons: request.reasons ?? [] })
          } else if (request.status === 'failed') {
            setRevisionResult({
              kind: 'failed',
              message: request.message ?? '题目修改没有完成，原版本未受影响',
              blockers: request.blockers ?? [],
            })
          }
        }
      }
      return response
    } catch (err) {
      setError(userMessage(err, '题目版本暂时读取不到，请稍后重试'))
      return null
    } finally {
      setLoading(false)
    }
  }, [materialId])

  useEffect(() => {
    if (!enabled || !materialId) return
    void load()
    return () => {
      revisionAttemptRef.current += 1
      abortRef.current?.abort()
      revisionInFlightRef.current = false
    }
  }, [enabled, materialId, load])

  useEffect(() => {
    if (!revisionStage || !enabled || !materialId) return
    const timer = window.setInterval(() => void load(), 5000)
    return () => window.clearInterval(timer)
  }, [enabled, load, materialId, revisionStage])

  const selectedVersion = useMemo(
    () => versions.find((version) => version.id === selectedVersionId) ?? null,
    [selectedVersionId, versions],
  )
  const adopt = useCallback(async () => {
    if (!selectedVersion || selectedVersion.id === activeVersionId) return
    setAdopting(true)
    setError(null)
    try {
      const response = await api.adoptQuestionVersion(materialId, selectedVersion.id)
      const nextActiveId = response.active_version_id ?? ''
      setActiveVersionId(nextActiveId)
      setVersions((current) =>
        current.map((version) => ({
          ...version,
          is_active: version.id === nextActiveId,
        })),
      )
    } catch (err) {
      setError(userMessage(err, '这个题目版本没有采用成功，请稍后重试'))
    } finally {
      setAdopting(false)
    }
  }, [activeVersionId, materialId, selectedVersion])

  const runRevision = useCallback(async (
    start: RevisionStarter,
    fallbackMessage: string,
  ) => {
    if (revisionInFlightRef.current) return
    revisionInFlightRef.current = true
    const abort = new AbortController()
    const attempt = revisionAttemptRef.current + 1
    revisionAttemptRef.current = attempt
    let keepQueued = false
    let requestId = ''
    abortRef.current = abort
    setRevisionResult(null)
    setError(null)
    setRevisionStage('queued')
    const recoverDurableResult = async () => {
      let stillRunning = false
      for (const delay of REVISION_RECOVERY_DELAYS_MS) {
        if (delay > 0) {
          await new Promise<void>((resolve) => window.setTimeout(resolve, delay))
        }
        if (abort.signal.aborted || revisionAttemptRef.current !== attempt) {
          return 'aborted' as const
        }
        let refreshed: MaterialQuestionVersionsResponse
        try {
          refreshed = await api.materialQuestionVersions(materialId)
        } catch {
          continue
        }
        const durableRequest =
          refreshed?.revision_request ??
          (refreshed?.running_request
            ? { ...refreshed.running_request, status: 'running' as const }
            : null)
        if (requestId && durableRequest?.request_id !== requestId) continue
        if (durableRequest?.status === 'running') {
          stillRunning = true
          continue
        }
        if (durableRequest) {
          await load()
          return 'terminal' as const
        }
      }
      return stillRunning ? 'running' as const : 'missing' as const
    }
    try {
      const terminal = await start(
        (event) => {
          if (event.request_id) requestId = event.request_id
          if (event.event === 'progress') setRevisionStage(event.stage)
        },
        abort.signal,
      )
      if (terminal.request_id) requestId = terminal.request_id
      if (terminal.event === 'revised') {
        setRevisionResult({
          kind: 'revised',
          versionId: terminal.version_id,
          baselineAdvisories: terminal.baseline_advisories ?? [],
        })
        await load(terminal.version_id)
      } else if (terminal.event === 'needs_material_revision') {
        setRevisionResult({ kind: 'needs_material', reasons: terminal.reasons })
        await load()
      } else if (terminal.event === 'no_change') {
        setRevisionResult({ kind: 'no_change', reasons: terminal.reasons })
        await load()
      } else if (terminal.event === 'needs_replan') {
        setRevisionResult({ kind: 'needs_replan', reasons: terminal.reasons })
        await load()
      } else {
        const recovery = await recoverDurableResult()
        keepQueued = recovery === 'running'
        if (recovery === 'missing') {
          setRevisionResult({ kind: 'failed', message: terminal.message, blockers: [] })
        }
      }
    } catch (err) {
      if (!abort.signal.aborted) {
        const recovery = await recoverDurableResult()
        keepQueued = recovery === 'running'
        if (recovery === 'missing') {
          setRevisionResult({
            kind: 'failed',
            message: userMessage(err, fallbackMessage),
            blockers: [],
          })
        }
      }
    } finally {
      if (revisionAttemptRef.current === attempt) {
        setRevisionStage(keepQueued ? 'queued' : null)
      }
      if (abortRef.current === abort) {
        abortRef.current = null
        revisionInFlightRef.current = false
      }
    }
  }, [load, materialId])

  const revise = useCallback(async (comments: MaterialComment[]) => {
    if (!selectedVersion || selectedVersion.id !== activeVersionId || revisionStage) return
    const questionCommentIds = comments
      .filter((comment) => comment.anchor.type === 'question')
      .map((comment) => comment.id)
    if (questionCommentIds.length === 0) return
    await runRevision(
      (onEvent, signal) => streamQuestionRevision(
        materialId,
        { base_version_id: activeVersionId, comment_ids: questionCommentIds },
        onEvent,
        signal,
      ),
      '题目没有修改成功，原版本未受影响',
    )
  }, [
    activeVersionId,
    materialId,
    revisionStage,
    runRevision,
    selectedVersion,
  ])

  const replan = useCallback(async () => {
    const sourceRequestId =
      revisionRequest?.status === 'replan_questions'
        ? revisionRequest.request_id
        : revisionRequest?.status === 'failed' &&
            revisionRequest.operation === 'replan_questions'
          ? revisionRequest.source_request_id
          : undefined
    if (
      revisionStage ||
      !sourceRequestId ||
      !selectedVersion ||
      selectedVersion.id !== activeVersionId ||
      revisionRequest?.base_version_id !== activeVersionId
    ) {
      return
    }
    await runRevision(
      (onEvent, signal) => streamQuestionReplan(
        materialId,
        { source_request_id: sourceRequestId },
        onEvent,
        signal,
      ),
      '重新命题没有完成，现有版本未受影响',
    )
  }, [
    activeVersionId,
    materialId,
    revisionRequest,
    revisionStage,
    runRevision,
    selectedVersion,
  ])

  const reviseMaterial = useCallback(async () => {
    const sourceRequestId =
      revisionRequest?.status === 'needs_material_revision'
        ? revisionRequest.request_id
        : revisionRequest?.status === 'failed' &&
            revisionRequest.operation === 'revise_material'
          ? revisionRequest.source_request_id
          : undefined
    if (
      revisionStage ||
      !sourceRequestId ||
      !selectedVersion ||
      selectedVersion.id !== activeVersionId ||
      revisionRequest?.base_version_id !== activeVersionId
    ) {
      return
    }
    await runRevision(
      (onEvent, signal) =>
        streamMaterialRevision(
          materialId,
          { source_request_id: sourceRequestId },
          onEvent,
          signal,
        ),
      '材料修改没有完成，现有版本未受影响',
    )
  }, [
    activeVersionId,
    materialId,
    revisionRequest,
    revisionStage,
    runRevision,
    selectedVersion,
  ])

  const dismissRevisionResult = useCallback(() => {
    if (revisionRequest?.request_id) {
      window.localStorage.setItem(
        `question-revision-dismissed:${materialId}`,
        revisionRequest.request_id,
      )
    }
    setRevisionResult(null)
  }, [materialId, revisionRequest?.request_id])

  return {
    versions,
    activeVersionId,
    selectedVersion,
    selectedVersionId,
    setSelectedVersionId,
    loading,
    error,
    adopting,
    adopt,
    revisionStage,
    revisionRequest,
    revisionResult,
    dismissRevisionResult,
    revise,
    replan,
    reviseMaterial,
    reload: () => void load(),
  }
}

export type QuestionVersionsState = ReturnType<typeof useQuestionVersions>
