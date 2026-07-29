/**
 * 提交前给出的耗时预估。
 *
 * ## 为什么不是「套数 × 单套耗时」
 *
 * 旧的算法是 `total × 100…160 秒`，一个**串行**模型：4 套 → 7–11 分钟。
 * 后端根本不串行跑。`backend/orchestration/batch.py` 的 `MAX_CONCURRENCY`
 * （环境变量 `IELTS_CONCURRENCY`，默认 6）把待生成的套分批同时跑，所以墙钟时间
 * 跟的是**波数** `ceil(total / concurrency)`，不是套数。串行公式会把大约 3–4 分钟
 * 的活报成 7–11 分钟，用户据此砍掉本来提交得起的场景。
 *
 * ## 一波多长：实测，不是估的
 *
 * `WAVE_SECONDS` 来自一次真实的 **4 套批次在 AWS 上的端到端墙钟计时：
 * 182–230 秒**（并发 6，4 套一波跑完）。不是推算值。
 *
 * 与 `backend/docs/timing.md` 对得上：那份文档实测单套 146s（1 次重生成）到
 * 225s（2 次重生成），而"一波"的耗时就是这一波里最慢那一套的耗时——所以一波
 * 182–230s 落在单套区间的上半段，正是并发跑时应有的样子。timing.md 里
 * 「6 套分两波 292–450s」的算法用的是当时的并发 3；并发提到 6 之后，
 * `max_batch: 6` 的一整批只需要一波。
 *
 * 区间照旧是区间：方差主要来自重生成次数（timing.md 实测一次重生成 +33～85s），
 * 报一个精确的分钟数只会是假精度。
 *
 * 一个刻意**没有**建模的二阶效应：一波的耗时是这一波里最慢那套的耗时，所以 6 套
 * 一波理论上比 2 套一波略慢。没有实测数据支撑这个差值，宁可不编——区间的上界
 * 已经覆盖了实测的最慢情况。
 */

/**
 * 一波的墙钟区间（秒）。**实测值**：4 套批次在 AWS 上端到端 182–230s（并发 6）。
 * 改这两个数之前请先测一次，不要按单套耗时推。
 */
export const WAVE_SECONDS: readonly [number, number] = [182, 230]

/**
 * 后端默认并发度，对应 `backend/orchestration/batch.py` 的
 * `MAX_CONCURRENCY = int(os.environ.get("IELTS_CONCURRENCY", "6"))`。
 *
 * 前端拿不到后端的环境变量，所以这里是一份镜像；后端调低并发时这里也要跟着调，
 * 否则预估会偏乐观。当前 `max_batch` 也是 6，于是整批只需一波。
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
 * 底栏那句「约 3–4 分钟」。
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
