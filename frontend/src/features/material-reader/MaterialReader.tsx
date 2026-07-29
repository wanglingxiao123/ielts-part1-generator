/**
 * The reader: distribution strip (sticky) + script + annotation column.
 * Three channels, three jobs (design.md §3.2).
 */
import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react'
import { getThresholds } from '@/config/runtimeConfig'
import { computeDistribution } from '@/domain/distribution'
import { analyseFormGroups } from '@/domain/formGroups'
import type { ViewMaterial } from '@/domain/types'
import { AnnotationCard } from './AnnotationCard'
import { DistributionStrip } from './DistributionStrip'
import { LeaderLines } from './LeaderLines'
import { TurnList } from './TurnList'
import { useAnnotationLayout } from './useAnnotationLayout'

/** Local UI state stays local — it is per-material, not global (design.md §7.2). */
interface ReaderState {
  selectedTurn: number | null
  selectedItem: number | null
  flashTurn: number | null
  onlyAnnotated: boolean
}

type ReaderAction =
  | { type: 'selectTurn'; turnIndex: number }
  | { type: 'selectItem'; itemNumber: number; turnIndex: number }
  | { type: 'jumpTurn'; turnIndex: number }
  | { type: 'clearFlash' }
  | { type: 'toggleOnlyAnnotated' }

function reducer(state: ReaderState, action: ReaderAction): ReaderState {
  switch (action.type) {
    case 'selectTurn':
      return { ...state, selectedTurn: action.turnIndex, selectedItem: null }
    case 'jumpTurn':
      // Flash even when the target turn carries no annotation — a finding jump
      // must be visible on its own.
      return {
        ...state,
        selectedTurn: action.turnIndex,
        selectedItem: null,
        flashTurn: action.turnIndex,
      }
    case 'selectItem':
      return {
        ...state,
        selectedItem: action.itemNumber,
        selectedTurn: action.turnIndex,
        flashTurn: action.turnIndex,
      }
    case 'clearFlash':
      return { ...state, flashTurn: null }
    case 'toggleOnlyAnnotated':
      return { ...state, onlyAnnotated: !state.onlyAnnotated }
  }
}

interface Props {
  view: ViewMaterial
  height?: number
  /** Narrow mode for the side-by-side compare view (design.md §4.2). */
  narrow?: boolean
  playingTurn?: number | null
  onPlayTurn?: (turnIndex: number) => void
  unplayableTurns?: number[]
  /** Lets the parent (e.g. a finding click) drive the scroll + flash. */
  jumpToTurn?: { turnIndex: number; nonce: number } | null
  showQuestionTypePanel?: boolean
}

const ANN_COL_WIDTH = 336
const COL_GAP = 56

export function MaterialReader({
  view,
  height = 620,
  narrow = false,
  playingTurn = null,
  onPlayTurn,
  unplayableTurns,
  jumpToTurn,
}: Props) {
  const thresholds = getThresholds()
  const metrics = useMemo(() => computeDistribution(view, thresholds), [view, thresholds])
  const groups = useMemo(() => analyseFormGroups(view, thresholds), [view, thresholds])

  const [state, dispatch] = useReducer(reducer, {
    selectedTurn: null,
    selectedItem: null,
    flashTurn: null,
    onlyAnnotated: false,
  })

  const layout = useAnnotationLayout(view)
  const scriptColRef = useRef<HTMLDivElement | null>(null)

  const selectItem = useCallback(
    (itemNumber: number, turnIndex: number) => {
      dispatch({ type: 'selectItem', itemNumber, turnIndex })
      layout.scrollToTurn(turnIndex)
    },
    [layout],
  )

  useEffect(() => {
    if (state.flashTurn === null) return
    const id = window.setTimeout(() => dispatch({ type: 'clearFlash' }), 1400)
    return () => window.clearTimeout(id)
  }, [state.flashTurn])

  // Keyed on `nonce` only. `layout` is a fresh object each render, so listing it
  // as a dependency makes this effect re-run on every render — and since it
  // dispatches, that is an infinite loop.
  const scrollToTurn = layout.scrollToTurn
  const nonce = jumpToTurn?.nonce ?? null
  const jumpTarget = jumpToTurn?.turnIndex ?? null
  useEffect(() => {
    if (nonce === null || jumpTarget === null) return
    const item = view.turns[jumpTarget]?.items[0]
    if (item) dispatch({ type: 'selectItem', itemNumber: item.number, turnIndex: jumpTarget })
    else dispatch({ type: 'jumpTurn', turnIndex: jumpTarget })
    scrollToTurn(jumpTarget)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce])

  const playingOrdinal =
    playingTurn != null ? (view.turns[playingTurn]?.dialogueOrdinal ?? null) : null

  const scriptWidth = scriptColRef.current?.offsetWidth ?? 0

  return (
    <div className="reader">
      <DistributionStrip
        view={view}
        metrics={metrics}
        groups={groups}
        selectedItem={state.selectedItem}
        onPickItem={selectItem}
        playingOrdinal={playingOrdinal}
        compact={narrow}
      />

      {/* 这里原来有 AnchorMismatchBanner：「有 N 个信息点标错了位置，旁注可能贴在了不相干的
          句子旁边……请退回重新生成」。那是把我们自己的标注 bug 当成材料质量问题摊给用户。
          现在的处理在 domain/anchors.ts：能确定挪正的按后端同一条规则静默挪正，确定不了的
          那一条旁注不显示（其余九条照常）。页面上不出现任何「可能标错了、你自己核对一下」。
          剔除只在显示层，view.blueprint 仍是完整十个点；开发者从控制台
          与 /dev/fixtures 的「定位」块看得到发生了什么。 */}
      <div className="reader-scroll" style={{ height }} ref={layout.containerRef}>
        <div className={`reader-body${narrow ? ' narrow' : ''}`}>
          <div ref={scriptColRef}>
            <TurnList
              view={view}
              selectedTurn={state.selectedTurn}
              selectedItem={state.selectedItem}
              flashTurn={state.flashTurn}
              playingTurn={playingTurn}
              onSelectTurn={(turnIndex) => dispatch({ type: 'selectTurn', turnIndex })}
              onSelectItem={selectItem}
              onPlayTurn={onPlayTurn}
              registerTurnRef={layout.registerTurnRef}
              onlyAnnotated={state.onlyAnnotated}
              unplayableTurns={unplayableTurns}
            />
          </div>

          {!narrow && (
            <>
              <LeaderLines
                cards={layout.cards}
                fromX={scriptWidth}
                toX={scriptWidth + COL_GAP}
                height={layout.contentHeight}
                selectedItem={state.selectedItem}
              />
              <div className="ann-col" style={{ width: ANN_COL_WIDTH }}>
                {layout.cards.map((card) => (
                  <AnnotationCard
                    key={card.id}
                    card={card}
                    blueprint={view.blueprint}
                    selectedItem={state.selectedItem}
                    onSelect={selectItem}
                    cardRef={(el) => layout.registerCardRef(card.id, el)}
                  />
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
