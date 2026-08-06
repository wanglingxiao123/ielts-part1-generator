/**
 * Question-type fit panel (design.md §3.6) — answers spec §6「题型适配」.
 *
 * item_form and the layout coverage map are redundant by contract. This panel
 * shows WHETHER THEY AGREE rather than picking one: a disagreement means the
 * artefact contradicts itself, which is what the reviewer needs to know.
 */
import type { FormGroupAnalysis } from '@/domain/formGroups'
import { circled, ITEM_FORM_GLYPH } from '@/domain/types'

export function QuestionTypePanel({ analysis }: { analysis: FormGroupAnalysis }) {
  const c = analysis.consistency
  return (
    <div className="panel panel-pad">
      <h3>能出哪些题型</h3>
      <table className="qt-table">
        <thead>
          <tr>
            <th>题型</th>
            <th>可用的信息点</th>
            <th>能否成组出题</th>
          </tr>
        </thead>
        <tbody>
          {analysis.rows.map((row) => {
            const groups = analysis.groups.filter((g) => g.itemForm === row.itemForm)
            return (
              <tr key={row.itemForm}>
                <td>
                  {ITEM_FORM_GLYPH[row.itemForm]} {row.label}
                </td>
                <td>
                  {row.itemFormNumbers.map((n) => circled(n)).join('') || '—'}{' '}
                  {/* 两份声明本该一致；不一致说明产物自相矛盾，得退回重生成。 */}
                  {!row.agrees && (
                    <span className="flag flag-bad" title="两处记录对不上，材料本身自相矛盾">
                      记录矛盾
                    </span>
                  )}
                </td>
                <td>
                  {groups.length === 0 && <span className="muted">—</span>}
                  {groups.map((g) => (
                    <div key={`${g.name ?? 'null'}`}>
                      {g.name ? `${g.name} 组` : '各自独立的点'} · {g.numbers.length} 个
                      {/* Span is only a property of a declared group; showing it
                          for the null bucket reads as a defect that is not one. */}
                      {!g.ungrouped && ` · 前后隔 ${g.turnSpan} 轮`}{' '}
                      {g.spanWarn && <span className="flag flag-warn">隔太远，要跨半篇回忆</span>}
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
          十道题是否齐备：
          {c.coversAllTen ? (
            <span className="flag flag-good">1–10 题各有着落</span>
          ) : (
            <span className="flag flag-bad">
              {c.missingNumbers.length > 0 && `第 ${c.missingNumbers.join('、')} 题没有对应信息点 `}
              {c.duplicateNumbers.length > 0 && `第 ${c.duplicateNumbers.join('、')} 题重复 `}
              {c.extraNumbers.length > 0 && `多出第 ${c.extraNumbers.join('、')} 题`}
            </span>
          )}
        </span>
        {/* 只在自相矛盾时出声：一致是常态，为常态挂一个绿标只是噪音。 */}
        {c.disagreeingNumbers.length > 0 && (
          <span className="flag flag-bad" title="材料对同一个点给了两种题型记录，须退回重新生成">
            第 {c.disagreeingNumbers.join('、')} 题的题型记录自相矛盾
          </span>
        )}
        <span>
          表格/表单题：
          {analysis.hasViableQuestionGroup ? (
            <span className="flag flag-good">可以出</span>
          ) : (
            <span className="flag flag-bad">出不了，同类信息点凑不满 3 个</span>
          )}
        </span>
      </div>
    </div>
  )
}
