import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '@/api/endpoints'
import { userMessage } from '@/api/http'
import { streamQuestionRevision } from '@/api/questionRevisions'
import type { MaterialComment } from '@/contracts/comments'
import type {
  MaterialRevisionReason,
  QuestionPackageVersion,
  QuestionRevisionStage,
} from '@/contracts/questionVersions'

export type RevisionResult =
  | { kind: 'needs_material'; reasons: MaterialRevisionReason[] }
  | { kind: 'failed'; message: string }
  | null

export function useQuestionVersions(materialId: string, enabled: boolean) {
  const [versions, setVersions] = useState<QuestionPackageVersion[]>([])
  const [activeVersionId, setActiveVersionId] = useState('')
  const [selectedVersionId, setSelectedVersionId] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [adopting, setAdopting] = useState(false)
  const [revisionStage, setRevisionStage] = useState<QuestionRevisionStage | null>(null)
  const [revisionResult, setRevisionResult] = useState<RevisionResult>(null)
  const abortRef = useRef<AbortController | null>(null)

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
      if (response.running_request) {
        setRevisionStage((current) => current ?? 'queued')
      } else {
        setRevisionStage((current) => current === 'queued' ? null : current)
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
    return () => abortRef.current?.abort()
  }, [enabled, materialId, load])

  useEffect(() => {
    if (revisionStage !== 'queued' || !enabled || !materialId) return
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

  const revise = useCallback(async (comments: MaterialComment[]) => {
    if (!selectedVersion || selectedVersion.id !== activeVersionId || revisionStage) return
    const questionCommentIds = comments
      .filter((comment) => comment.anchor.type === 'question')
      .map((comment) => comment.id)
    if (questionCommentIds.length === 0) return

    const abort = new AbortController()
    let keepQueued = false
    abortRef.current = abort
    setRevisionResult(null)
    setError(null)
    setRevisionStage('queued')
    try {
      const terminal = await streamQuestionRevision(
        materialId,
        { base_version_id: activeVersionId, comment_ids: questionCommentIds },
        (event) => {
          if (event.event === 'progress') setRevisionStage(event.stage)
        },
        abort.signal,
      )
      if (terminal.event === 'revised') {
        await load(terminal.version_id)
      } else if (terminal.event === 'needs_material_revision') {
        setRevisionResult({ kind: 'needs_material', reasons: terminal.reasons })
      } else {
        setRevisionResult({ kind: 'failed', message: terminal.message })
        const refreshed = await load()
        keepQueued = Boolean(refreshed?.running_request)
      }
    } catch (err) {
      if (!abort.signal.aborted) {
        setRevisionResult({
          kind: 'failed',
          message: userMessage(err, '题目没有修改成功，原版本未受影响'),
        })
        const refreshed = await load()
        keepQueued = Boolean(refreshed?.running_request)
      }
    } finally {
      setRevisionStage(keepQueued ? 'queued' : null)
      if (abortRef.current === abort) abortRef.current = null
    }
  }, [activeVersionId, load, materialId, revisionStage, selectedVersion])

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
    revisionResult,
    revise,
    reload: () => void load(),
  }
}

export type QuestionVersionsState = ReturnType<typeof useQuestionVersions>
