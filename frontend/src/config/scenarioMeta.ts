/**
 * 场景的展示元数据：中文名、所属大类、图标。
 *
 * 三样都从 `scenarios.generated.ts`（codegen 自 config/scenarios.yaml）现查，
 * 不在这里抄一份 key 列表——抄了就会和后端漂移，而后端不认识的 key 会直接 400。
 * 唯一手写的是图标：yaml 里没有这个字段，而客户的版式要求每个场景块有个图标。
 * 图标按大类给，个别场景覆盖，未知 key 退到一个中性图标而不是空白。
 */
import { SCENARIO_CATALOG } from './scenarios.generated'
import { CUSTOM_SCENARIO_KEY } from './scenarioTypes'

export interface ScenarioMeta {
  key: string
  /** 中文名；未知 key 回落到 key 本身，绝不显示空标题。 */
  titleZh: string
  /** 大类中文名，渲染成场景名右侧的灰色标签。 */
  categoryZh: string
  icon: string
}

/** 大类图标。id 与 config/scenarios.yaml 的 categories[].id 一致。 */
const CATEGORY_ICON: Record<string, string> = {
  booking: '🏨',
  accommodation: '🏠',
  employment: '💼',
  customer_service: '🧾',
  community: '🌱',
  daily_services: '🩺',
}

/** 同一大类内区分度高的场景单独给图标，让一批里的多个场景块一眼可分。 */
const SCENARIO_ICON: Record<string, string> = {
  'booking-car-rental': '🚗',
  'booking-shipping': '📦',
  'booking-exhibition': '🖼️',
  'booking-festival': '🎪',
  'accommodation-student-hall': '🛏️',
  'service-refund': '💳',
  'service-cleaning': '🧽',
  'service-brochure': '📄',
  'community-event-organising': '📣',
  'daily-driving-lessons': '🚙',
}

const FALLBACK_ICON = '📝'

/** 大类图标，给场景选择页的分类标题用（那里没有单个场景可查）。 */
export function categoryIcon(categoryId: string): string {
  return CATEGORY_ICON[categoryId] ?? FALLBACK_ICON
}

const INDEX: Map<string, ScenarioMeta> = new Map(
  SCENARIO_CATALOG.categories.flatMap((category) =>
    category.scenarios.map((scenario): [string, ScenarioMeta] => [
      scenario.key,
      {
        key: scenario.key,
        titleZh: scenario.titleZh,
        categoryZh: category.titleZh,
        icon: SCENARIO_ICON[scenario.key] ?? CATEGORY_ICON[category.id] ?? FALLBACK_ICON,
      },
    ]),
  ),
)

/** 一段场景描述压成一行标题。整句太长，撑破卡片和侧栏。 */
function asTitle(text: string): string {
  const one = text.replace(/\s+/g, ' ').trim()
  const clipped = one.length > 24 ? `${one.slice(0, 24)}…` : one
  return clipped
}

/**
 * `label` 是这个场景的实际描述文本，只有自定义场景需要它。
 *
 * 后端给自定义场景的 key 是 `custom-<sha1(文本)[:8]>`——为了同一段文本永远落在同一个 S3 前缀，
 * 这是对的，但它不是给人看的。以前只认字面 `custom`，所以带哈希的那些落到最后一行兜底，
 * 界面上就出现了「📝custom-6cf6e9b3 未分类」。现在带哈希的一律认作自定义场景，有文本就用文本。
 */
export function scenarioMeta(key: string, label?: string): ScenarioMeta {
  const hit = INDEX.get(key)
  if (hit) return hit
  if (key === CUSTOM_SCENARIO_KEY || key.startsWith(`${CUSTOM_SCENARIO_KEY}-`)) {
    return {
      key,
      titleZh: label && label.trim() ? asTitle(label) : '自定义场景',
      categoryZh: '自定义',
      icon: '✍️',
    }
  }
  // 其余未知 key 说明前端目录落后于后端，显示 key 本身比「未知场景」更有助于排查。
  return { key, titleZh: key, categoryZh: '未分类', icon: FALLBACK_ICON }
}
