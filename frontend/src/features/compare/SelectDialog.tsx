import { useState } from 'react'
import type { MaterialRecord } from '@/contracts/api'

/**
 * Second confirmation: selection is irreversible (prd R5).
 *
 * 它是否**计费**取决于这一套有没有先在阅读页试听过：试听（`preview_audio`）和选定共用同一份
 * clip，所以已试听的材料在选定时一次 Polly 都不会调。`alreadySynthesised` 让这段文案说实话。
 */
export function SelectDialog({
  record,
  alreadySynthesised,
  onCancel,
  onConfirm,
}: {
  record: MaterialRecord
  /**
   * 这一套的音频是否已经存在（在阅读页点过「生成音频」，或之前选定过）。
   * `null` = 还在查。这一栏在查清之前不出文案，见下方注释。
   */
  alreadySynthesised: boolean | null
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
            {/* 「会产生费用」只在这一套还没有音频时才是真的。已经在阅读页点过「生成音频」的材料，
                音频与选定共用同一份 clip，选定时后端一次 Polly 都不会调——那种情况下把它说成
                「产生费用」，等于劝人别做一件其实免费的事。

                查清之前这一栏只说「正在确认」，不先摆一句可能是假的费用警告：这是唯一一条会影响
                人「要不要按下去」的信息，闪一下再改口比晚一拍出现更糟。 */}
            {alreadySynthesised === null ? (
              <li className="muted">正在确认这一套是否已经有音频…</li>
            ) : alreadySynthesised ? (
              <li>
                这一套的语音<strong>已经生成好了</strong>（{turns} 段），选定时直接沿用，
                不会重新合成、不产生新的费用。
              </li>
            ) : (
              <li>
                将对 <strong>{turns}</strong> 个 turn 逐段调用 Polly 合成语音，
                <strong>产生费用</strong>。
              </li>
            )}
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
          {/* 音频状态未知时也禁用：确认框此刻还说不清这一步要不要花钱，而「我已确认以上内容」
              指的就是那句话。 */}
          <button
            type="button"
            className="btn btn-danger"
            disabled={!ack || alreadySynthesised === null}
            onClick={onConfirm}
          >
            {alreadySynthesised ? '确认选定' : '确认选定并合成语音'}
          </button>
        </footer>
      </div>
    </div>
  )
}
