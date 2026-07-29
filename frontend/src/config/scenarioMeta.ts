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

export function scenarioMeta(key: string): ScenarioMeta {
  const hit = INDEX.get(key)
  if (hit) return hit
  // 自定义场景走的是用户自己写的一段英文提示，没有目录条目；其余未知 key 说明
  // 前端目录落后于后端，此时显示 key 本身比显示「未知场景」更有助于排查。
  if (key === CUSTOM_SCENARIO_KEY) {
    return { key, titleZh: '自定义场景', categoryZh: '自定义', icon: '✍️' }
  }
  return { key, titleZh: key, categoryZh: '未分类', icon: FALLBACK_ICON }
}
