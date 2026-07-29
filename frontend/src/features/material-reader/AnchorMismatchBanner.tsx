/**
 * Material-level warning (design.md §2.1).
 *
 * A stale anchor is not repaired by the frontend on purpose: if the UI silently
 * relocated the annotation the defect would never be found, while the backend
 * has already written the bad artefact to S3.
 */
import type { AnchorMismatch } from '@/domain/types'

const REASON_TEXT: Record<AnchorMismatch['reason'], string> = {
  'evidence-not-in-turn': '标注的那句话不在这一轮对话里',
  'turn-out-of-range': '指向的对话轮次不存在',
  'narrator-turn': '指向了旁白，而答案不能出自旁白',
}

export function AnchorMismatchBanner({ mismatches }: { mismatches: AnchorMismatch[] }) {
  return (
    <div className="banner banner-bad">
      <strong>⚠ 本材料旁注可能错位，请勿据此判断</strong>
      <div>
        有 {mismatches.length} 个信息点标错了位置，旁注可能贴在了不相干的句子旁边。
        系统不会自动挪正——挪正就看不出这个毛病了，请退回重新生成：
        <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
          {mismatches.map((m) => (
            <li key={`${m.itemNumber}-${m.reason}`}>
              第 {m.itemNumber} 题（标在 turn {m.turnIndex}）：{REASON_TEXT[m.reason]}
              <div style={{ fontSize: 11, opacity: 0.85 }}>
                应当出现 “{m.evidence}”
                {m.actualTurnText !== null && ` ｜ 这一轮实际是 “${m.actualTurnText.slice(0, 60)}…”`}
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
