/**
 * `/batches` —— 「生成结果」这个页签点进来的落地页。
 *
 * ## 为什么需要它
 *
 * 页签原来是这么写的：`store.batchId ? <NavLink> : <span class="nav-disabled">`。而 `store.batchId`
 * 是**本页会话**里正在跑（或刚跑完）的那一批，刷新即空。于是客户的实际遭遇是：S3 里躺着十几个历史
 * 批次，打开页面却看到「生成结果」是灰的，必须先去勾一个场景、提交一次生成，才能看见以前的东西。
 *
 * 用他的原话：
 *
 *   「用户首次打开页面时，之前的生成结果已经存在（S3 里有历史批次），但因为没有选场景就看不到
 *     『生成结果』——这不合理。」「三个 Tab 应该始终都能切换，不存在『灰置不可点』的情况。」
 *
 * ## 为什么是一个路由而不是在页签里算
 *
 * 页签要在**每一次渲染**时决定 href，而「最近一批是哪一批」要发一个请求才知道。把请求塞进顶栏，
 * 等于每次路由切换都去问一次 S3，且顶栏还得处理加载和失败两种状态——顶栏不该有状态。
 *
 * 所以页签永远指向 `/batches`（一个静态 href，永不灰置），由这一页去回答「哪一批」：
 *
 *   * 有历史 → 跳到最近那一批，`replace` 跳转，这样浏览器的后退键不会卡在这个中转页上。
 *   * 没有历史 → 空状态，一句话告诉他去哪儿开始。这是新用户第一次打开时唯一正确的画面。
 *   * store 里有活批次 → 直接用它，一个请求都不发。生成过程中点这个页签必须立刻回到那一批，
 *     而不是先去 S3 问一遍——那一批可能还没写完索引。
 */
import { useEffect, useState } from 'react'
import { Link, Navigate } from 'react-router-dom'
import { api } from '@/api/endpoints'
import { userMessage } from '@/api/http'
import { useBatchStore } from '@/stores/batchStore'

type Resolution =
  | { state: 'loading' }
  | { state: 'found'; batchId: string }
  | { state: 'empty' }
  | { state: 'error'; message: string }

export function LatestBatchRoute() {
  const liveBatchId = useBatchStore((s) => s.batchId)
  const [resolved, setResolved] = useState<Resolution>({ state: 'loading' })

  useEffect(() => {
    // 活批次优先：它可能还没写完 S3 索引，而它恰恰是用户此刻最想看的那一批。
    if (liveBatchId) {
      setResolved({ state: 'found', batchId: liveBatchId })
      return
    }
    let cancelled = false
    void api
      .batchHistory()
      .then((res) => {
        if (cancelled) return
        // 列表是后端按 created_at 倒序给的（`web/batch_store.py` 的 `_newest_first`），所以第一项
        // 就是最近一批。不在这里重排：排序是 store 的属性，两处各排一次就会漂移。
        const newest = res.batches[0]?.batch_id
        setResolved(newest ? { state: 'found', batchId: newest } : { state: 'empty' })
      })
      .catch((err) => {
        if (cancelled) return
        // 读不到 ≠ 没有。这两件事说成同一句话，会让一次 S3 故障看起来像「你还没生成过材料」，
        // 而那句话会让用户以为自己的东西丢了。
        setResolved({ state: 'error', message: userMessage(err, '历史记录暂时读取不到。') })
      })
    return () => {
      cancelled = true
    }
  }, [liveBatchId])

  if (resolved.state === 'found') {
    // `replace`：这一页是个中转站，留在历史栈里会让后退键先回到它、再被它送回去。
    return <Navigate to={`/batches/${resolved.batchId}`} replace />
  }

  if (resolved.state === 'loading') {
    return (
      <div className="page">
        <div className="panel panel-pad">正在读取历史批次…</div>
      </div>
    )
  }

  if (resolved.state === 'error') {
    return (
      <div className="page">
        <div className="panel panel-pad">
          <div>{resolved.message}</div>
          <Link className="btn btn-sm" to="/" style={{ marginTop: 10 }}>
            去场景选择
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div className="page">
      <div className="panel panel-pad empty-batches">
        <div className="empty-batches-icon" aria-hidden="true">
          📄
        </div>
        <h2>还没有生成过材料</h2>
        <p className="muted">请先到「场景选择」勾选场景并提交生成。生成好的批次会出现在这里。</p>
        <Link className="btn btn-primary" to="/">
          去场景选择
        </Link>
      </div>
    </div>
  )
}
