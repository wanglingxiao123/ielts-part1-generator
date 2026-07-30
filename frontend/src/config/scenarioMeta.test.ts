/**
 * 场景元信息的兜底行为。
 *
 * 这个模块以前没有测试，于是一个真实缺陷活到了客户手上：后端给自定义场景的 key 是
 * `custom-<sha1(文本)[:8]>`（为了同一段文本永远落在同一个 S3 前缀，这是对的），但这里只认字面
 * `custom`，带哈希的那些落到最后一行兜底，界面上就出现了「📝custom-6cf6e9b3 未分类」。
 */
import { describe, expect, it } from 'vitest'
import { scenarioMeta } from './scenarioMeta'

describe('scenarioMeta', () => {
  it('目录里的场景用目录的中文名', () => {
    const meta = scenarioMeta('booking-hotel')
    expect(meta.titleZh).toBe('酒店预订')
    expect(meta.categoryZh).not.toBe('未分类')
  })

  it('带哈希的自定义 key 不再显示成哈希', () => {
    const meta = scenarioMeta('custom-6cf6e9b3')
    expect(meta.titleZh).not.toContain('custom-')
    expect(meta.titleZh).not.toContain('6cf6e9b3')
    expect(meta.categoryZh).toBe('自定义')
  })

  it('给了描述文本就用文本，这才是用户输入的东西', () => {
    const meta = scenarioMeta(
      'custom-6cf6e9b3',
      'A student phones a bike shop about repairing a bicycle.',
    )
    // 24 字符后截断，所以只断言开头——完整句子是给阅读页的，不是给这一行标题的。
    expect(meta.titleZh.startsWith('A student phones a bike')).toBe(true)
  })

  it('过长的描述截断，不撑破卡片和侧栏', () => {
    const long = 'A very long custom scenario description that would otherwise overflow the card'
    const meta = scenarioMeta('custom-deadbeef', long)
    expect(meta.titleZh.length).toBeLessThanOrEqual(25)
    expect(meta.titleZh.endsWith('…')).toBe(true)
  })

  it('没有文本时退回一个通用名，而不是空字符串', () => {
    expect(scenarioMeta('custom-6cf6e9b3', '   ').titleZh).toBe('自定义场景')
    expect(scenarioMeta('custom').titleZh).toBe('自定义场景')
  })

  it('真正未知的 key 仍显示 key 本身', () => {
    // 这说明前端目录落后于后端。显示 key 比显示「未知场景」更有助于排查，所以这一支保留。
    const meta = scenarioMeta('booking-something-new')
    expect(meta.titleZh).toBe('booking-something-new')
    expect(meta.categoryZh).toBe('未分类')
  })
})
