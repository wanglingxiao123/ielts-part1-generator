export interface BatchRecoveryPlan {
  batch_id: string
  status: 'available' | 'running' | 'readonly' | 'complete'
  requested: number
  delivered: number
  targets: Array<{ slot_id: string; scenario_key: string; index: number; action: 'resume' | 'restart' | 'retry' | 'blocked' | 'wait'; reason?: string }>
}
