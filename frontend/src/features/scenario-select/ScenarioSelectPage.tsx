/**
 * 场景选择页（客户优化版版式）。
 *
 * 版式按客户给的稿：大类分组的场景 chip（标题右侧一个「N 个场景」计数）、一块
 * 「生成设置」放每场景数量、一块可选的自定义场景（与勾选共存）、底部固定条显示
 * 已选数 / 预计套数 / 预计耗时 / 已选场景的可删标签。指导原则是他写的那句
 * 「用户看到的页面要尽量简单」。
 *
 * 两处和稿子不同，都是为了不撒谎：
 *
 * 1. **每场景数量只有一个全局输入**，稿子里也是一个（`每场景生成数量 [3] 套`），
 *    但旧版是每张卡各自一个步进器。全局一个更简单，也够用——客户的稿子就这么画的。
 *
 * 2. **耗时不用稿子里的「约 5 分钟」**，走 domain/batchEstimate.ts 的实测公式。
 *    稿子那个数字是画图时随手填的；那里的常量是在 AWS 上量出来的。
 *
 * chip 上不显示 `prompt_hint`：那是给生成器的约束，不是给用户读的文案（放 title）。
 *
 * ## 这里曾有一个「单批上限」拦截，它被删掉了
 *
 * 旧版算 `total > CATALOG.maxBatch` 并在提交前拒绝，理由写的是「后端 AgentCore Runtime 的
 * 15 分钟同步硬限，前端不得放宽」。那个理由在「一整批共用一次 invoke」时是真的；现在 web 层
 * 每套材料发一次独立 invoke（`web/fanout.py`），15 分钟约束的是单套（实测 146-230s），
 * 上限就没有平台依据了。客户的原话：「用户想生成多少套就生成多少套，系统自己控制并发，
 * 对用户透明」。
 *
 * 取而代之的不是「什么都不说」，而是**如实说清代价**：底栏一直显示预计套数与预计耗时
 * （`describeBatchEstimate`，按 web 层并发算波数），套数大时那个数字自己就会变得刺眼。
 * 提交是用户的决定，不是需要前端代替他做的判断——而且大批量恰好是这个改动想支持的用法。
 */
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '@/api/endpoints'
import { categoryIcon } from '@/config/scenarioMeta'
import { SCENARIO_CATALOG } from '@/config/scenarios.generated'
import { CUSTOM_SCENARIO_KEY } from '@/config/scenarioTypes'
import type { CreateBatchRequest } from '@/contracts/api'
import { describeBatchEstimate } from '@/domain/batchEstimate'
import type { RequestedScenario } from '@/domain/resultSlots'
import { useBatchStore } from '@/stores/batchStore'
import { useBatchStream } from '../batch-progress/useBatchStream'

const CATALOG = SCENARIO_CATALOG

/**
 * 每场景数量输入框的手滑上限，**不是**产品上限。
 *
 * 区别是实质的：产品上限（原 `maxBatch`）会拒绝一个用户想提交的请求；这个数字只夹住输入框，
 * 防止粘贴或长按方向键把 5 变成 500。它对总套数没有意见——勾 20 个场景 × 50 套照样提交，
 * 底栏会如实告诉用户那要跑多久。
 *
 * 50 的依据是「一个真人不会手打 50 套」而不是任何平台约束；真需要更多的人可以多勾场景。
 */
const PER_SCENARIO_SANITY_CAP = 50

/**
 * 超过这个套数时，底栏加一句「本批较大」的中性提示。
 *
 * 12 = web 层并发（6）的两波。低于它整批一波或两波跑完，预估落在几分钟量级，不值得多说一句话；
 * 高于它波数开始线性增长，用户应该在点提交之前看清那个数字。提示不禁止任何操作——这是刻意的，
 * 「拦下来」正是这次改动删掉的东西。
 */
const LONG_RUN_SETS = 12

/** 中文名索引，用于底栏标签，不另抄一份 key 列表。 */
const TITLE_BY_KEY = new Map(
  CATALOG.categories.flatMap((c) => c.scenarios.map((s) => [s.key, s.titleZh] as const)),
)

export function ScenarioSelectPage() {
  const navigate = useNavigate()
  const stream = useBatchStream()
  const store = useBatchStore()

  const [picked, setPicked] = useState<ReadonlySet<string>>(() => new Set<string>())
  const [perScenario, setPerScenario] = useState(CATALOG.defaultCount)
  const [customText, setCustomText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // 自定义场景「与上方勾选共存」：判据就是有没有写字，不再要一个额外的复选框。
  // 少一个控件、少一种「勾了但没写」的错误状态。
  const customOn = customText.trim().length > 0
  const selectedKeys = [...picked]
  const scenarioCount = selectedKeys.length + (customOn ? 1 : 0)
  const total = scenarioCount * perScenario
  const estimate = useMemo(() => describeBatchEstimate(total), [total])

  const toggle = (key: string) => {
    setPicked((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const submit = async () => {
    setError(null)
    if (total === 0) {
      setError('请至少勾选一个场景，或填写自定义场景。')
      return
    }

    const requested: RequestedScenario[] = selectedKeys.map((key) => ({
      scenarioKey: key,
      count: perScenario,
    }))
    if (customOn) requested.push({ scenarioKey: CUSTOM_SCENARIO_KEY, count: perScenario })

    const requests: CreateBatchRequest['requests'] = selectedKeys.map((key) => ({
      scenario_key: key,
      count: perScenario,
    }))
    if (customOn) {
      requests.push({
        scenario_key: CUSTOM_SCENARIO_KEY,
        scenario_text: customText.trim(),
        count: perScenario,
      })
    }

    setBusy(true)
    store.startCreating()
    try {
      const created = await api.createBatch({ requests, options: { narration_mode: 'full' } })
      store.initBatch({
        batchId: created.batch_id,
        total: created.total,
        // 结果页据此在任何 material 事件之前铺出骨架卡。
        requested,
        items: created.items.map((i) => ({
          ...i,
          status: 'pending',
          stage: 'queued',
          attempt: 0,
        })),
      })
      stream.connect(created.batch_id)
      navigate(`/batches/${created.batch_id}`)
    } catch (err) {
      // 没有 BATCH_LIMIT_EXCEEDED 分支了：后端与适配层都不再产生这个 code（见本文件顶部）。
      // 留一个只会命中死代码的分支，等于把已删除的上限当成还可能出现的失败态展示。
      setError(err instanceof Error ? err.message : String(err))
      store.setConnection('idle')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="page scn-page">
        <h2>选择生成场景</h2>
        <p className="scn-page-sub">勾选需要的场景，系统将为每个场景生成听力对话材料</p>

        {error && (
          <div className="banner banner-bad">
            <strong>无法提交</strong>
            <div>{error}</div>
          </div>
        )}

        {CATALOG.categories.map((cat) => (
          <section key={cat.id} className="scn-cat">
            <div className="scn-cat-head">
              <span className="scn-cat-icon" aria-hidden="true">
                {categoryIcon(cat.id)}
              </span>
              <span className="scn-cat-name">{cat.titleZh}</span>
              <span className="scn-cat-count">{cat.scenarios.length} 个场景</span>
            </div>
            <div className="scn-chips">
              {cat.scenarios.map((scn) => {
                const on = picked.has(scn.key)
                return (
                  <button
                    key={scn.key}
                    type="button"
                    className={`scn-chip${on ? ' on' : ''}`}
                    aria-pressed={on}
                    // prompt_hint 是给生成器的约束，不是页面文案；放 title 供需要时查看。
                    title={scn.hint}
                    onClick={() => toggle(scn.key)}
                  >
                    <span className="scn-chip-box" aria-hidden="true">
                      {on ? '✓' : ''}
                    </span>
                    <span>{scn.titleZh}</span>
                  </button>
                )
              })}
            </div>
          </section>
        ))}

        <div className="panel panel-pad scn-settings">
          <div className="scn-panel-title">生成设置</div>
          <label className="scn-setting">
            <span>每场景生成数量</span>
            {/*
              没有 max。上限已删除，这里也不再夹住输入——`min={1}` 留着，因为 0 套或负数套是
              无意义的请求而不是一个昂贵的请求。`PER_SCENARIO_SANITY_CAP` 只防手滑：粘贴或
              长按方向键很容易打出 500，那是打错字，不是意图。
            */}
            <input
              type="number"
              min={1}
              value={perScenario}
              onChange={(e) => {
                const n = Number(e.target.value)
                if (!Number.isFinite(n)) return
                setPerScenario(Math.max(1, Math.min(PER_SCENARIO_SANITY_CAP, Math.round(n))))
              }}
            />
            <span className="muted">套</span>
          </label>
        </div>

        {CATALOG.customScenario.enabled && (
          <div className="panel panel-pad scn-custom">
            <div className="scn-panel-title">自定义场景（可选）</div>
            <textarea
              rows={2}
              maxLength={CATALOG.customScenario.maxLength}
              placeholder="例如：餐厅点餐"
              value={customText}
              onChange={(e) => setCustomText(e.target.value)}
            />
            <div className="muted scn-custom-hint">
              与上方勾选共存，可同时提交。{customText.length}/
              {CATALOG.customScenario.maxLength}
            </div>
          </div>
        )}
      </div>

      <div className="summary-bar">
        <div className="scn-bar-left">
          <span>
            已选 <strong className="count">{scenarioCount}</strong> 个场景
          </span>
          <span className="muted">
            · 预计生成 <strong>{total}</strong> 套 · {estimate}
          </span>
          <div className="scn-tags">
            {selectedKeys.map((key) => (
              <button
                key={key}
                type="button"
                className="scn-tag"
                aria-label={`取消选择 ${TITLE_BY_KEY.get(key) ?? key}`}
                onClick={() => toggle(key)}
              >
                {TITLE_BY_KEY.get(key) ?? key} <span aria-hidden="true">×</span>
              </button>
            ))}
            {customOn && (
              <button
                type="button"
                className="scn-tag"
                aria-label="清空自定义场景"
                onClick={() => setCustomText('')}
              >
                自定义场景 <span aria-hidden="true">×</span>
              </button>
            )}
          </div>
        </div>
        {total > LONG_RUN_SETS && (
          // 不是拦截，是提示。套数大到耗时以「小时」计时才出现，措辞刻意是中性的
          // （`flag` 而不是 `flag-bad`）：这不是错误，就是一个用户应该看清楚再点的代价。
          <span className="flag">本批较大，预计 {estimate}，提交后请保持页面打开</span>
        )}
        <div className="spacer" style={{ flex: 1 }} />
        <button
          type="button"
          className="btn btn-primary"
          disabled={total === 0 || busy}
          onClick={() => void submit()}
        >
          {busy ? '提交中…' : `提交生成 ${scenarioCount} × ${perScenario} 套`}
        </button>
      </div>
    </>
  )
}
