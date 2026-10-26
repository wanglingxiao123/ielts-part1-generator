import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { TrialResults } from './TrialResults'

afterEach(() => vi.restoreAllMocks())

it('copies exact snapshot and output on the HTTP ALB through the fallback', async () => {
  vi.stubGlobal('isSecureContext', false)
  const copied: string[] = []
  Object.defineProperty(document, 'execCommand', { configurable: true, value: vi.fn(() => {
    copied.push((document.activeElement as HTMLTextAreaElement).value)
    return true
  }) })
  render(<TrialResults id="test" trial={{ status: 'completed', baseline_prompt: 'A1规则', draft_prompt: 'A2规则', baseline: 'A1输出', draft: 'A2输出' }} />)
  fireEvent.click(screen.getByRole('button', { name: '复制生效版本输出' }))
  // Disclosure content is hidden until expanded.
  fireEvent.click(screen.getByText('草稿版本提示词（本次测试快照）'))
  fireEvent.click(screen.getByRole('button', { name: '复制草稿版本提示词' }))
  expect(copied).toEqual(['A1输出', 'A2规则'])
  expect(document.querySelector('textarea')).toBeNull()
  vi.unstubAllGlobals()
})
