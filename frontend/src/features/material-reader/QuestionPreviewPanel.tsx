/**
 * 「题目预览」页签。
 *
 * 这一页要同时是两样东西，而它们的要求是相反的：
 *
 *   · **考生看到的那张纸**——所以题面按真实版式排（form / note / table，见 QuestionLayouts.tsx），
 *     而不是十张一模一样的问答卡片。
 *   · **命题人的复核台**——所以答案、证据原文、轮次编号、题解都要能看到。
 *
 * 「显示答案和证据」这个开关就是这两者之间的切换，内部审核默认开着。关掉之后不是少画几个 div：
 * `buildQuestionPreview` 拿到的 `showAnswers` 为假时根本不把 `answer_key` / `evidence` 挂进返回值
 * （见 domain/questionPreview.ts），所以这个组件手上就没有答案可泄露。blueprint 同理——它只喂给
 * 题解事实的计算，而关掉开关时 `facts` 是空数组。
 *
 * 题解只搬运后端已有的字段。后端没有一段自由文本的「解析」，所以一道题没有任何可搬的事实时，
 * 「查看题解」整块不出现——宁可少一块，也不由前端编一段命题人会当成后端结论去信的话。
 */
import { useMemo, useState } from 'react'
import type { Blueprint, QuestionPackage } from '@/contracts'
import { buildQuestionPreview, dialogueOrdinalOf, LAYOUT_LABEL } from '@/domain/questionPreview'
import type { PreviewGroup, PreviewQuestion } from '@/domain/questionPreview'
import type { ViewMaterial } from '@/domain/types'
import { GroupFace } from './QuestionLayouts'

export interface QuestionPreviewPanelProps {
  pkg: QuestionPackage
  blueprint: Blueprint | null
  /** 用来把 evidence 的 turn_index 翻成「对话第 N 轮」，并支持跳到原文那一句。 */
  view: ViewMaterial | null
  onJump?: (turnIndex: number) => void
}

export function QuestionPreviewPanel({ pkg, blueprint, view, onJump }: QuestionPreviewPanelProps) {
  // 内部审核页面，默认开启。
  const [showAnswers, setShowAnswers] = useState(true)
  const preview = useMemo(
    () => buildQuestionPreview(pkg, blueprint, showAnswers),
    [pkg, blueprint, showAnswers],
  )

  return (
    <div className="qp">
      <div className="qp-bar">
        <label className="qp-toggle">
          <input
            type="checkbox"
            checked={showAnswers}
            onChange={(e) => setShowAnswers(e.target.checked)}
          />
          <span>显示答案和证据</span>
        </label>
        <div className="qp-bar-facts">
          <span className="flag flag-neutral">共 {preview.count} 题</span>
          {preview.layouts.map((layout) => (
            <span className="flag flag-neutral" key={layout}>
              {LAYOUT_LABEL[layout]}
            </span>
          ))}
          {/* 十道题是这一套的定义（schema 要求恰好十道）。少于十说明拿到的包不完整，
              说出来，而不是安静地画九道。 */}
          {preview.count !== 10 && (
            <span className="flag flag-bad" title="题目包应当恰好十道题">
              题目不足十道，这个包不完整
            </span>
          )}
        </div>
        {!showAnswers && (
          <span className="muted qp-blind-note">
            盲看模式：只显示考生可见的题面，答案、证据与题解都不在页面上
          </span>
        )}
      </div>

      {preview.groups.map((group) => (
        <GroupBlock
          key={group.group.group_id}
          group={group}
          view={view}
          onJump={onJump}
          showAnswers={showAnswers}
        />
      ))}
    </div>
  )
}

function GroupBlock({
  group,
  view,
  onJump,
  showAnswers,
}: {
  group: PreviewGroup
  view: ViewMaterial | null
  onJump?: (turnIndex: number) => void
  showAnswers: boolean
}) {
  const instruction = group.instruction
  return (
    <section className="panel panel-pad qp-group">
      <header className="qp-group-head">
        <span className="flag flag-neutral">{LAYOUT_LABEL[group.group.layout]}</span>
        {instruction && <span className="mono muted">Questions {instruction.question_range}</span>}
        {/* 题组落在哪个旁白窗口，是考生被告知「读到这里换一组」的位置，也是跨窗口错误唯一
            看得出来的地方。 */}
        <span className="muted">旁白窗口 {group.group.narrator_window_id}</span>
      </header>

      {/* rubric 逐字照印：印在纸上的那句话才是考生遵守的那一句，字数限制也在里面。 */}
      {instruction && <p className="qp-rubric">{instruction.instruction_text}</p>}

      {group.group.title && <h4 className="qp-title">{group.group.title}</h4>}

      {/* 定位句。不带空格、用来让考生找位置，所以印在题面之上而不是混进题里。 */}
      {group.group.signposts.length > 0 && (
        <ul className="qp-signposts">
          {group.group.signposts.map((line, i) => (
            <li key={`${line}-${i}`}>{line}</li>
          ))}
        </ul>
      )}

      <GroupFace group={group} />

      {showAnswers && (
        <div className="qp-reveals">
          {group.questions.map((q) => (
            <QuestionReveal key={q.number} q={q} view={view} onJump={onJump} />
          ))}
        </div>
      )}
    </section>
  )
}

/**
 * 一道题的答案区：答案 + 证据原文 + 轮次，以及可折叠的题解。
 *
 * 答案在这一页出现两次，是刻意的。题面空格后面那一处（`QuestionLayouts` 的 `FaceLine`）回答的是
 * 「这一行填进去通不通」；这一处回答的是「这句原文撑不撑得起这个答案」——而后者要求答案与原文**紧挨着
 * 一行**读。只留题号不印答案的话，复核的人得把眼睛在表格和这一栏之间来回移十次。
 *
 * 这一块另外放的是题面上放不下的东西：轮次、以及同样算对的写法。
 */
function QuestionReveal({
  q,
  view,
  onJump,
}: {
  q: PreviewQuestion
  view: ViewMaterial | null
  onJump?: (turnIndex: number) => void
}) {
  const [open, setOpen] = useState(false)
  const reveal = q.reveal
  const ordinal = reveal ? dialogueOrdinalOf(view, reveal.turnIndex) : null

  return (
    <div className="qp-reveal">
      <div className="qp-reveal-head">
        <span className="qp-num">{q.number}</span>
        {reveal ? (
          <>
            <span className="qp-answer">{reveal.canonical}</span>
            {reveal.alternatives.length > 0 && (
              <span className="muted">或 {reveal.alternatives.join(' / ')}</span>
            )}
          </>
        ) : (
          // 答案与证据缺了任何一半就都不显示（见 questionPreview.ts）：半个答案比没有更容易误读。
          <span className="flag flag-warn">这一题的答案或证据在包里缺失</span>
        )}
        {q.facts.length > 0 && (
          <button
            type="button"
            className="btn btn-sm qp-more"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            {open ? '收起题解' : '查看题解'}
          </button>
        )}
      </div>

      {reveal && (
        <div className="qp-evidence">
          <span className="qp-quote">“{reveal.quote}”</span>
          {onJump ? (
            <button
              type="button"
              className="qp-turn"
              title="跳到原文这一句"
              onClick={() => onJump(reveal.turnIndex)}
            >
              {ordinal !== null ? `对话第 ${ordinal} 轮` : `第 ${reveal.turnIndex} 轮`}
            </button>
          ) : (
            <span className="qp-turn-static muted">
              {ordinal !== null ? `对话第 ${ordinal} 轮` : `第 ${reveal.turnIndex} 轮`}
            </span>
          )}
        </div>
      )}

      {open && q.facts.length > 0 && (
        <dl className="qp-facts">
          {q.facts.map((fact) => (
            <div className={`qp-fact qp-fact-${fact.tone}`} key={fact.key}>
              <dt>{fact.label}</dt>
              <dd>{fact.text}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  )
}
