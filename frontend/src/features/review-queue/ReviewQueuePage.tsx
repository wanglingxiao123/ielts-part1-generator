/**
 * 审核队列：用户已提交审核的材料。
 *
 * 这个页签原来是「隔离区」。隔离区不存在了——每套材料都会返回，都可选，没有一个
 * 「系统扣下、用户不该看」的抽屉。第三个页签因此换了主体：从「系统扣下的材料」
 * 变成「用户自己送进来的材料」。
 *
 * 队列目前是本机的（见 reviewQueueStore.ts：后端还没有记录这个状态的地方）。
 * 页面直说这一点，而不是假装它是服务端事实——那会让人以为换台电脑也能看到。
 */
import { Link } from 'react-router-dom'
import { scenarioMeta } from '@/config/scenarioMeta'
import { useReviewQueue } from '@/stores/reviewQueueStore'

function submittedAgo(at: number): string {
  const minutes = Math.floor((Date.now() - at) / 60_000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.floor(hours / 24)} 天前`
}

export function ReviewQueuePage() {
  const items = useReviewQueue((s) => s.items)
  const remove = useReviewQueue((s) => s.remove)

  const byScenario = new Map<string, typeof items>()
  for (const item of items) {
    const list = byScenario.get(item.scenarioKey) ?? []
    list.push(item)
    byScenario.set(item.scenarioKey, list)
  }

  return (
    <div className="page">
      <h2>审核队列</h2>
      <div className="muted" style={{ marginBottom: 12, fontSize: 12 }}>
        在生成结果页勾选并提交审核的材料会列在这里。这份清单目前保存在本机浏览器，
        换设备或清理浏览器数据后不会保留。
      </div>

      {items.length === 0 && (
        <div className="panel panel-pad muted">
          还没有提交审核的材料。到「生成结果」勾选后点「提交审核」即可。
        </div>
      )}

      {[...byScenario.entries()].map(([scenarioKey, list]) => {
        const meta = scenarioMeta(scenarioKey)
        return (
          <div key={scenarioKey} className="panel" style={{ marginBottom: 12 }}>
            <div className="panel-pad" style={{ borderBottom: '1px solid var(--line-2)' }}>
              <div className="row">
                <span aria-hidden="true">{meta.icon}</span>
                <strong>{meta.titleZh}</strong>
                <span className="flag flag-neutral">{meta.categoryZh}</span>
                <span className="muted" style={{ fontSize: 12 }}>
                  {list.length} 套待审核
                </span>
              </div>
            </div>
            {list.map((item) => (
              <div key={item.materialId} className="q-row">
                <strong style={{ fontSize: 13 }}>第 {item.index + 1} 套</strong>
                <span style={{ flex: 1, fontSize: 12 }}>
                  {item.summary}
                  <div className="muted" style={{ fontSize: 11 }}>
                    {submittedAgo(item.submittedAt)}提交
                    {item.shortcomingCount > 0 &&
                      ` · 提交时已知 ${item.shortcomingCount} 处缺陷`}
                  </div>
                </span>
                <Link className="btn btn-sm" to={`/materials/${item.materialId}`}>
                  阅读全文
                </Link>
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => remove(item.materialId)}
                >
                  撤回
                </button>
              </div>
            ))}
          </div>
        )
      })}
    </div>
  )
}
