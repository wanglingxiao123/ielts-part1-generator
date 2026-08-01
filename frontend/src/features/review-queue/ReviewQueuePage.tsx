/**
 * 审核队列：用户已提交审核的材料，按批次分组。
 *
 * 这个页签原来是「隔离区」。隔离区不存在了——每套材料都会返回，都可选，没有一个
 * 「系统扣下、用户不该看」的抽屉。第三个页签因此换了主体：从「系统扣下的材料」
 * 变成「用户自己送进来的材料」。
 *
 * ## 为什么按批次分组，而不是按场景
 *
 * 提交和撤回都是**批次级**的动作（客户定的）。按场景分组读起来更像一份材料清单，但撤回按钮就没有
 * 自然的落点：一个场景的材料可能来自两个批次，那颗按钮撤谁？按批次分组之后，「一组 = 一次提交 =
 * 一次可撤回的单位」，按钮放在组头，位置和它的作用域一致。
 *
 * 每行的摘要是本机的（见 reviewQueueStore.ts），而「这一批被提交了」是后端记录的事实
 * （web/batch_history.py 的 submit / withdraw）。页面直说前者是本机的，而不是假装整份队列都是
 * 服务端事实——那会让人以为换台电脑也能看到这些摘要。
 */
import { Link } from 'react-router-dom'

import { api } from '@/api/endpoints'
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
  const removeBatch = useReviewQueue((s) => s.removeBatch)

  /**
   * 撤回一整批。两件事，和提交对称：清掉本机队列里这一批的所有条目，同时让后端把这一批的提交记录
   * 抹掉，状态回到「待选稿」。
   *
   * 原来只做了第一件，而且是逐条撤。于是把某一批全部撤完之后，队列是空的，历史面板那条却还写着
   * 「已提交」，而且没有任何操作能清掉——那个状态只有 submit 会写，没有东西会撤。
   *
   * 后端记不上不阻塞界面：本机那几条已经删了，为了一个状态标签把它们留在页面上更费解。失败只会让
   * 面板上多留一个「已提交」，再撤一次会重试。
   */
  const withdraw = (batchId: string) => {
    removeBatch(batchId)
    if (!batchId) return
    void api
      .withdrawBatch(batchId)
      .catch((err) => console.warn('[review-queue] 撤回状态没有记录成功', err))
  }

  const byBatch = new Map<string, typeof items>()
  for (const item of items) {
    const list = byBatch.get(item.batchId) ?? []
    list.push(item)
    byBatch.set(item.batchId, list)
  }

  return (
    <div className="page">
      <h2>审核队列</h2>
      <div className="muted" style={{ marginBottom: 12, fontSize: 12 }}>
        在生成结果页勾选并提交审核的材料会列在这里，按提交的批次分组。撤回是整批的：撤回之后这一批
        回到「待选稿」，可以重新选稿。这份清单保存在本机浏览器，换设备或清理浏览器数据后不会保留。
      </div>

      {items.length === 0 && (
        <div className="panel panel-pad muted">
          还没有提交审核的材料。到「生成结果」勾选后点「提交审核」即可。
        </div>
      )}

      {[...byBatch.entries()].map(([batchId, list]) => {
        const submittedAt = Math.max(...list.map((i) => i.submittedAt))
        return (
          <div key={batchId} className="panel" style={{ marginBottom: 12 }}>
            <div className="panel-pad" style={{ borderBottom: '1px solid var(--line-2)' }}>
              <div className="row">
                <strong style={{ fontSize: 13 }}>{batchId}</strong>
                <span className="flag flag-info">已提交</span>
                <span className="muted" style={{ fontSize: 12 }}>
                  {list.length} 套 · {submittedAgo(submittedAt)}提交
                </span>
                <span style={{ flex: 1 }} />
                <Link className="btn btn-sm" to={`/batches/${batchId}`}>
                  查看批次
                </Link>
                {/* 撤回在组头，因为它的作用域是整批。放在每条材料旁边会暗示可以只撤一条，而后端
                    与产品都以批次为单位——一个作用域比看起来更大的按钮，点下去才发现别的也没了。 */}
                <button type="button" className="btn btn-sm" onClick={() => withdraw(batchId)}>
                  撤回整批
                </button>
              </div>
            </div>
            {list.map((item) => {
              const meta = scenarioMeta(item.scenarioKey)
              return (
                <div key={item.materialId} className="q-row">
                  <span aria-hidden="true">{meta.icon}</span>
                  <strong style={{ fontSize: 13 }}>
                    {meta.titleZh} 第 {item.index + 1} 套
                  </strong>
                  <span style={{ flex: 1, fontSize: 12 }}>
                    {item.summary}
                    {/* 「提交时已知 N 处缺陷」去掉了：质量评价建议一律在「阅读全文」里说，
                        因为只有在全文的上下文里那句建议才读得懂（客户原话）。一个脱离上下文的
                        缺陷计数在这里既不能行动，也和详情页里的说法难以对上。 */}
                  </span>
                  <Link className="btn btn-sm" to={`/materials/${item.materialId}`}>
                    阅读全文
                  </Link>
                </div>
              )
            })}
          </div>
        )
      })}
    </div>
  )
}
