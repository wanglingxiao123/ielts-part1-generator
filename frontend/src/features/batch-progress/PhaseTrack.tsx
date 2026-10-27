import { PHASE_LABEL, PHASE_SEQUENCE, type ProgressPhase } from '@/domain/progressStages'

export function PhaseTrack({ phase, finished }: { phase: ProgressPhase | null; finished: boolean }) {
  const activeIdx = phase ? PHASE_SEQUENCE.indexOf(phase) : -1
  return (
    <div className="phase-track" aria-label="生成进度">
      {PHASE_SEQUENCE.map((p, i) => {
        const done = finished || i < activeIdx
        const active = !finished && i === activeIdx
        return (
          <span key={p} className="phase-step-wrap">
            {i > 0 && <span className="phase-sep">›</span>}
            <span className={`phase-step${done ? ' done' : ''}${active ? ' active' : ''}`}>
              <span className="dot" />
              {PHASE_LABEL[p]}
            </span>
          </span>
        )
      })}
    </div>
  )
}

