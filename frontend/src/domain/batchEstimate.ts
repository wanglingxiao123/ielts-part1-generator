/**
 * 提交前给出的耗时预估。
 *
 * ## 它现在是唯一的信号
 *
 * 「单批上限」已经删除（原 `max_batch: 6`）：web 层每套材料发一次独立的 AgentCore invoke
 * （`web/fanout.py`），15 分钟同步硬限约束的对象从整批变成单套，上限失去了平台依据。
 * 客户的要求是「用户想生成多少套就生成多少套，系统自己控制并发，对用户透明」。
 *
 * 于是这里的数字从「上限旁边的一个补充说明」变成了**唯一如实告知代价的地方**。20 套要跑
 * 十几分钟、100 套要跑一个多小时，这两句话都必须由这个函数说出来，因为再没有别的东西会
 * 阻止用户提交——这也正是应该的：提交是用户的决定。
 *
 * ## 为什么不是「套数 × 单套耗时」
 *
 * 旧的算法是 `total × 100…160 秒`，一个**串行**模型：4 套 → 7–11 分钟。系统根本不串行跑，
 * 所以墙钟时间跟的是**波数** `ceil(total / concurrency)`，不是套数。
 *
 * 变化的是并发度住在哪里：过去是后端 `batch.py` 的 `MAX_CONCURRENCY` 在一次 invoke 内部
 * 并发跑多套；现在是 web 层的 `FANOUT_CONCURRENCY`（`WEB_FANOUT_CONCURRENCY`，默认 6）
 * 同时开多少次 invoke。默认值一样是 6，波数公式因此不变——但对得上的是另一个模块了，
 * 见下面 `BACKEND_CONCURRENCY` 的注释。
 *
 * ## 一波多长：实测，不是估的
 *
 * `WAVE_SECONDS` 来自完整材料+题目链路上线后的真实 6 套批次：约 18:41、20:49、
 * 21 分钟和 39:15。并发 6 时它们都是一波。旧的 182–230 秒只测了早期材料链路，
 * 没包含当前题目生成、盲审、修改、候选替换和替补 slot，已经不代表用户等待时间。
 *
 * 区间保留为 18–40 分钟：方差主要来自质量重试和候选替换，报一个精确分钟数是假精度。
 *
 * 一个刻意**没有**建模的二阶效应：一波的耗时是这一波里最慢那套的耗时，所以 6 套
 * 一波理论上比 2 套一波略慢。没有实测数据支撑这个差值，宁可不编——区间的上界
 * 已经覆盖了实测的最慢情况。
 */

/**
 * 一波的墙钟区间（秒）。**实测范围**：完整生成链路约 18–39 分钟（并发 6），
 * 对用户显示时取整为 18–40 分钟。
 * 改这两个数之前请先测一次，不要按单套耗时推。
 */
export const WAVE_SECONDS: readonly [number, number] = [18 * 60, 40 * 60]

/**
 * 服务端默认并发度，现在对应 **web 层** `web/fanout.py` 的
 * `FANOUT_CONCURRENCY = max(1, int(os.environ.get("WEB_FANOUT_CONCURRENCY", "6")))`。
 *
 * 名字里的 BACKEND 是历史遗留：这个常量原本镜像的是 `backend/orchestration/batch.py` 的
 * `IELTS_CONCURRENCY`（一次 invoke 内部同时跑几套）。并发已经上移到 web 层——每套一次独立
 * invoke，同时开几个由 web 层控制——所以要跟着调的是 `WEB_FANOUT_CONCURRENCY`。两个默认值
 * 都是 6，所以数字没变；改动的是「跟谁对齐」，而这正是没写清楚就会悄悄漂移的那种事。
 *
 * 前端拿不到服务端的环境变量，所以这里是一份镜像；web 层调低并发时这里也要跟着调，
 * 否则预估会偏乐观。
 */
export const BACKEND_CONCURRENCY = 6

/** 波数：一波最多同时跑 `concurrency` 套。 */
export function waveCount(total: number, concurrency: number = BACKEND_CONCURRENCY): number {
  if (total <= 0) return 0
  return Math.ceil(total / Math.max(1, concurrency))
}

/** 预估墙钟区间（秒）。`total` 为 0 时是 [0, 0]。 */
export function estimateBatchSeconds(
  total: number,
  concurrency: number = BACKEND_CONCURRENCY,
): [number, number] {
  const waves = waveCount(total, concurrency)
  return [waves * WAVE_SECONDS[0], waves * WAVE_SECONDS[1]]
}

/**
 * 底栏的整分钟区间。
 *
 * 下界向下取整、上界向上取整，得到的是一个**包住**实测区间的整分钟范围——宁可说宽
 * 一点，也不要报 3.03 分钟这种假精度。
 */
export function describeBatchEstimate(
  total: number,
  concurrency: number = BACKEND_CONCURRENCY,
): string {
  if (total <= 0) return '—'
  const [minSec, maxSec] = estimateBatchSeconds(total, concurrency)
  const min = Math.max(1, Math.floor(minSec / 60))
  const max = Math.max(min, Math.ceil(maxSec / 60))
  return min === max ? `约 ${min} 分钟` : `约 ${min}–${max} 分钟`
}
