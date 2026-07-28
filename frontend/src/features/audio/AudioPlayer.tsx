import type { AudioStatusResponse } from '@/contracts/api'
import { getConfig } from '@/config/runtimeConfig'
import { formatMs, type Playlist } from '@/domain/playlist'
import { useAudioStore } from '@/stores/audioStore'
import type { AudioPoolApi } from './useAudioPool'

interface Props {
  playlist: Playlist
  pool: AudioPoolApi
  currentTurn: number | null
}

export function AudioPlayer({ playlist, pool, currentTurn }: Props) {
  const playing = useAudioStore((s) => s.playing)
  const cursor = useAudioStore((s) => s.cursor)
  const positionMs = useAudioStore((s) => s.positionMs)
  const rate = useAudioStore((s) => s.rate)
  const follow = useAudioStore((s) => s.follow)
  const setRate = useAudioStore((s) => s.setRate)
  const toggleFollow = useAudioStore((s) => s.toggleFollow)

  const entry = playlist.entries[cursor]
  const globalMs = (entry?.startMs ?? 0) + positionMs
  const pct = playlist.totalMs > 0 ? (globalMs / playlist.totalMs) * 100 : 0
  const gapMs = pool.lastGapMs()

  const synthetic = playlist.engine === 'synthetic-local'

  return (
    <div className="player">
      {synthetic && (
        <span className="flag flag-bad" title="flags.syntheticAudio=true；真实合成端点尚未就绪">
          非真实语音 · 本地占位音
        </span>
      )}
      <button
        type="button"
        className="btn btn-primary"
        onClick={() => (playing ? pool.pause() : cursor === 0 && positionMs === 0 ? pool.playFrom(0) : pool.resume())}
      >
        {playing ? '⏸ 暂停' : '▶ 全文播放'}
      </button>
      <button type="button" className="btn btn-sm" onClick={() => pool.stop()}>
        ⏹
      </button>

      <div
        className="seek"
        onClick={(e) => {
          const rect = e.currentTarget.getBoundingClientRect()
          const ratio = (e.clientX - rect.left) / rect.width
          const targetMs = ratio * playlist.totalMs
          const idx = playlist.entries.findIndex((x) => x.endMs >= targetMs)
          pool.playFrom(idx < 0 ? 0 : idx)
        }}
        title="点击跳转"
      >
        <i style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
      </div>
      <span className="mono" style={{ fontSize: 12 }}>
        {formatMs(globalMs)} / {formatMs(playlist.totalMs)}
      </span>

      <label style={{ fontSize: 12 }}>
        速率{' '}
        <select
          value={rate}
          onChange={(e) => setRate(Number(e.target.value) as 0.75 | 1 | 1.25)}
        >
          <option value={0.75}>0.75×</option>
          <option value={1}>1×</option>
          <option value={1.25}>1.25×</option>
        </select>
      </label>

      <label style={{ fontSize: 12 }}>
        <input type="checkbox" checked={follow} onChange={toggleFollow} /> 自动滚动跟随
      </label>

      <span className="muted" style={{ fontSize: 11 }}>
        片段 {cursor + 1}/{playlist.entries.length}
        {currentTurn !== null && ` · turn ${currentTurn}`}
        {gapMs !== null && ` · 衔接超出预设停顿 ${Math.round(gapMs)}ms`}
      </span>

      {playlist.unplayableTurnIndexes.length > 0 && (
        <span className="flag flag-warn">
          {playlist.unplayableTurnIndexes.length} 段合成失败，播放时跳过（turn{' '}
          {playlist.unplayableTurnIndexes.join(', ')}）
        </span>
      )}
      {playlist.orderingProblems.length > 0 && (
        <span className="flag flag-bad" title={playlist.orderingProblems.join('; ')}>
          清单顺序异常
        </span>
      )}
    </div>
  )
}

/** Synthesis progress / unavailability notice. Renders nothing once ready. */
export function AudioStatusNotice({
  status,
  error,
}: {
  status: AudioStatusResponse | null
  error: string | null
}) {
  if (error) {
    return (
      <div className="banner banner-bad">
        <strong>音频状态查询失败</strong>
        <div>{error}</div>
      </div>
    )
  }
  if (!status || status.status === 'not_requested') {
    const cfg = getConfig()
    return (
      <div className="banner banner-info">
        <strong>音频尚未合成</strong>
        <div>语音在选定材料之后才合成，避免为被弃用的材料付费。</div>
        {!cfg.flags.syntheticAudio && (
          <div style={{ marginTop: 6 }}>
            当前后端 <span className="mono">/invocations</span> 仅支持{' '}
            <span className="mono">generate</span> 与 <span className="mono">list_scenarios</span>
            ，选稿与合成端点尚未就绪（由 audio-storage 任务补齐）。
            此处不提供播放器，也不会产生任何 Polly 调用。
          </div>
        )}
      </div>
    )
  }
  if (status.status === 'queued' || status.status === 'synthesizing') {
    return (
      <div className="banner banner-info">
        <strong>语音合成中</strong>
        <div className="row">
          <div className="progress-track" style={{ maxWidth: 240 }}>
            <div
              className="progress-fill"
              style={{
                width: `${status.progress.total > 0 ? (status.progress.done / status.progress.total) * 100 : 0}%`,
              }}
            />
          </div>
          <span className="mono">
            已合成 {status.progress.done} / {status.progress.total} 段
          </span>
        </div>
      </div>
    )
  }
  if (status.status === 'failed') {
    return (
      <div className="banner banner-bad">
        <strong>语音合成失败</strong>
        <div>{status.error ?? '未知原因'}</div>
      </div>
    )
  }
  return null
}
