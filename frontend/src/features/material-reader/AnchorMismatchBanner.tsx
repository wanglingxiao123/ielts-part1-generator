/**
 * Material-level warning (design.md §2.1).
 *
 * A stale anchor is not repaired by the frontend on purpose: if the UI silently
 * relocated the annotation the defect would never be found, while the backend
 * has already written the bad artefact to S3.
 */
import type { AnchorMismatch } from '@/domain/types'

const REASON_TEXT: Record<AnchorMismatch['reason'], string> = {
  'evidence-not-in-turn': 'evidence 不在该 turn 的文本中',
  'turn-out-of-range': 'turn_index 超出 turns 数组范围',
  'narrator-turn': 'turn_index 指向旁白（speaker1），旁白不得承载答案',
}

export function AnchorMismatchBanner({ mismatches }: { mismatches: AnchorMismatch[] }) {
  return (
    <div className="banner banner-bad">
      <strong>⚠ 本材料旁注可能错位，请勿据此判断</strong>
      <div>
        {mismatches.length} 个信息点的锚点与脚本不一致（前端不做自动纠正，以免掩盖缺陷）：
        <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
          {mismatches.map((m) => (
            <li key={`${m.itemNumber}-${m.reason}`}>
              信息点 {m.itemNumber} → turn {m.turnIndex}：{REASON_TEXT[m.reason]}
              <div className="mono" style={{ fontSize: 11, opacity: 0.85 }}>
                期望 “{m.evidence}”
                {m.actualTurnText !== null && ` ｜ 实际 “${m.actualTurnText.slice(0, 60)}…”`}
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
