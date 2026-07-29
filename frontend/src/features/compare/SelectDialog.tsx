import { useState } from 'react'
import type { MaterialRecord } from '@/contracts/api'

/** Second confirmation: selection is irreversible and bills Polly (prd R5). */
export function SelectDialog({
  record,
  onCancel,
  onConfirm,
}: {
  record: MaterialRecord
  onCancel: () => void
  onConfirm: () => void
}) {
  const [ack, setAck] = useState(false)
  const turns = record.material.listening_material_parts[0].script.turns.length

  return (
    <div className="dialog-backdrop" onClick={onCancel}>
      <div className="dialog" onClick={(e) => e.stopPropagation()}>
        <h2>确认选定这一套？</h2>
        <div style={{ fontSize: 13 }}>
          {/* No verdict enum (see DecisionBar); the rejection, if any, is stated
              as a sentence below — which is the form a reviewer about to spend
              money on this material can actually act on. */}
          <div className="row">
            <span className="mono">{record.material_id}</span>
            <span>{record.audit.score.total} 分</span>
          </div>
          {record.audit_rejection && (
            <div className="banner banner-warn" style={{ marginTop: 8 }}>
              <div>{record.audit_rejection.message}。仍可选定，建议先通读全文确认。</div>
            </div>
          )}
          <ul style={{ margin: '10px 0 0 18px', padding: 0 }}>
            <li>
              将对 <strong>{turns}</strong> 个 turn 逐段调用 Polly 合成语音，
              <strong>产生费用</strong>。
            </li>
            <li>
              <strong>不可撤销</strong>：同场景的其他候选将被标注为弃用。
            </li>
            <li>后端选定接口是幂等的，重复提交不会重复计费。</li>
          </ul>
          <label style={{ display: 'block', marginTop: 12 }}>
            <input type="checkbox" checked={ack} onChange={() => setAck((v) => !v)} /> 我已确认以上内容
          </label>
        </div>
        <footer>
          <button type="button" className="btn" onClick={onCancel}>
            取消
          </button>
          <button type="button" className="btn btn-danger" disabled={!ack} onClick={onConfirm}>
            确认选定并合成语音
          </button>
        </footer>
      </div>
    </div>
  )
}
