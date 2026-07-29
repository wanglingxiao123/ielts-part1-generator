/**
 * 考点小结面板。
 *
 * 客户要的东西：「把『拼读、先说后改、同义替换』这些考点抽取出来，然后用你页面的高亮块标注」。
 * 所以这里只有考点，没有评价流程的产物——评价方给的 finding 列表、校验器的原始提示
 * （`dialogue words outside preferred 600-650: 559`）、盲评的「计划 10 个，听出 10 个」都不在
 * 这一页上了：命题人读它们得不到任何该做的动作。
 *
 * 每一块的点号都是按钮：点一下跳到原文那一句。考点小结的用处就是「从考点直接看到句子」，一个
 * 不能跳的点号只是个数字。
 */
import type { ExamPointSummary } from '@/domain/examPoints'
import { circled } from '@/domain/types'
import { CONTENT_RULES } from '@/domain/pointFacts'

interface Props {
  summary: ExamPointSummary
  onJump: (turnIndex: number) => void
}

const TONE_CLASS: Record<ExamPointSummary['blocks'][number]['tone'], string> = {
  good: 'ep-block',
  warn: 'ep-block warn',
  bad: 'ep-block bad',
}

export function ExamPointPanel({ summary, onJump }: Props) {
  return (
    <div className="panel panel-pad exam-points">
      <h3>考点小结</h3>
      <div className="ep-headline">{summary.headline}</div>

      <div className="ep-blocks">
        {summary.blocks.map((b) => (
          <div key={b.key} className={TONE_CLASS[b.tone]} title={b.hint}>
            <span className="ep-label">{b.label}</span>
            <span className="ep-nums">
              {b.numbers.map((n) => (
                <button
                  key={n}
                  type="button"
                  className="ep-num"
                  title={`第 ${n} 题的信息在 turn ${b.turnOf[n]}，点击跳到原文`}
                  onClick={() => onJump(b.turnOf[n] ?? 0)}
                >
                  {circled(n)}
                </button>
              ))}
            </span>
          </div>
        ))}
      </div>

      {/* 八类信息点的覆盖情况（规范 §4B-3）。少于四类是规范自己写明的下限，不是我们造的阈值。 */}
      <div className="ep-types">
        <div className="ep-types-head">
          信息点类型
          <span className={summary.typeKindCount >= CONTENT_RULES.MIN_TYPE_KINDS ? 'muted' : 'ep-thin'}>
            {summary.typeKindCount >= CONTENT_RULES.MIN_TYPE_KINDS
              ? `覆盖 ${summary.typeKindCount} 类`
              : `只覆盖 ${summary.typeKindCount} 类，规范要求至少 ${CONTENT_RULES.MIN_TYPE_KINDS} 类`}
          </span>
        </div>
        <div className="ep-type-rows">
          {summary.typeCoverage.map((row) => (
            <span key={row.type} className="ep-type">
              <span className="ep-type-label">{row.label}</span>
              {row.numbers.map((n) => (
                <button
                  key={n}
                  type="button"
                  className="ep-num"
                  title={`第 ${n} 题的信息在 turn ${summary.turnOf[n]}，点击跳到原文`}
                  onClick={() => onJump(summary.turnOf[n] ?? 0)}
                >
                  {circled(n)}
                </button>
              ))}
            </span>
          ))}
        </div>
      </div>
    </div>
  )
}
