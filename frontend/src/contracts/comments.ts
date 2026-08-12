export type CommentSeverity = 'critical' | 'major' | 'minor'
export type QuestionCommentStatus = 'open' | 'resolved' | 'needs_material'

export type CommentAnchor =
  | { type: 'question'; index: number }
  | { type: 'turn'; index: number }

export interface MaterialComment {
  id: string
  created_at: string
  anchor: CommentAnchor
  severity: CommentSeverity
  text: string
  version_id?: string
  status?: QuestionCommentStatus
  resolved_by_version_id?: string
  revision_request_id?: string
  resolved_at?: string
}

export interface MaterialCommentsDocument {
  material_id: string
  comments: MaterialComment[]
}

export interface CreateMaterialComment {
  anchor: CommentAnchor
  severity: CommentSeverity
  text: string
  version_id?: string
}
