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

/** 一道题的印刷行：空前文 + 带题号的空格 + 空后文，答案开启时在空格后补一个绿色答案。 */
function FaceLine({ q }: { q: PreviewQuestion }) {
  return (
    <span className="qp-line">
      {q.face.carrier_before && <span>{q.face.carrier_before}</span>}
      <span className="qp-blank">{q.face.blank}</span>
      {q.reveal && <span className="qp-inline-answer">{q.reveal.canonical}</span>}
      {q.face.carrier_after && <span>{q.face.carrier_after}</span>}
    </span>
  )
}

/**
 * form：一行一个 `标签: ______`。
 *
 * 左列走 `structure.row_labels`，右列是印刷行。这两处常常说同一句话（参考包里 row_label 是
 * `Street`，而 `carrier_before` 是 `Street:`），因为行标签本来就是印在那一行开头的字——重复印两遍
 * 会让人以为产物出了错。所以标签与空前文实为同一句时，这一行合成一格只印一次；两者都不删。
 */
function FormLayout({ group }: { group: PreviewGroup }) {
  const labels = group.group.structure.row_labels ?? []
  return (
    <div className="qp-form">
      {group.questions.map((q, i) => {
        const label = labels[i] ?? ''
        const carrier = q.face.carrier_before.trim().replace(/[:：]\s*$/, '')
        const sameThing = label !== '' && carrier === label.trim()
        return (
          <div className="qp-form-row" key={q.number}>
            {sameThing || label === '' ? (
              <div className="qp-form-full">
                <FaceLine q={q} />
              </div>
            ) : (
              <>
                <div className="qp-form-label">{label}</div>
                <div className="qp-form-value">
                  <FaceLine q={q} />
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
 * `hierarchy` 是「按印刷顺序的标题与子标题」，但包里没有说哪道题挂在哪个标题下，所以标题按声明
 * 顺序先印，题目行缩进跟在后面——不猜归属。
 */
function NoteLayout({ group }: { group: PreviewGroup }) {
  const hierarchy = group.group.structure.hierarchy ?? []
  return (
    <div className="qp-note">
      {hierarchy.map((line, i) => (
        <div className="qp-note-head" key={`${line}-${i}`} style={{ paddingLeft: i * 14 }}>
          {line}
        </div>
      ))}
      <ul className="qp-note-list">
        {group.questions.map((q) => (
          <li key={q.number}>
            <FaceLine q={q} />
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * table：列标签作表头，行标签作首列。
 *
 * 表头左上角留空——那一格在真实试卷上就是空的，列标签只管内容列。题面横跨全部内容列，因为包里
 * 没有单元格坐标（见文件顶部第三条）。
 */
function TableLayout({ group }: { group: PreviewGroup }) {
  const rows = group.group.structure.row_labels ?? []
  const cols = group.group.structure.column_labels ?? []
  const span = Math.max(1, cols.length)
  return (
    <table className="qp-table">
      {cols.length > 0 && (
        <thead>
          <tr>
            {rows.length > 0 && <th className="qp-table-corner" />}
            {cols.map((col, i) => (
              <th key={`${col}-${i}`}>{col}</th>
            ))}
          </tr>
        </thead>
      )}
      <tbody>
        {group.questions.map((q, i) => (
          <tr key={q.number}>
            {rows.length > 0 && <th scope="row">{rows[i] ?? ''}</th>}
            <td colSpan={span}>
              <FaceLine q={q} />
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

export function GroupFace({ group }: { group: PreviewGroup }) {
  const layout = group.group.layout
  if (layout === 'note') return <NoteLayout group={group} />
  if (layout === 'table') return <TableLayout group={group} />
  return <FormLayout group={group} />
}
