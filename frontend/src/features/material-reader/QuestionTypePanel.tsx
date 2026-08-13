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
                {/* row.itemForm 是 CurrentLayout（三值），所以字形表可以直接索引；历史版式没有行，
                    也没有字形——见 domain/types.ts 上那段注释。 */}
                <td>
                  {ITEM_FORM_GLYPH[row.itemForm]} {row.label}
                </td>
                <td>
                  {row.itemFormNumbers.map((n) => circled(n)).join('') || '—'}{' '}
                  {/*
                    两份声明本该一致；不一致说明产物自相矛盾，得退回重生成。
                    版本未知时不出这个标：coverage 读不出来，`agrees` 必然为 false，
                    挂上去就是把「读不懂」说成「材料矛盾」。
                  */}
                  {c.known && !row.agrees && (
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
                      {/* Span is descriptive context for declared groups, not a defect. */}
                      {!g.ungrouped && ` · 前后隔 ${g.turnSpan} 轮`}{' '}
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
          {/*
            未知版本单独说一句，不能落进下面的「没有着落」分支。那个分支会列出第 1…10 题全部缺失
            ——对一个明明声明了十个点、只是本前端读不懂其合同的记录，那是在指控材料有问题，
            而真正该说的是「这个页面太旧」。这里也不猜版本：`known` 由版本字段本身决定。
          */}
          {!c.known ? (
            <span className="flag flag-warn" title="这份记录声明的 blueprint 版本本页面尚不支持，题型信息未做判断">
              版本未知，题型信息暂不解读
            </span>
          ) : c.coversAllTen ? (
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
          成组完成题（Form / Note / Table）：
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
