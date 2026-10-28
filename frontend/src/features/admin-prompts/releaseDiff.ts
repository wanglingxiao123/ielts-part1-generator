export interface DiffLine { text: string; changed: boolean }
export type ContextRow = { line: DiffLine } | { omitted: number }

/** Keep exact text; highlight the range between the shared prefix and suffix. */
export function changedLines(before: string, after: string) {
  const left = before.split('\n'), right = after.split('\n')
  let start = 0, end = 0
  while (start < left.length && start < right.length && left[start] === right[start]) start++
  while (end < left.length - start && end < right.length - start &&
    left[left.length - end - 1] === right[right.length - end - 1]) end++
  return {
    before: left.map((text, i) => ({ text, changed: i >= start && i < left.length - end })),
    after: right.map((text, i) => ({ text, changed: i >= start && i < right.length - end })),
    start, end,
  }
}

/** Fold only shared context, retaining two adjacent lines on each side. */
export function collapseContext(lines: DiffLine[], start: number, end: number): ContextRow[] {
  if (start === lines.length && end === 0) return lines.map((line) => ({ line }))
  const from = start > 4 ? start - 2 : 0
  const to = end > 4 ? lines.length - end + 2 : lines.length
  return [
    ...(from > 0 ? [{ omitted: from }] : []),
    ...lines.slice(from, to).map((line) => ({ line })),
    ...(to < lines.length ? [{ omitted: lines.length - to }] : []),
  ]
}
