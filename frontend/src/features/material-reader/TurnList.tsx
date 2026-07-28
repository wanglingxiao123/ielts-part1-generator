/**
 * Script rendering. Evidence highlighting slices the text by the character
 * ranges computed in joinArtifacts — never by a runtime regex/substring search,
 * which would silently relocate a highlight onto a coincidentally matching turn.
 */
import { circled, type ViewMaterial, type ViewTurn } from '@/domain/types'

interface Props {
  view: ViewMaterial
  selectedTurn: number | null
  selectedItem: number | null
  flashTurn: number | null
  playingTurn: number | null
  onSelectTurn: (turnIndex: number) => void
  onSelectItem: (itemNumber: number, turnIndex: number) => void
  onPlayTurn?: (turnIndex: number) => void
  registerTurnRef: (turnIndex: number, el: HTMLDivElement | null) => void
  onlyAnnotated?: boolean
  unplayableTurns?: number[]
}

function TurnText({ turn, onSelectItem }: { turn: ViewTurn; onSelectItem: Props['onSelectItem'] }) {
  if (turn.highlights.length === 0) return <>{turn.text}</>
  const parts: React.ReactNode[] = []
  let cursor = 0
  turn.highlights.forEach((h, i) => {
    if (h.start > cursor) parts.push(turn.text.slice(cursor, h.start))
    parts.push(
      <mark key={`h${i}`} data-items={h.itemNumbers.join(',')}>
        {turn.text.slice(h.start, h.end)}
        {h.itemNumbers.map((n) => (
          <button
            key={n}
            type="button"
            className="inline-badge"
            style={{ background: 'none', border: 'none', padding: 0 }}
            onClick={(e) => {
              e.stopPropagation()
              onSelectItem(n, turn.index)
            }}
            title={`跳到旁注 ${n}`}
          >
            {circled(n)}
          </button>
        ))}
      </mark>,
    )
    cursor = h.end
  })
  if (cursor < turn.text.length) parts.push(turn.text.slice(cursor))
  return <>{parts}</>
}

export function TurnList({
  view,
  selectedTurn,
  selectedItem,
  flashTurn,
  playingTurn,
  onSelectTurn,
  onSelectItem,
  onPlayTurn,
  registerTurnRef,
  onlyAnnotated,
  unplayableTurns,
}: Props) {
  const turns = onlyAnnotated
    ? view.turns.filter((t) => t.items.length > 0 || t.findings.length > 0)
    : view.turns

  return (
    <div>
      {turns.map((turn) => {
        const isSelected =
          selectedTurn === turn.index ||
          (selectedItem !== null && turn.items.some((i) => i.number === selectedItem))
        return (
          <div
            key={turn.index}
            ref={(el) => registerTurnRef(turn.index, el)}
            data-turn={turn.index}
            className={
              'turn' +
              (turn.speaker === 'speaker1' ? ' narration' : '') +
              (turn.items.length > 0 ? ' has-item' : '') +
              (isSelected ? ' selected' : '') +
              (flashTurn === turn.index ? ' flash' : '') +
              (playingTurn === turn.index ? ' playing' : '')
            }
            onClick={() => onSelectTurn(turn.index)}
          >
            <div className="tno">{turn.index}</div>
            <div className="role">
              {turn.role}
              {turn.dialogueOrdinal !== null && (
                <div className="muted" style={{ fontSize: 10 }}>
                  #{turn.dialogueOrdinal}
                </div>
              )}
            </div>
            <div className="text">
              <TurnText turn={turn} onSelectItem={onSelectItem} />
              {turn.findings.map((f, i) => (
                <span
                  key={`f${i}`}
                  className={`flag finding-chip ${
                    f.severity === 'critical' ? 'flag-bad' : f.severity === 'major' ? 'flag-warn' : 'flag-neutral'
                  }`}
                  title={`${f.rule}\n修正：${f.fix}`}
                >
                  {f.severity}
                </span>
              ))}
              {onPlayTurn && (
                <>
                  <button
                    type="button"
                    className="play-turn"
                    disabled={unplayableTurns?.includes(turn.index)}
                    title={
                      unplayableTurns?.includes(turn.index)
                        ? '该片段合成失败，不可播放'
                        : '从此 turn 起连播'
                    }
                    onClick={(e) => {
                      e.stopPropagation()
                      onPlayTurn(turn.index)
                    }}
                  >
                    ▶ 此句
                  </button>
                  {unplayableTurns?.includes(turn.index) && (
                    <span className="flag flag-bad" style={{ marginLeft: 4 }}>
                      音频缺失
                    </span>
                  )}
                </>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
