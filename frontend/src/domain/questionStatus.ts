/**
 * 没有题目时，页面该说什么。
 *
 * 「暂无题目」是好几种完全不同的处境挤在一句话里，而它们对读者的意思相差极大：
 *
 *   · 还在出题        —— 等着就行，这一次 invocation 会做完
 *   · 被时钟停在半路  —— 材料已经存住了，题目要等**下一次** invocation，等这个页面没用
 *   · 名额用尽        —— 这个位置换过几套材料都没能出成题，不会自己好
 *   · 系统故障        —— 与材料质量无关，要有人去看
 *   · 真的没出过      —— 这套材料从来没进过出题环节
 *
 * 判据只用后端写下的字段（`slot.checkpointed` / `system_fault` / `resumable` / `state`、以及请求
 * 文档自己的 `request_status`），一条都不从「有没有题」反推——见 `web/slot_state.find_slot` 上的
 * 注释：这里不出关于状态的第二个意见。
 *
 * `batch_id` 缺席时后端根本不去读 slot，两个字段都是 null，于是只能落到最后那句最弱的话。这是设计
 * 上的取舍（有题的常见路径不该付一次多余的 S3 读），不是漏了分支。
 */
import type { MaterialQuestionsResponse, MaterialQuestionSlot } from '@/contracts/api'

export type QuestionStatusTone = 'info' | 'warn' | 'bad' | 'neutral'

export interface QuestionStatusNote {
  /** `.banner-{tone}` / `.flag-{tone}` 的语气。`neutral` 表示不必配色，只是一句说明。 */
  tone: QuestionStatusTone
  /** 一行标题。 */
  headline: string
  /** 一到两句话：现在是什么处境，以及看的人要不要做什么。 */
  detail: string
  /** 这个处境会不会自己变好。真时页面留着轮询与「刷新」按钮。 */
  willResolveItself: boolean
}

/** 出题环节记录下来的失败原因 → 中文。缺项照原样显示，不编造。 */
const REASON_LABEL: Record<string, string> = {
  time_budget: '这一次运行的时间用完了',
  feasibility_regenerate: '可行性预检判定这套材料出不了十道可靠的题',
  feasibility_undecided: '可行性预检无法判定',
  feasibility_unrecognised: '可行性预检给出了本系统不认识的结论',
  not_assessable: '材料本身没通过评价，未进入出题',
  questions_not_deliverable: '出题反复修订后仍不达交付标准',
  question_stage_crashed: '出题环节连续异常中断',
  material_unreadable: '已存住的材料读不回来',
  slot_state_unwritable: '进度记录写不进存储',
  scenario_missing: '这个位置引用的场景不在本次请求里',
  validator_unavailable: '校验器不可用',
  model_error: '模型调用出错',
  audit_failed: '评价环节未通过',
  no_material_generated: '没有生成出材料',
  unhandled_error: '出现未处理的异常',
}

function reasonOf(slot: MaterialQuestionSlot): string {
  const reason = slot.last_failure?.reason
  if (!reason) return ''
  return REASON_LABEL[reason] ?? reason
}

/**
 * 题目缺席的原因。`null` 表示「有题」——调用方不该拿这个函数问一个已经有题的包。
 */
export function explainMissingQuestions(
  res: MaterialQuestionsResponse | null,
): QuestionStatusNote | null {
  if (!res || res.questions) return null
  const slot = res.slot
  const status = res.request_status

  // slot 读不到（没带 batch_id，或者这套材料不在任何 slot 里，比如它来自 fan-out 之前的旧批次）。
  if (!slot) {
    return {
      tone: 'neutral',
      headline: '暂无题目',
      detail: '这套材料还没有已交付的题目包。出题在材料之后进行，可稍后再回到这一页查看。',
      willResolveItself: false,
    }
  }

  const reason = reasonOf(slot)
  const suffix = reason ? `原因：${reason}。` : ''

  // 系统故障优先：它与材料质量无关，而下面几条都会被读成「材料不行」。
  if (slot.system_fault || status === 'system_failure') {
    return {
      tone: 'bad',
      headline: '出题因系统故障中断',
      detail: `${suffix}这不是材料质量问题，需要有人查看后端日志；重试这一页不会有帮助。`,
      willResolveItself: false,
    }
  }

  // checkpoint：这一次停了，材料存住了，下一次接着做。这是最容易被误读成「卡住了」的一种。
  if (slot.checkpointed) {
    return {
      tone: 'warn',
      headline: '出题已暂停，等待下一次运行接着做',
      detail: `${suffix}材料已经存住，下一次运行会从出题环节继续，不会重新生成材料。这一页不用一直等。`,
      willResolveItself: false,
    }
  }

  if (slot.state === 'exhausted') {
    return {
      tone: 'bad',
      headline: '这个位置的候选材料已用尽',
      detail: `${suffix}这一位置换过的材料都没能出成题，需要人工介入或另开一批。`,
      willResolveItself: false,
    }
  }

  // 请求已经结束，而这个位置仍然没有题：不会再自己动了。
  if (status === 'incomplete' || status === 'succeeded') {
    return {
      tone: 'warn',
      headline: '本次运行结束时这套材料还没有题',
      detail: `${suffix}要有题需要再跑一次；这一页不会自己更新。`,
      willResolveItself: false,
    }
  }

  // 还在推进。`state` 直接说到哪一步了——「出题中」和「材料刚做完还没进出题」不是一件事。
  const stage =
    slot.state === 'questions_pending'
      ? '正在生成、审核与修订题目'
      : slot.state === 'material_done'
        ? '材料已完成，出题即将开始'
        : '材料还在生成，出题在其之后'
  return {
    tone: 'info',
    headline: '题目正在生成中',
    detail: `${stage}。题目出好后这一页会自动显示，不需要手动刷新。`,
    willResolveItself: true,
  }
}
