/**
 * 单套材料的阅读页。
 *
 * 这一页展示什么，是按「命题人读完之后能做什么」筛过一遍的结果。去掉的三样：
 *
 *   · **评价指出的问题 / 无缺陷记录**。评价方的 finding 列表是内部质检记录，措辞是给系统看的；
 *     真正必须改的那几处已经由「考点小结」里的「听不出来」块指出来，并且能直接跳到句子。
 *   · **提示（不影响采用）**。原文照抄校验器输出，形如
 *     `dialogue words outside preferred 600-650: 559`。它是英文的、是阈值口径的，而且按定义不
 *     影响采用——一条读不懂又不用管的信息，只会训练人忽略这一整块。篇幅数字保留在下方「篇幅」里。
 *   · **盲读复核：这些点听得出来吗 / 计划 10 个，听出 10 个**。盲读本身是真的质量保证，但它的
 *     **计数**是内部核对口径。留下的是它唯一可行动的产出：具体哪几个点没被听出来、哪几个有歧义
 *     ——已经并入考点小结，用命题人的词说（「听不出来」），点号可跳转。
 *
 * 加上的一样：**考点小结**（ExamPointPanel）。客户的要求是把「拼读、先说后改、同义替换」这些
 * 考点抽出来用高亮块标注，判据取自规范 §3 与 §4B-3/4B-4，复用 domain/examPoints.ts。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '@/api/endpoints'
import { getThresholds } from '@/config/runtimeConfig'
import type { MaterialRecord } from '@/contracts/api'
import { summariseExamPoints } from '@/domain/examPoints'
import { analyseFormGroups } from '@/domain/formGroups'
import { joinFromRecord } from '@/domain/joinArtifacts'
import type { Playlist } from '@/domain/playlist'
import { buildPlaylist } from '@/domain/playlist'
import { circled } from '@/domain/types'
import { summariseValidationNotes } from '@/domain/validationNotes'
import { useAudioStore } from '@/stores/audioStore'
import { useBatchStore } from '@/stores/batchStore'
import { AudioPanel, AudioPlayer } from '../audio/AudioPlayer'
import { useAudioStatus } from '../audio/useAudioStatus'
import { useAudioPool } from '../audio/useAudioPool'
import { ExamPointPanel } from './ExamPointPanel'
import { MaterialReader } from './MaterialReader'
import { QuestionTypePanel } from './QuestionTypePanel'

export function MaterialPage() {
  const { materialId } = useParams<{ materialId: string }>()
  const fromStore = useBatchStore((s) => (materialId ? s.materials[materialId] : undefined))
  const [record, setRecord] = useState<MaterialRecord | null>(fromStore ?? null)
  const [error, setError] = useState<string | null>(null)
  const [jump, setJump] = useState<{ turnIndex: number; nonce: number } | null>(null)
  /** 生成音频：已按下、还没等到第一次「合成中」状态的那一小段。 */
  const [generating, setGenerating] = useState(false)
  const [generateError, setGenerateError] = useState<string | null>(null)
  /** 每次点「生成音频」+1，用来重新开启轮询（`not_requested` 会让轮询停下来）。 */
  const [pollKey, setPollKey] = useState(0)

  const cursor = useAudioStore((s) => s.cursor)
  const playing = useAudioStore((s) => s.playing)
  const follow = useAudioStore((s) => s.follow)

  // Audio is owned here, not inside a child that would hand a fresh pool object
  // back up on every render (that loops).
  // Audio may or may not exist yet: it is synthesised on demand from the button below (a preview,
  // which does NOT select the material) or by an earlier selection. Either way this poll reports
  // the same job, so the panel needs no knowledge of which of the two paid for the clips.
  const audioEnabled = Boolean(record)
  const { status: audioStatus, error: audioError } = useAudioStatus(
    materialId ?? '',
    audioEnabled,
    pollKey,
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
  const examPoints = useMemo(() => (view ? summariseExamPoints(view) : null), [view])
  /**
   * 校验意见。只在这一页出现——结果页卡片上不放任何评价文字（客户明确要求）。
   *
   * 非空意味着：三次生成的结构校验都没过，后端把最后一次交付了（校验是质检报告，不是门卫）。
   * 材料是完整的、可读的、可选的；这些只是出题前值得先看一眼的位置。
   */
  const validationNotes = useMemo(
    () => summariseValidationNotes(record?.validation_findings ?? []),
    [record?.validation_findings],
  )

  const jumpTo = useCallback((turnIndex: number) => {
    setJump({ turnIndex, nonce: Date.now() })
  }, [])

  /**
   * 生成音频。走 `preview_audio`，不是 `select`。
   *
   * 幂等由后端保证（同一个 job，不第二次调 Polly），所以重复点击是安全的；这里仍然在请求期间禁用
   * 按钮，只是为了不让人以为什么都没发生。
   */
  const doGenerate = useCallback(() => {
    if (!materialId) return
    setGenerating(true)
    setGenerateError(null)
    void api
      .previewAudio(materialId)
      // 先让轮询重新跑起来，再解除「生成中」——顺序反了会有一帧回到按钮态，看起来像点击丢了。
      .then(() => setPollKey((k) => k + 1))
      .catch((err) => {
        setGenerateError(err instanceof Error ? err.message : String(err))
        setGenerating(false)
      })
  }, [materialId])

  // 轮询已经报出真实状态之后，本地那个「刚点过」的标记就没用了，交给状态本身。
  useEffect(() => {
    if (audioStatus && audioStatus.status !== 'not_requested') setGenerating(false)
  }, [audioStatus])

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
  if (!view || !record || !groups || !examPoints) {
    return (
      <div className="page">
        <div className="panel panel-pad">加载中…</div>
      </div>
    )
  }

  return (
    <div className="page-wide">
      <div className="row" style={{ marginBottom: 8 }}>
        {/* 返回批次。这一页过去也是单向的：从结果页的「阅读全文」进来，页面上没有任何回去的入口，
            只剩浏览器后退键——而它是全宽布局、跟结果页长得不像，读起来像是离开了那个批次。
            batchId 取自材料自己，不靠 store：看历史批次的材料时 store 装的是当前活批次。 */}
        {record.batch_id && (
          <Link className="btn btn-sm" to={`/batches/${record.batch_id}`}>
            ← 返回批次
          </Link>
        )}
        <h2 style={{ margin: 0 }}>{view.scenario.slice(0, 70)}</h2>
        <span className="mono muted">{record.material_id}</span>
        {/* 「N 个点听不出来」原来在这里只是个数字。它已经并入考点小结的「听不出来」块——那里带
            点号、可跳转，能直接看到是哪几句。这里不再重复一个不能行动的计数。 */}
        {record.degraded && (
          <span className="flag flag-warn" title="首次评价即通过，未经修改与复评环节">
            未经修改环节
          </span>
        )}
        <div className="spacer" style={{ flex: 1 }} />
        {/* 带上 batch：对比页对历史批次只能靠它取材料（见 ComparePage 顶部注释）。 */}
        <Link
          className="btn btn-sm"
          to={`/compare/${record.scenario_key}?batch=${record.batch_id}`}
        >
          对比本场景
        </Link>
      </div>

      {/* 评价环节的判定在这里说成一句缺点，而不是一个状态：材料照样可读、可选，
          只是把「它哪里不行」摆出来让审阅者自己决定。分数与 verdict 枚举不出现。 */}
      {record.audit_rejection && (
        <div className="banner banner-warn">
          <strong>这一套有明显缺陷</strong>
          <div>{record.audit_rejection.message}。仍可选用，建议先通读全文确认。</div>
        </div>
      )}

      {audioEnabled &&
        (playlist ? (
          <AudioPlayer playlist={playlist} pool={pool} currentTurn={playingTurn} />
        ) : (
          <AudioPanel
            status={audioStatus}
            error={audioError}
            onGenerate={doGenerate}
            generating={generating}
            generateError={generateError}
          />
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
        <ExamPointPanel summary={examPoints} onJump={jumpTo} />
        <div className="panel-stack">
          <QuestionTypePanel analysis={groups} />

          {/* 校验意见。放在原文下面、题型面板旁边，因为它只有对着原文才有意义——
              这也是客户把评价文字限制在阅读页的理由。
              措辞是「看这里」而不是「这里坏了」：校验器自己会判错（本轮实测有 5 条规则
              会判掉真题），所以它给的是线索，不是判决。 */}
          {validationNotes.notes.length > 0 && (
            <div className="panel panel-pad">
              <h3>结构校验意见</h3>
              <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
                {validationNotes.headline}
              </div>
              <ul className="vn-list">
                {validationNotes.notes.map((note) => (
                  <li key={note.key}>
                    {note.numbers.length > 0 && (
                      <span className="vn-nums">
                        {note.numbers.map((n) => (
                          <button
                            key={n}
                            type="button"
                            className="ep-num"
                            title={`跳到第 ${n} 题的信息所在的那一句`}
                            onClick={() => jumpTo(examPoints.turnOf[n] ?? 0)}
                          >
                            {circled(n)}
                          </button>
                        ))}
                      </span>
                    )}
                    <span>{note.text}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div className="panel panel-pad">
            <h3>篇幅</h3>
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
