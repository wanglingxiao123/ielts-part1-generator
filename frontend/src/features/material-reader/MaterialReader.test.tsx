import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { joinFromRecord } from '@/domain/joinArtifacts'
import { buildRecord } from '@/mocks/fixtures'
import { MaterialReader } from './MaterialReader'

const O = { batchId: 'b', scenarioKey: 's', index: 0 }
const view = (kind: Parameters<typeof buildRecord>[0], id: string) =>
  joinFromRecord(buildRecord(kind, { ...O, materialId: id }))

describe('MaterialReader', () => {
  it('renders every turn with its index and role', () => {
    render(<MaterialReader view={view('balanced', 'bal')} />)
    const turns = document.querySelectorAll('[data-turn]')
    expect(turns).toHaveLength(43)
    expect(document.querySelector('[data-turn="0"]')?.className).toContain('narration')
    expect(document.querySelector('[data-turn="4"]')?.textContent).toContain('需求方')
  })

  it('highlights evidence inside the anchored turn only', () => {
    render(<MaterialReader view={view('balanced', 'bal')} />)
    const turn4 = document.querySelector('[data-turn="4"]')!
    const mark = turn4.querySelector('mark')
    expect(mark?.textContent).toContain("It's Anna Woods.")
    expect(mark?.getAttribute('data-items')).toBe('1')
    // A turn with no information point must carry no highlight.
    expect(document.querySelector('[data-turn="5"] mark')).toBeNull()
  })

  it('shows one cluster card labelled with the turn range for the clustered fixture', () => {
    render(<MaterialReader view={view('clustered', 'clu')} />)
    const clusters = document.querySelectorAll('.ann-card[data-cluster="true"]')
    expect(clusters).toHaveLength(1)
    expect(clusters[0]!.textContent).toContain('3 点集中于 turn 27–29')
  })

  it('shows no cluster card for the balanced fixture', () => {
    render(<MaterialReader view={view('balanced', 'bal')} />)
    expect(document.querySelectorAll('.ann-card[data-cluster="true"]')).toHaveLength(0)
    expect(document.querySelectorAll('.ann-card').length).toBeGreaterThan(0)
  })

  it('warns at material level and flags the card when an anchor is stale', () => {
    render(<MaterialReader view={view('anchorMismatch', 'mis')} />)
    expect(screen.getByText(/旁注可能错位/)).toBeInTheDocument()
    expect(screen.getByText('锚点失配')).toBeInTheDocument()
  })

  it('renders the distribution strip with raw metrics and a calibration caveat', () => {
    render(<MaterialReader view={view('clustered', 'clu')} />)
    expect(screen.getByText(/点位不避让/)).toBeInTheDocument()
    // Raw values are always shown, so the reviewer can still trust the numbers
    // even if the thresholds turn out to be wrong (design.md §3.4).
    const metrics = document.querySelector('.strip-metrics')!.textContent!
    expect(metrics).toContain('1.10') // CV
    expect(metrics).toContain('最大间隔 14') // max gap
    expect(metrics).toContain('前段 5 / 后段 5')
    expect(metrics).toContain('阈值待校准')
    expect(screen.getByText(/3 点挤在 turn 27–29/)).toBeInTheDocument()
  })

  it('places overview strip point marks at true position without avoidance', () => {
    // Points 6 and 7 both sit on turn 27 → identical left offset. The strip must
    // NOT spread them horizontally; that would fake an even distribution.
    render(<MaterialReader view={view('clustered', 'clu')} />)
    const marks = [...document.querySelectorAll<HTMLElement>('.axis-point')]
    expect(marks).toHaveLength(10)
    const lefts = marks.map((m) => m.style.left)
    const p6 = marks.find((m) => m.textContent === '⑥')!
    const p7 = marks.find((m) => m.textContent === '⑦')!
    expect(p6.style.left).toBe(p7.style.left)
    // ...but they are stacked vertically so neither is fully occluded.
    expect(p6.style.top).not.toBe(p7.style.top)
    // The clustered fixture must show fewer distinct x positions than points.
    expect(new Set(lefts).size).toBeLessThan(marks.length)
  })

  it('hides the annotation column in narrow (compare) mode and shows no audio', () => {
    render(<MaterialReader view={view('balanced', 'bal')} narrow />)
    expect(document.querySelectorAll('.ann-card')).toHaveLength(0)
    expect(document.querySelector('.reader-body')?.className).toContain('narrow')
    // Inline numbered badges remain, so points are still locatable.
    expect(document.querySelectorAll('.inline-badge').length).toBeGreaterThan(0)
    expect(document.querySelector('audio')).toBeNull()
    expect(document.querySelector('.player')).toBeNull()
  })
})
