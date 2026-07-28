/**
 * React wrapper for the pure layout (design.md §3.5 steps 1 + the rAF/measure
 * plumbing). All the placement logic lives in annotationLayout.ts so it can be
 * asserted against fixtures without a DOM.
 */
import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { getThresholds } from '@/config/runtimeConfig'
import type { ViewMaterial } from '@/domain/types'
import {
  DEFAULT_LAYOUT_OPTIONS,
  groupItemsIntoCards,
  layoutAnnotations,
  type PlacedCard,
} from './annotationLayout'

/** Estimated card height before the real DOM measurement lands. */
const ESTIMATED_CARD_HEIGHT = (itemCount: number) => 78 + (itemCount - 1) * 46

export interface AnnotationLayoutResult {
  cards: PlacedCard[]
  registerTurnRef: (turnIndex: number, el: HTMLDivElement | null) => void
  registerCardRef: (cardId: string, el: HTMLDivElement | null) => void
  containerRef: (el: HTMLDivElement | null) => void
  scrollToTurn: (turnIndex: number) => void
  contentHeight: number
}

export function useAnnotationLayout(view: ViewMaterial): AnnotationLayoutResult {
  const turnEls = useRef(new Map<number, HTMLDivElement>())
  const cardEls = useRef(new Map<string, HTMLDivElement>())
  const containerEl = useRef<HTMLDivElement | null>(null)
  const [cards, setCards] = useState<PlacedCard[]>([])
  const [contentHeight, setContentHeight] = useState(0)
  const frame = useRef<number | null>(null)

  const compute = useCallback(() => {
    const container = containerEl.current
    if (!container) return
    const containerTop = container.getBoundingClientRect().top

    const anchorOf = (turnIndex: number) => {
      const el = turnEls.current.get(turnIndex)
      if (!el) return 0
      return el.getBoundingClientRect().top - containerTop + container.scrollTop
    }

    const byTurn = view.turns
      .filter((t) => t.items.length > 0)
      .map((t) => ({ turnIndex: t.index, items: t.items }))

    const input = groupItemsIntoCards(byTurn, anchorOf, (turnIndex, count) => {
      const measured = cardEls.current.get(`turn-${turnIndex}`)?.offsetHeight
      return measured && measured > 0 ? measured : ESTIMATED_CARD_HEIGHT(count)
    })

    const t = getThresholds()
    const height = container.scrollHeight
    const placed = layoutAnnotations(input, {
      ...DEFAULT_LAYOUT_OPTIONS,
      containerHeight: Math.max(height, 400),
      clusterDispPx: t.CLUSTER_DISP_PX,
      clusterSpan: t.CLUSTER_SPAN,
      clusterMinPoints: t.CLUSTER_MIN_POINTS,
      // Cluster ids are turn-range based, so the count alone cannot look one
      // up; the tallest measured cluster card is a good enough proxy and the
      // second rAF pass corrects any residual error.
      clusterHeightFor: (n) => {
        let measured = 0
        for (const [id, el] of cardEls.current) {
          if (id.startsWith('cluster-')) measured = Math.max(measured, el.offsetHeight)
        }
        return measured > 0 ? measured : 30 + n * 66
      },
    })
    setCards(placed)
    setContentHeight(height)
  }, [view])

  const schedule = useCallback(() => {
    if (frame.current !== null) return
    frame.current = requestAnimationFrame(() => {
      frame.current = null
      compute()
    })
  }, [compute])

  useLayoutEffect(() => {
    compute()
    // Second pass once cards have real heights (first pass used estimates).
    schedule()
    const onResize = () => schedule()
    window.addEventListener('resize', onResize)
    const ro = new ResizeObserver(() => schedule())
    if (containerEl.current) ro.observe(containerEl.current)
    return () => {
      window.removeEventListener('resize', onResize)
      ro.disconnect()
      if (frame.current !== null) cancelAnimationFrame(frame.current)
      frame.current = null
    }
  }, [compute, schedule])

  const registerTurnRef = useCallback(
    (turnIndex: number, el: HTMLDivElement | null) => {
      if (el) turnEls.current.set(turnIndex, el)
      else turnEls.current.delete(turnIndex)
    },
    [],
  )

  const registerCardRef = useCallback((cardId: string, el: HTMLDivElement | null) => {
    if (el) cardEls.current.set(cardId, el)
    else cardEls.current.delete(cardId)
  }, [])

  const containerRef = useCallback(
    (el: HTMLDivElement | null) => {
      containerEl.current = el
      if (el) schedule()
    },
    [schedule],
  )

  const scrollToTurn = useCallback((turnIndex: number) => {
    turnEls.current.get(turnIndex)?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [])

  return { cards, registerTurnRef, registerCardRef, containerRef, scrollToTurn, contentHeight }
}
