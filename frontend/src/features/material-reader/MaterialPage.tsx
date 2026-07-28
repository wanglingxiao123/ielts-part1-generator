import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '@/api/endpoints'
import { getThresholds } from '@/config/runtimeConfig'
import type { MaterialRecord } from '@/contracts/api'
import { analyseFormGroups } from '@/domain/formGroups'
import { joinFromRecord } from '@/domain/joinArtifacts'
import type { Playlist } from '@/domain/playlist'
import { buildPlaylist } from '@/domain/playlist'
import { useAudioStore } from '@/stores/audioStore'
import { useBatchStore } from '@/stores/batchStore'
import { AudioPlayer, AudioStatusNotice } from '../audio/AudioPlayer'
import { useAudioStatus } from '../audio/useAudioStatus'
import { useAudioPool } from '../audio/useAudioPool'
import { MaterialReader } from './MaterialReader'
import { QuestionTypePanel } from './QuestionTypePanel'

export function MaterialPage() {
  const { materialId } = useParams<{ materialId: string }>()
  const fromStore = useBatchStore((s) => (materialId ? s.materials[materialId] : undefined))
  const [record, setRecord] = useState<MaterialRecord | null>(fromStore ?? null)
  const [error, setError] = useState<string | null>(null)
  const [jump, setJump] = useState<{ turnIndex: number; nonce: number } | null>(null)

  const cursor = useAudioStore((s) => s.cursor)
  const playing = useAudioStore((s) => s.playing)
  const follow = useAudioStore((s) => s.follow)

  // Audio is owned here, not inside a child that would hand a fresh pool object
  // back up on every render (that loops).
  const audioEnabled = Boolean(record && !record.quarantined)
  const { status: audioStatus, error: audioError } = useAudioStatus(
    materialId ?? '',
    audioEnabled,
  )
  const playlist = useMemo<Playlist | null>(
    () =>
      audioStatus?.status === 'ready' && audioStatus.manifest
        ? buildPlaylist(audioStatus.manifest)
        : null,
    [audioStatus],
  )
  const pool = useAudioPool(playlist)

  useEffect(() => {
    if (fromStore) {
      setRecord(fromStore)
      return
    }
    if (!materialId) return
    void api
      .getMaterial(materialId)
      .then(setRecord)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
  }, [materialId, fromStore])

  const view = useMemo(() => (record ? joinFromRecord(record) : null), [record])
  const groups = useMemo(
    () => (view ? analyseFormGroups(view, getThresholds()) : null),
    [view],
  )

  const playingTurn =
    playing && playlist ? (playlist.entries[cursor]?.turnIndex ?? null) : null

  const onPlayTurn = useCallback(
    (turnIndex: number) => {
      if (!playlist) return
      const idx = playlist.turnToEntry.get(turnIndex)
      if (idx === undefined) return
      pool.playFrom(idx)
    },
    [playlist, pool],
  )

  // Auto-scroll follow (prd R6): can be switched off, reviewers often read
  // elsewhere while listening.
  useEffect(() => {
    if (!follow || playingTurn === null) return
    document
      .querySelector(`[data-turn="${playingTurn}"]`)
      ?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [follow, playingTurn])

  if (error) {
    return (
      <div className="page">
        <div className="banner banner-bad">
          <strong>加载失败</strong>
          <div>{error}</div>
        </div>
      </div>
    )
  }
  if (!view || !record || !groups) {
    return (
      <div className="page">
        <div className="panel panel-pad">加载中…</div>
      </div>
    )
  }

  return (
    <div className="page-wide">
      <div className="row" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>{view.scenario.slice(0, 70)}</h2>
        <span className={`flag ${record.quarantined ? 'flag-bad' : 'flag-good'}`}>
          {record.verdict}
        </span>
        <span className="mono muted">{record.material_id}</span>
        <span>
          总分 <strong className="mono">{view.audit.score.total}</strong>
        </span>
        <span>
          不可回收{' '}
          <strong className="mono" style={{ color: view.crossCheck.unrecoverable.length > 0 ? 'var(--bad)' : undefined }}>
            {view.crossCheck.unrecoverable.length}
          </strong>
        </span>
        {record.degraded && (
          <span className="flag flag-warn" title="首次评价即通过，未经修改与复评环节">
            degraded · 未经修改环节
          </span>
        )}
        <div className="spacer" style={{ flex: 1 }} />
        <Link className="btn btn-sm" to={`/compare/${record.scenario_key}`}>
          对比本场景
        </Link>
      </div>

      {record.quarantined && record.quarantine_reason && (
        <div className="banner banner-bad">
          <strong>本材料已隔离（{record.quarantine_reason.code}）</strong>
          <div>{record.quarantine_reason.message}</div>
        </div>
      )}

      {audioEnabled &&
        (playlist ? (
          <AudioPlayer playlist={playlist} pool={pool} currentTurn={playingTurn} />
        ) : (
          <AudioStatusNotice status={audioStatus} error={audioError} />
        ))}

      <MaterialReader
        view={view}
        height={640}
        playingTurn={playingTurn}
        onPlayTurn={playlist ? onPlayTurn : undefined}
        unplayableTurns={playlist?.unplayableTurnIndexes}
        jumpToTurn={jump}
      />

      <div className="split-2" style={{ marginTop: 12 }}>
        <QuestionTypePanel analysis={groups} />
        <div className="panel panel-pad">
          <h3>评价缺陷（audit.findings）</h3>
          {view.audit.findings.length === 0 && <div className="muted">无缺陷记录</div>}
          {view.audit.findings.map((f, i) => (
            <div key={i} style={{ borderBottom: '1px solid var(--line-2)', padding: '6px 0' }}>
              <div className="row">
                <span
                  className={`flag ${
                    f.severity === 'critical'
                      ? 'flag-bad'
                      : f.severity === 'major'
                        ? 'flag-warn'
                        : 'flag-neutral'
                  }`}
                >
                  {f.severity}
                </span>
                <strong style={{ fontSize: 12 }}>{f.rule}</strong>
                {f.turn_index != null ? (
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() => setJump({ turnIndex: f.turn_index!, nonce: Date.now() })}
                  >
                    跳到 turn {f.turn_index}
                  </button>
                ) : (
                  <span className="muted" style={{ fontSize: 11 }}>
                    全篇性问题
                  </span>
                )}
              </div>
              <div className="muted" style={{ fontSize: 11 }}>
                “{f.evidence}” → {f.fix}
              </div>
            </div>
          ))}
          {view.audit.warnings && view.audit.warnings.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <h3>warnings（不构成失败）</h3>
              {view.audit.warnings.map((w) => (
                <div key={w} className="muted" style={{ fontSize: 11 }}>
                  · {w}
                </div>
              ))}
            </div>
          )}
          <div style={{ marginTop: 10 }}>
            <h3>盲测对照（cross_check）</h3>
            <div className="row" style={{ fontSize: 12 }}>
              <span>
                matched <strong className="mono">{view.crossCheck.matched}</strong>
                {view.crossCheck.planned != null && ` / 计划 ${view.crossCheck.planned}`}
              </span>
              <span>
                不可回收{' '}
                <strong className="mono">
                  {view.crossCheck.unrecoverable.map((r) => r.number).join(', ') || '—'}
                </strong>
              </span>
              <span>
                意外考点{' '}
                <strong className="mono">
                  {view.crossCheck.unintended_target.map((r) => `seq${r.audit_seq}`).join(', ') ||
                    '—'}
                </strong>
              </span>
              {view.crossCheck.ambiguous && view.crossCheck.ambiguous.length > 0 && (
                <span className="flag flag-warn">
                  歧义 {view.crossCheck.ambiguous.map((r) => r.number).join(', ')}
                </span>
              )}
            </div>
            {/* The reason/evidence rows are the actionable part; a bare count
                tells a reviewer a point is unhearable but not which sentence. */}
            {view.crossCheck.unrecoverable.map((r) => (
              <div key={`u${r.number}`} className="muted" style={{ fontSize: 11, marginTop: 4 }}>
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => setJump({ turnIndex: r.turn_index, nonce: Date.now() })}
                >
                  不可回收 #{r.number} → turn {r.turn_index}
                </button>{' '}
                {r.target && <span className="mono">{r.target}</span>} “{r.evidence}”
              </div>
            ))}
            {view.crossCheck.unintended_target.map((r) => (
              <div key={`x${r.audit_seq}`} className="muted" style={{ fontSize: 11, marginTop: 4 }}>
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => setJump({ turnIndex: r.turn_index, nonce: Date.now() })}
                >
                  意外考点 seq{r.audit_seq} → turn {r.turn_index}
                </button>{' '}
                “{r.evidence}”
              </div>
            ))}
          </div>
          <div style={{ marginTop: 10 }}>
            <h3>篇幅指标（audit.metrics）</h3>
            <div className="row mono" style={{ fontSize: 12 }}>
              <span>对话 {view.audit.metrics.dialogue_words} 词</span>
              <span>{view.audit.metrics.dialogue_turns} 轮</span>
              <span>
                前 {view.audit.metrics.first_half_turns} / 后{' '}
                {view.audit.metrics.second_half_turns}
              </span>
              <span>旁白 {view.audit.metrics.narrator_words} 词</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
