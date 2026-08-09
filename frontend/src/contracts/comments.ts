export type CommentSeverity = 'critical' | 'major' | 'minor'

export type CommentAnchor =
  | { type: 'question'; index: number }
  | { type: 'turn'; index: number }

export interface MaterialComment {
  id: string
  created_at: string
  anchor: CommentAnchor
  severity: CommentSeverity
  text: string
}

export interface MaterialCommentsDocument {
  material_id: string
  comments: MaterialComment[]
}

export interface CreateMaterialComment {
  anchor: CommentAnchor
  severity: CommentSeverity
  text: string
}
