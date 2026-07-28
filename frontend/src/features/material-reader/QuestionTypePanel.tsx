/**
 * Question-type fit panel (design.md §3.6) — answers spec §6「题型适配」.
 *
 * item_form and question_type_coverage are redundant by contract. This panel
 * shows WHETHER THEY AGREE rather than picking one: a disagreement means the
 * artefact contradicts itself, which is what the reviewer needs to know.
 */
import type { FormGroupAnalysis } from '@/domain/formGroups'
import { circled, ITEM_FORM_GLYPH } from '@/domain/types'

export function QuestionTypePanel({ analysis }: { analysis: FormGroupAnalysis }) {
  const c = analysis.consistency
  return (
    <div className="panel panel-pad">
      <h3>题型适配（question_type_coverage）</h3>
      <table className="qt-table">
        <thead>
          <tr>
            <th>题型</th>
            <th>coverage 声明</th>
            <th>item_form 实际</th>
            <th>分组</th>
          </tr>
        </thead>
        <tbody>
          {analysis.rows.map((row) => {
            const groups = analysis.groups.filter((g) => g.itemForm === row.itemForm)
            return (
              <tr key={row.itemForm}>
                <td>
                  {ITEM_FORM_GLYPH[row.itemForm]} {row.label}
                  <div className="muted mono" style={{ fontSize: 10 }}>
                    {row.itemForm}
                  </div>
                </td>
                <td className="mono">
                  {row.coverageNumbers.map((n) => circled(n)).join('') || '—'}
                </td>
                <td className="mono">
                  {row.itemFormNumbers.map((n) => circled(n)).join('') || '—'}{' '}
                  {row.agrees ? (
                    <span className="flag flag-good">一致</span>
                  ) : (
                    <span className="flag flag-bad">不一致</span>
                  )}
                </td>
                <td>
                  {groups.length === 0 && <span className="muted">—</span>}
                  {groups.map((g) => (
                    <div key={`${g.name ?? 'null'}`}>
                      {g.name ? `组 ${g.name}` : '未分组 (form_group=null)'} · {g.numbers.length} 点
                      {/* Span is only a property of a declared group; showing it
                          for the null bucket reads as a defect that is not one. */}
                      {!g.ungrouped && ` · 跨度 ${g.turnSpan}`}{' '}
                      {g.spanWarn && <span className="flag flag-warn">跨度过大</span>}
                      {g.canFormQuestion ? (
                        <span className="flag flag-good">可成题</span>
                      ) : (
                        !g.ungrouped &&
                        g.numbers.length > 1 && (
                          <span className="flag flag-neutral">不足以单独成题</span>
                        )
                      )}
                    </div>
                  ))}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>

      <div className="row" style={{ marginTop: 8, fontSize: 12 }}>
        <span>
          覆盖校验：
          {c.coversAllTen ? (
            <span className="flag flag-good">1–10 全覆盖、不重不漏</span>
          ) : (
            <span className="flag flag-bad">
              {c.missingNumbers.length > 0 && `缺 ${c.missingNumbers.join(',')} `}
              {c.duplicateNumbers.length > 0 && `重复 ${c.duplicateNumbers.join(',')} `}
              {c.extraNumbers.length > 0 && `多出 ${c.extraNumbers.join(',')}`}
            </span>
          )}
        </span>
        <span>
          两视图一致性：
          {c.disagreeingNumbers.length === 0 ? (
            <span className="flag flag-good">item_form 与 coverage 一致</span>
          ) : (
            <span className="flag flag-bad">
              信息点 {c.disagreeingNumbers.join(', ')} 的 item_form 与 coverage 矛盾
            </span>
          )}
        </span>
        <span>
          可成题分组：
          {analysis.hasViableQuestionGroup ? (
            <span className="flag flag-good">存在 ≥3 点同构表格/表单组</span>
          ) : (
            <span className="flag flag-bad">无可成题的表格/表单组</span>
          )}
        </span>
        <span>
          多选点数 <strong className="mono">{analysis.multipleChoiceCount}</strong>
          {analysis.multipleChoiceCount >= 2 ? (
            <span className="flag flag-good">可做多选</span>
          ) : (
            <span className="flag flag-warn">不足 2 点</span>
          )}
        </span>
      </div>
    </div>
  )
}
