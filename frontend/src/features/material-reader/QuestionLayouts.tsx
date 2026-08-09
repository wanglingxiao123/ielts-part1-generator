/**
 * 三种真实版式：form、note、table。
 *
 * 为什么不把十道题统一画成问答卡片：`layout` 是题目包里唯一声明一次、由题组自己拥有的字段
 * （见 question_package.schema.json 对两层结构的说明），而版式就是考生看到的那张纸。一个 table 组
 * 摊平成十张卡片以后，「列标签有没有、行标签对不对、空格落在句子哪个位置」这些出题环节真正要复核
 * 的事情就都看不见了——那正是这个页签存在的理由。
 *
 * 三条共同的规矩：
 *
 * **一、题面文字逐字照搬。** `carrier_before` / `blank` / `carrier_after` 原样输出，包括
 * `blank` 里那串点。空格里必须带题号（schema 的硬要求），所以这里不重新编号、不改写标点。
 *
 * **二、结构标签只按声明画，不补。** `structure` 里哪些键存在取决于 layout，由后端校验器判定；
 * 这里缺了就少画一列，不编一个占位标签——一个前端造出来的行标签会被当成后端的产物去复核。
 *
 * **三、题目与结构节点的对应关系，包里没有。** table 只声明行标签与列标签，note 只声明层级顺序，
 * 都没有「第 8 题在哪个单元格」。所以下面按顺序对齐，并且 table 的题面横跨内容列而不是挑一列
 * 放进去——挑一列就是在替后端断言一件它没说的事。
 */
import type { PreviewGroup, PreviewQuestion } from '@/domain/questionPreview'

export interface QuestionInteraction {
  selectedQuestion?: number | null
  commentCounts?: ReadonlyMap<number, number>
  onSelectQuestion?: (questionNumber: number) => void
}

function questionClass(q: PreviewQuestion, interaction: QuestionInteraction): string {
  const count = interaction.commentCounts?.get(q.number) ?? 0
  return (
    ' qp-question-anchor' +
    (count > 0 ? ' has-comments' : '') +
    (interaction.selectedQuestion === q.number ? ' selected' : '')
  )
}

function QuestionCount({
  q,
  interaction,
}: {
  q: PreviewQuestion
  interaction: QuestionInteraction
}) {
  const count = interaction.commentCounts?.get(q.number) ?? 0
  return count > 0 ? <span className="comment-count-badge">{count}</span> : null
}

function AnswerBlank({ text }: { text: string }) {
  const number = text.match(/\d+/)?.[0]

  return (
    <span className="qp-blank" aria-label={number ? `Question ${number} answer blank` : 'Answer blank'}>
      {number && <span className="qp-blank-number">{number}</span>}
      <span className="qp-blank-line" aria-hidden="true" />
    </span>
  )
}

/** 一道题的印刷行：空前文 + 带题号的空格 + 空后文，答案开启时在空格后补一个绿色答案。 */
function FaceLine({ q }: { q: PreviewQuestion }) {
  const before = q.face.carrier_before
  const after = q.face.carrier_after
  const spaceBeforeBlank = before !== '' && !/\s$/.test(before)
  const spaceBeforeAfter = after !== '' && !/^[\s,.;:!?)]/.test(after)

  return (
    <span className="qp-line">
      {before && <span>{before}</span>}
      {spaceBeforeBlank && ' '}
      <AnswerBlank text={q.face.blank} />
      {q.reveal && (
        <>
          {' '}
          <span className="qp-inline-answer">{q.reveal.canonical}</span>
        </>
      )}
      {spaceBeforeAfter && ' '}
      {after && <span>{after}</span>}
    </span>
  )
}

/**
 * form：一行一个 `标签: ______`。
 *
 * 左列走 `structure.row_labels`，右列是印刷行。行标签与空前文各有一份职责、不得重复：标签负责
 * 命名字段，空前文只补这一行在名字之外还需要的东西（单位、限定、句子的其余部分，或者什么都不补）。
 * 这条现在写在出题规则里（question-rules.md §4「Row label and carrier: one job each」），也是审核
 * Agent 的第 12 项判断。
 *
 * 所以这里**照印**，不再把重复的两格合成一格。早先的合并是想让页面好看一点，代价是把
 * `row_label: Street` + `carrier_before: "Street:"` 这种重复藏了起来——审核要读的正是「这一行印出来
 * 长什么样」，前端替它去重，等于替产物打了个补丁，然后让人在补丁上做复核。没有标签的行仍然整行铺满。
 */
function FormLayout({
  group,
  interaction,
}: {
  group: PreviewGroup
  interaction: QuestionInteraction
}) {
  const labels = group.group.structure.row_labels ?? []
  return (
    <div className="qp-form">
      {group.questions.map((q, i) => {
        const label = labels[i] ?? ''
        return (
          <div
            className={`qp-form-row${questionClass(q, interaction)}`}
            key={q.number}
            data-question={q.number}
            role="button"
            tabIndex={0}
            onClick={() => interaction.onSelectQuestion?.(q.number)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault()
                interaction.onSelectQuestion?.(q.number)
              }
            }}
          >
            {label === '' ? (
              <div className="qp-form-full">
                <FaceLine q={q} />
                <QuestionCount q={q} interaction={interaction} />
              </div>
            ) : (
              <>
                <div className="qp-form-label">{label}</div>
                <div className="qp-form-value">
                  <FaceLine q={q} />
                  <QuestionCount q={q} interaction={interaction} />
                </div>
              </>
            )}
          </div>
        )
      })}
      {/* 声明了却没有题落在上面的行标签照样印出来：真实试卷上的 form 常有已填好的行，
          而悄悄丢掉它们会让这一组看起来比实际更短。 */}
      {labels.slice(group.questions.length).map((label) => (
        <div className="qp-form-row" key={`extra-${label}`}>
          <div className="qp-form-label">{label}</div>
          <div className="qp-form-value muted">—</div>
        </div>
      ))}
    </div>
  )
}

/**
 * note：层级标题 + 其下的笔记行。
 *
 * 新包用 `note_sections[].question_numbers` 明确题目归属。旧包只有 `hierarchy`、无法证明归属时，
 * 宁可退化成一个普通项目列表，也不把标题按数组下标硬配给错误的题目。
 */
function NoteLayout({
  group,
  interaction,
}: {
  group: PreviewGroup
  interaction: QuestionInteraction
}) {
  const byNumber = new Map(group.questions.map((q) => [q.number, q]))
  const declared = group.group.structure.note_sections ?? []
  const sections =
    declared.length > 0
      ? declared.map((section) => ({
          heading: section.heading,
          questions: section.question_numbers.flatMap((number) => {
            const question = byNumber.get(number)
            return question ? [question] : []
          }),
        }))
      : [{ heading: undefined, questions: group.questions }]

  return (
    <div className="qp-note">
      {sections.map((section, i) => (
        <div className="qp-note-section" key={`${section.heading ?? 'items'}-${i}`}>
          {section.heading && <div className="qp-note-head">{section.heading}</div>}
          <ul className="qp-note-list">
            {section.questions.map((q) => (
              <li
                key={q.number}
                className={questionClass(q, interaction)}
                data-question={q.number}
                role="button"
                tabIndex={0}
                onClick={() => interaction.onSelectQuestion?.(q.number)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    interaction.onSelectQuestion?.(q.number)
                  }
                }}
              >
                <FaceLine q={q} />
                <QuestionCount q={q} interaction={interaction} />
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}

/**
 * table：列标签作表头，行标签作首列。
 *
 * 表头左上角留空——那一格在真实试卷上就是空的，列标签只管内容列。题面横跨全部内容列，因为包里
 * 没有单元格坐标（见文件顶部第三条）。
 */
function TableLayout({
  group,
  interaction,
}: {
  group: PreviewGroup
  interaction: QuestionInteraction
}) {
  const rowHeader = group.group.structure.row_header_label
  const rows = group.group.structure.row_labels ?? []
  const cols = group.group.structure.column_labels ?? []
  const span = Math.max(1, cols.length)
  return (
    <table className="qp-table">
      {cols.length > 0 && (
        <thead>
          <tr>
            {rows.length > 0 && <th className="qp-table-corner">{rowHeader ?? ''}</th>}
            {cols.map((col, i) => (
              <th key={`${col}-${i}`}>{col}</th>
            ))}
          </tr>
        </thead>
      )}
      <tbody>
        {group.questions.map((q, i) => (
          <tr
            key={q.number}
            className={questionClass(q, interaction)}
            data-question={q.number}
            tabIndex={0}
            onClick={() => interaction.onSelectQuestion?.(q.number)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault()
                interaction.onSelectQuestion?.(q.number)
              }
            }}
          >
            {rows.length > 0 && <th scope="row">{rows[i] ?? ''}</th>}
            <td colSpan={span}>
              <FaceLine q={q} />
              <QuestionCount q={q} interaction={interaction} />
            </td>
          </tr>
        ))}
        {rows.slice(group.questions.length).map((label) => (
          <tr key={`extra-${label}`}>
            <th scope="row">{label}</th>
            <td colSpan={span} className="muted">
              —
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function GroupFace({
  group,
  ...interaction
}: { group: PreviewGroup } & QuestionInteraction) {
  const layout = group.group.layout
  if (layout === 'note') return <NoteLayout group={group} interaction={interaction} />
  if (layout === 'table') return <TableLayout group={group} interaction={interaction} />
  return <FormLayout group={group} interaction={interaction} />
}
