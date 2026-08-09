import { useCallback, useEffect, useState } from 'react'
import { api } from '@/api/endpoints'
import { userMessage } from '@/api/http'
import type {
  CreateMaterialComment,
  MaterialComment,
} from '@/contracts/comments'

export function useMaterialComments(materialId: string, enabled: boolean) {
  const [comments, setComments] = useState<MaterialComment[]>([])
  const [commentsMaterialId, setCommentsMaterialId] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    if (!enabled || !materialId) return
    let active = true
    setLoading(true)
    setError(null)
    void api
      .materialComments(materialId)
      .then((document) => {
        if (active) {
          setComments(document.comments)
          setCommentsMaterialId(document.material_id)
        }
      })
      .catch((err) => {
        if (active) setError(userMessage(err, '评论暂时读取不到，请稍后重试。'))
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [enabled, materialId, reloadKey])

  const create = useCallback(
    async (comment: CreateMaterialComment) => {
      setSaving(true)
      setError(null)
      try {
        const document = await api.createMaterialComment(materialId, comment)
        setComments(document.comments)
        setCommentsMaterialId(document.material_id)
        return true
      } catch (err) {
        setError(userMessage(err, '评论没有保存成功，请稍后重试。'))
        return false
      } finally {
        setSaving(false)
      }
    },
    [materialId],
  )

  const remove = useCallback(
    async (commentId: string) => {
      setSaving(true)
      setError(null)
      try {
        const document = await api.deleteMaterialComment(materialId, commentId)
        setComments(document.comments)
        setCommentsMaterialId(document.material_id)
      } catch (err) {
        setError(userMessage(err, '评论没有删除成功，请稍后重试。'))
      } finally {
        setSaving(false)
      }
    },
    [materialId],
  )

  return {
    comments: commentsMaterialId === materialId ? comments : [],
    loading,
    saving,
    error,
    create,
    remove,
    reload: () => setReloadKey((key) => key + 1),
  }
}
