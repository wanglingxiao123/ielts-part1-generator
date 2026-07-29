import type { AudioStatusResponse } from '@/contracts/api'
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

  return (
    <div className="player">
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

/**
 * 播放器还没有音频时占的那块位置：一个「生成音频」按钮，合成中显示进度。
 *
 * 客户的原话：「改成一个 button，让用户自行决定是否生成音频呢？点击后就可以在这个页面直接生成
 * 音频，后续如果选择这个材料音频也一直跟随，不用重新生成」。这就是这块面板的全部职责。
 *
 * 按钮走的是 `preview_audio` 而不是 `select`：选定会认领候选组、丢弃同场景的另一套，只想先听一
 * 遍的人不该因此失去备选。两条路径共用同一份 clip，所以「音频一直跟随」是后端保证的，不是这里
 * 记住了什么。
 *
 * 从这里删掉的一段话：「当前后端 /invocations 仅支持 generate 与 list_scenarios，选稿与合成端点
 * 尚未就绪」。那句话已经不成立——四个端点都在，真实 Polly 合成已端到端验证过。
 */
export function AudioPanel({
  status,
  error,
  onGenerate,
  generating,
  generateError,
}: {
  status: AudioStatusResponse | null
  error: string | null
  onGenerate: () => void
  /** 已按下按钮、还没等到第一个「合成中」状态的那一小段时间。 */
  generating: boolean
  generateError: string | null
}) {
  if (error) {
    return (
      <div className="banner banner-bad">
        <strong>音频状态查询失败</strong>
        <div>{error}</div>
      </div>
    )
  }

  if (status?.status === 'failed') {
    return (
      <div className="banner banner-bad">
        <strong>语音合成失败</strong>
        <div>{status.error ?? '未知原因'}</div>
        <div style={{ marginTop: 8 }}>
          <button type="button" className="btn" onClick={onGenerate}>
            重新生成音频
          </button>
        </div>
      </div>
    )
  }

  const inFlight = generating || status?.status === 'queued' || status?.status === 'synthesizing'
  if (inFlight) {
    const done = status?.progress.done ?? 0
    const total = status?.progress.total ?? 0
    return (
      <div className="audio-cta busy">
        <span className="spinner" aria-hidden="true" />
        <span className="audio-cta-title">正在生成音频</span>
        <div className="progress-track" style={{ maxWidth: 240 }}>
          <div
            className="progress-fill"
            style={{ width: `${total > 0 ? (done / total) * 100 : 0}%` }}
          />
        </div>
        {/* 分母是脚本的轮次数，所以「N 段」对得上原文里的 turn，不是一个只有我们懂的单位。 */}
        <span className="mono muted" style={{ fontSize: 12 }}>
          {total > 0 ? `${done} / ${total} 段` : '排队中'}
        </span>
      </div>
    )
  }

  return (
    <div className="audio-cta">
      <button type="button" className="btn btn-primary" onClick={onGenerate}>
        🎧 生成音频
      </button>
      <span className="muted" style={{ fontSize: 12 }}>
        先听一遍再决定要不要选用。生成后音频跟随这一套材料，选用时不会重新生成。
      </span>
      {generateError && <span className="flag flag-bad">{generateError}</span>}
    </div>
  )
}
