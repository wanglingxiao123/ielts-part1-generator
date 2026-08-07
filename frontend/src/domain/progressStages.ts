/**
 * 内部环节名 → 用户看到的四段进度：生成 → 校验 → 修改 → 复评。
 *
 * 后端的环节名比这四段多得多，而且其中一半是「重试」而不是「进展」：
 * `regenerating`（确定性校验没过，重写一遍）、`anchors_repaired`（锚点自动修好）、
 * `infra_retry`（模型调用失败重试）、`refilling`（评价方判不了，静默重跑这一套）。
 * 这些名字以前直接漏到卡片上，用户读到的是「校验未过，重新生成」——那是系统在
 * 汇报自己的失败，用户既不该看也管不了。
 *
 * 所以这里定的规则只有两条：
 *
 * 1. **重试不是失败，也不是倒退。** 每个环节名只映射到它属于的那一段；
 *    `advancePhase` 保证显示的段只前进不后退，于是「第 2 次生成」在用户眼里
 *    和第一次生成没有区别，就是还在生成。
 * 2. **不属于任何一段的环节不出声。** `infra_retry`、`refill_abandoned` 返回
 *    null：前者只是重试它打断的那一步，后者是后端放弃补位的内部决定。
 *
 * 这层只决定「显示哪一段」，不决定文案里出现什么数字——尝试次数、失败原因一律
 * 不在这里，也不在页面上。
 */
import type { MaterialStage } from '@/contracts/api'

/**
 * 客户写的顺序：生成→校验→修改→复评。后面三段是出题环节，`generate_sets` 才会走到。
 *
 * 出题为什么必须是**另外三段**、而不是复用前四段：一套材料合格之后，出题从零开始再走一遍
 * 「写→审→改」。复用的话，用户会看到进度条从「复评」跳回「生成」——而 `advancePhase` 只前进
 * 不后退，于是它压根不跳，整个出题阶段（可能好几分钟）进度条一动不动停在「复评」。
 * 那正是这一版之前的真实表现。
 */
export type ProgressPhase =
  | 'writing'
  | 'checking'
  | 'revising'
  | 'reviewing'
  | 'questioning'
  | 'question_review'
  | 'question_revising'

export const PHASE_SEQUENCE: readonly ProgressPhase[] = [
  'writing',
  'checking',
  'revising',
  'reviewing',
  'questioning',
  'question_review',
  'question_revising',
] as const

export const PHASE_LABEL: Record<ProgressPhase, string> = {
  writing: '生成',
  checking: '校验',
  revising: '修改',
  reviewing: '复评',
  questioning: '出题',
  question_review: '题目审核',
  question_revising: '题目修订',
}

/**
 * 每个已知环节名归属哪一段。键同时覆盖 §8 契约的六个 stage 和后端真实发出的
 * 环节名，因为适配层现在把原始名字原样带上来（agentcore.ts 的 sub_stage）。
 */
const PHASE_BY_STAGE: Record<string, ProgressPhase | null> = {
  // 生成：排队等着生成，也还是「生成」这一段——用户不关心队列。
  queued: 'writing',
  generating: 'writing',
  regenerating: 'writing',
  refilling: 'writing',
  // `generate_sets` 每个卡位开头发的那一下。和 `generating` 同段：用户看到的是「开始写这一套了」。
  material_started: 'writing',
  // 校验：确定性校验 + 锚点修复 + 评价方初评，用户眼里都是「在检查」。
  validating: 'checking',
  anchors_repaired: 'checking',
  auditing: 'checking',
  audited: 'checking',
  revising: 'revising',
  re_auditing: 'reviewing',

  // ── 出题环节（`action: generate_sets`）──
  //
  // 材料这一半到 `material_done` 为止，所以它归「复评」——那是材料的最后一段，不是出题的第一段。
  // 出题自己的三段按第 1 条同样的规则折叠：写题（含被拒后重写）、审题（校验+盲审+交叉检查）、
  // 改题。`question_set_clean` / `set_complete` 归「题目修订」而不是新开一段「完成」：进度条的
  // 最后一段亮起就是完成，再加一段会让「跑完了」和「还剩一段」看起来一样。
  material_done: 'reviewing',
  questions_started: 'questioning',
  question_generation_started: 'questioning',
  questions_restarting: 'questioning',
  questions_rejected: 'questioning',
  question_validated: 'question_review',
  question_cross_check: 'question_review',
  question_revision_started: 'question_revising',
  question_revision_skipped: 'question_revising',
  question_set_clean: 'question_revising',
  question_set_blocked: 'question_revising',
  set_complete: 'question_revising',

  // 不出声的两个：见文件头第 2 条。
  infra_retry: null,
  refill_abandoned: null,
}

/** 该环节属于哪一段；null = 不改变显示的段。未知环节名同样按 null 处理。 */
export function phaseOfStage(name: string): ProgressPhase | null {
  return PHASE_BY_STAGE[name] ?? null
}

/** 只前进不后退。重生成回到「生成」时，显示的段保持在已到过的最远处。 */
export function advancePhase(
  current: ProgressPhase | null,
  next: ProgressPhase | null,
): ProgressPhase | null {
  if (next === null) return current
  if (current === null) return next
  return PHASE_SEQUENCE.indexOf(next) > PHASE_SEQUENCE.indexOf(current) ? next : current
}

export interface StageInput {
  /** §8 契约的六段之一，来自 progress 事件。 */
  stage: MaterialStage
  /** 后端原始环节名（如果有）。比 stage 精确，优先用它判断段。 */
  rawStage?: string | null
}

/** 一条 progress 事件应显示在哪一段。原始名字优先，因为它没被折叠过。 */
export function phaseOfProgress(input: StageInput): ProgressPhase | null {
  const raw = input.rawStage ? phaseOfStage(input.rawStage) : null
  return raw ?? phaseOfStage(input.stage)
}

/**
 * 整批的一句话进度。
 *
 * 只说「到哪了」，不说任何一套材料正在经历什么第几次尝试：客户的原则是
 * 「生成完就直接返回结果」，页面顶部这一行是进度条的说明文字，不是日志。
 *
 * **不再重复 M/N。** 这句话原来是「已生成 0 / 2 套 · 正在生成」，而它右边紧挨着的
 * `.progress-count` 已经在说「已完成 0/2」——同一个数字在同一行出现两次。数字归计数器，这句话
 * 只负责计数器说不出来的那部分：还在跑的时候是哪一段，跑完了是齐没齐。
 */
export function describeProgress(input: {
  completed: number
  total: number
  /** 尚未产出材料的那些套里，走得最远的一段。 */
  phase: ProgressPhase | null
  finished: boolean
  /**
   * 这一次运行的时间用完了，余下的卡位在 S3 里等下一次接着做（`request_status: 'incomplete'`
   * 且 `resumable_slots` 非空）。
   *
   * 必须单独告诉这个函数，不能从 `finished` + 差额推：断点的外观和真缺口**完全一样**
   * （批次终态、`completed < total`），而下面那句「其余未能生成」会和页面上的断点提示直接对立
   * ——一句说没做出来、一句说存住了待续，用户没法同时相信两句。
   */
  checkpointed?: boolean
}): string {
  if (input.total > 0 && input.completed >= input.total) {
    return '全部生成完毕'
  }
  if (input.checkpointed) {
    return '本次已结束，余下留有断点'
  }
  // 跑完了但没跑齐（partial）。说「已全部生成」会和页面上那几张红色的「生成异常」
  // 卡片直接矛盾，而矛盾比缺一句话更像 bug。
  if (input.finished) {
    return '已结束，其余未能生成'
  }
  return input.phase ? `正在${PHASE_LABEL[input.phase]}` : '正在生成'
}
