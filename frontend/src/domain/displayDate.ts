/** API timestamps are Unix seconds or ISO strings; display in the viewer's local time. */
export function displayDate(value: string | number | undefined, fallback = '时间未知') {
  if (value === undefined || value === '') return fallback
  const date = new Date(typeof value === 'number' ? value * 1000 : value)
  if (!Number.isFinite(date.getTime())) return fallback
  return date.toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  })
}
