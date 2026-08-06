/**
 * 题型面板在**版本未知**时说什么（design.md D2）。
 *
 * 单独测这个组件，是因为出错的方式很具体：分析层已经用 `known: false` 表示「什么都没核对」，
 * 但面板只要照旧渲染那些字段，页面上就会出现「第 1…10 题没有对应信息点」和满屏「记录矛盾」——
 * 把「本页面读不懂这份合同」说成「这份材料是坏的」。命题人拿到的结论会完全相反：真正该做的是升级
 * 页面，而页面却在让人退回重做材料。
 *
 * 真实 v1 记录也在这里过一遍，因为它必须走的是另一条路：v1 是**读得懂**的，一切照常显示。
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { FALLBACK_CONFIG } from '@/config/runtimeConfig'
import { analyseFormGroups } from '@/domain/formGroups'
import { joinFromRecord } from '@/domain/joinArtifacts'
import { buildRecord } from '@/mocks/fixtures'
import type { Blueprint } from '@/contracts'
import { QuestionTypePanel } from './QuestionTypePanel'

const T = FALLBACK_CONFIG.thresholds
const O = { batchId: 'b', scenarioKey: 'accommodation-rental', index: 0 }

const analysisFor = (kind: Parameters<typeof buildRecord>[0], id: string) =>
  analyseFormGroups(joinFromRecord(buildRecord(kind, { ...O, materialId: id })), T)

describe('QuestionTypePanel', () => {
  it('reports a v2 record as complete', () => {
    render(<QuestionTypePanel analysis={analysisFor('balanced', 'bal')} />)
    expect(screen.getByText('1–10 题各有着落')).toBeTruthy()
    expect(screen.queryByText(/版本未知/)).toBeNull()
    expect(screen.queryByText('记录矛盾')).toBeNull()
  })

  /**
   * 真实 v1 归档记录：读得懂，所以一切照常，不出任何「未知」或「矛盾」。
   *
   * 这一条钉的是那次虚报的反面。那份记录的三个号码在 `multiple_choice` 键下，flattening 漏掉之后
   * 面板报「第 5、6、7 题没有对应信息点」——一份刚生成的合格材料被判成自相矛盾。
   */
  it('reports a real v1 record as complete, with no legacy-layout row', () => {
    render(<QuestionTypePanel analysis={analysisFor('v1Legacy', 'v1')} />)
    expect(screen.getByText('1–10 题各有着落')).toBeTruthy()
    expect(screen.queryByText('记录矛盾')).toBeNull()
    expect(screen.queryByText(/版本未知/)).toBeNull()
    // 历史版式不作为一行题型出现：它不是命题人今天能选的东西。
    expect(screen.queryByText(/multiple_choice/)).toBeNull()
  })

  it('says the version is unknown instead of accusing the material', () => {
    const bp = structuredClone(
      buildRecord('balanced', { ...O, materialId: 'x' }).blueprint,
    ) as unknown as Record<string, unknown>
    bp.blueprint_schema_version = 3
    const view = joinFromRecord({
      ...buildRecord('balanced', { ...O, materialId: 'v3' }),
      blueprint: bp as unknown as Blueprint,
    })
    render(<QuestionTypePanel analysis={analyseFormGroups(view, T)} />)

    expect(screen.getByText('版本未知，题型信息暂不解读')).toBeTruthy()
    // 三处不该出现的指控：完整性结论、缺题清单、以及每一行的「记录矛盾」。
    expect(screen.queryByText('1–10 题各有着落')).toBeNull()
    expect(document.body.textContent).not.toMatch(/没有对应信息点/)
    expect(screen.queryByText('记录矛盾')).toBeNull()
    // 题组信息仍然显示：那部分只读 items，不依赖 coverage，未知版本下依然是可信的事实。
    expect(document.querySelectorAll('.qt-table tbody tr')).toHaveLength(3)
  })
})
