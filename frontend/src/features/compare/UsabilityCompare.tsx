/**
 * 两套候选的「哪套更好出题、为什么」对照。
 *
 * 取代原先的「指标对照（domain/distribution.ts 的确定性输出）」表：那张表的行是
 * 度量名（间隔序列 / 间隔 CV / 均匀度），列是候选，读者得先自己把系数翻译成结论，
 * 再自己比较——两步都不是命题人该做的事。
 *
 * 这里的行是命题人真正要问的四个问题，每格直接给该候选在这个问题上的答案；行首的
 * ⇢ 标出这一行哪套更好，所以「哪套更可用」不用逐格心算。结论与分布预览图同源
 * （domain/usability.ts 读的就是画图那份 metrics），两个通道不可能互相矛盾。
 */
import type { DistributionMetrics } from '@/domain/distribution'
import {
  assessUsability,
  READINESS_FLAG,
  READINESS_LABEL,
  type Readiness,
  type UsabilityCheck,
} from '@/domain/usability'

export interface UsabilityColumn {
  label: string
  metrics: DistributionMetrics
}

const RANK: Record<Readiness, number> = { ready: 0, needsWork: 1, blocked: 2 }

export function UsabilityCompare({ columns }: { columns: UsabilityColumn[] }) {
  const verdicts = columns.map((c) => assessUsability(c.metrics))
  const first = verdicts[0]
  if (!first) return null

  // Row order == the check order in assessUsability, which is the order a
  // question-writer hits the problems in: 顺序 → 节奏 → 覆盖 → 题组.
  const keys = first.checks.map((c) => c.key)

  return (
    <table className="usability-cmp">
      <thead>
        <tr>
          <th />
          {columns.map((c, i) => (
            <th key={c.label}>
              {c.label}
              <div>
                <span className={`flag ${READINESS_FLAG[verdicts[i]!.level]}`}>
                  {READINESS_LABEL[verdicts[i]!.level]}
                </span>
              </div>
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {keys.map((key) => {
          const cells = verdicts.map((v) => v.checks.find((c) => c.key === key))
          const best = Math.min(...cells.map((c) => (c ? RANK[c.level] : RANK.ready)))
          const anyWorse = cells.some((c) => c && RANK[c.level] > best)
          return (
            <tr key={key}>
              <th scope="row">{cells.find((c) => c)?.label ?? key}</th>
              {cells.map((c, i) => (
                <Cell
                  key={columns[i]?.label ?? i}
                  check={c}
                  /* Only mark a winner when the two sides actually differ —
                     "⇢" on both columns of a tied row is noise. */
                  better={anyWorse && c !== undefined && RANK[c.level] === best}
                />
              ))}
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function Cell({ check, better }: { check: UsabilityCheck | undefined; better: boolean }) {
  if (!check) return <td className="muted">—</td>
  return (
    <td className={check.level === 'ready' ? 'ok' : 'todo'}>
      {better && (
        <span className="cmp-better" title="这一项这套更好">
          ⇢{' '}
        </span>
      )}
      {check.detail}
    </td>
  )
}
