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
 *
 * 标题下的两个页签把这一页分成两件事：[对话原文] 是上面说的那一整套，行为一字未改；[题目预览]
 * 是这套材料已交付的题目包。两者共用同一个 `record`，所以切页签不重新取材料，也不丢播放位置——
 * 音频播放器在页签之上，理由是听音频与看题面是同时进行的动作，把播放器藏进某一个页签会让另一个
 * 页签里的人失去它。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '@/api/endpoints'
import { getThresholds } from '@/config/runtimeConfig'
import type { MaterialRecord } from '@/contracts/api'
import type { CommentAnchor } from '@/contracts/comments'
import { summariseExamPoints } from '@/domain/examPoints'
import { analyseFormGroups } from '@/domain/formGroups'
import { joinFromRecord } from '@/domain/joinArtifacts'
import type { Playlist } from '@/domain/playlist'
import { buildPlaylist } from '@/domain/playlist'
import { explainMissingQuestions } from '@/domain/questionStatus'
import { circled } from '@/domain/types'
import { summariseValidationNotes } from '@/domain/validationNotes'
import { useAudioStore } from '@/stores/audioStore'
import { useBatchStore } from '@/stores/batchStore'
import { AudioPanel, AudioPlayer } from '../audio/AudioPlayer'
import { useAudioStatus } from '../audio/useAudioStatus'
import { useAudioPool } from '../audio/useAudioPool'
import { ExamPointPanel } from './ExamPointPanel'
import { CommentComposer, CommentList } from './MaterialComments'
import { MaterialReader } from './MaterialReader'
import { QuestionPreviewPanel } from './QuestionPreviewPanel'
import { QuestionRevisionAction, QuestionVersionBar } from './QuestionVersionControls'
import { QuestionTypePanel } from './QuestionTypePanel'
import { useMaterialQuestions } from './useMaterialQuestions'
import { useMaterialComments } from './useMaterialComments'
import { useQuestionVersions } from './useQuestionVersions'

/** 标题下的两个页签。`script` 是进来时的默认，因为「阅读全文」是这一页原本的名字。 */
type Tab = 'script' | 'questions'

export function MaterialPage() {
  const { materialId } = useParams<{ materialId: string }>()
  const fromStore = useBatchStore((s) => (materialId ? s.materials[materialId] : undefined))
  const [record, setRecord] = useState<MaterialRecord | null>(fromStore ?? null)
  const [error, setError] = useState<string | null>(null)
  const [jump, setJump] = useState<{ turnIndex: number; nonce: number } | null>(null)
  const [tab, setTab] = useState<Tab>('script')
  const [questionAnchor, setQuestionAnchor] = useState<CommentAnchor | null>(null)
  const [turnAnchor, setTurnAnchor] = useState<CommentAnchor | null>(null)
  const [scriptCommentsOpen, setScriptCommentsOpen] = useState(false)
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
   * 从题目预览跳到原文。先切回 [对话原文] 页签，否则跳转发生在一个没挂载的阅读器上——点了没反应，
   * 而这个按钮的全部意义就是「让我看这句话在哪」。
   */
  const jumpToScript = useCallback(
    (turnIndex: number) => {
      setTab('script')
      setJump({ turnIndex, nonce: Date.now() })
    },
    [],
  )

  /**
   * 题目包。只在 [题目预览] 被打开过之后才去取：这一页最常见的用法是读原文，而题目在多数材料上
   * 根本还不存在，进页面就发一次注定 `questions: null` 的请求只是给每次打开都加一次往返。
   */
  const [questionsRequested, setQuestionsRequested] = useState(false)
  const questions = useMaterialQuestions(
    materialId ?? '',
    record?.batch_id,
    questionsRequested && Boolean(record),
  )
  const questionVersions = useQuestionVersions(
    materialId ?? '',
    Boolean(questions.data?.questions),
  )
  const missing = useMemo(() => explainMissingQuestions(questions.data), [questions.data])
  const comments = useMaterialComments(materialId ?? '', Boolean(record))
  const reloadComments = comments.reload
  const reconciledRevisionRef = useRef('')
  useEffect(() => {
    const request = questionVersions.revisionRequest
    if (
      !request?.request_id ||
      ![
        'completed',
        'no_change',
        'replan_questions',
        'needs_material_revision',
      ].includes(request.status) ||
      reconciledRevisionRef.current === request.request_id
    ) {
      return
    }
    reconciledRevisionRef.current = request.request_id
    reloadComments()
  }, [questionVersions.revisionRequest, reloadComments])
  const questionComments = useMemo(
    () =>
      comments.comments.filter(
        (comment) =>
          comment.anchor.type === 'question' &&
          (comment.version_id ?? 'original') === questionVersions.selectedVersionId,
      ),
    [comments.comments, questionVersions.selectedVersionId],
  )
  const turnComments = useMemo(
    () => comments.comments.filter((comment) => comment.anchor.type === 'turn'),
    [comments.comments],
  )
  const questionCommentCounts = useMemo(
    () =>
      questionComments.reduce((counts, comment) => {
        counts.set(comment.anchor.index, (counts.get(comment.anchor.index) ?? 0) + 1)
        return counts
      }, new Map<number, number>()),
    [questionComments],
  )
  const turnCommentCounts = useMemo(
    () =>
      turnComments.reduce((counts, comment) => {
        counts.set(comment.anchor.index, (counts.get(comment.anchor.index) ?? 0) + 1)
        return counts
      }, new Map<number, number>()),
    [turnComments],
  )

  const navigateComment = useCallback((anchor: CommentAnchor) => {
    if (anchor.type === 'question') {
      setTab('questions')
      setQuestionsRequested(true)
      setQuestionAnchor(anchor)
      window.setTimeout(() => {
        document
          .querySelector(`[data-question="${anchor.index}"]`)
          ?.scrollIntoView({ block: 'center', behavior: 'smooth' })
      }, 0)
      return
    }
    setTab('script')
    setScriptCommentsOpen(true)
    setTurnAnchor(anchor)
    setJump({ turnIndex: anchor.index, nonce: Date.now() })
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

      {/* 页签。切换的是标题以下的主体，音频播放器和上面那两条横幅留在外面——它们说的是这套材料
          本身，两个页签下都成立。 */}
      <div className="tabs" role="tablist" aria-label="材料内容">
        <button
          type="button"
          role="tab"
          className={`tab${tab === 'script' ? ' tab-on' : ''}`}
          aria-selected={tab === 'script'}
          onClick={() => setTab('script')}
        >
          对话原文
        </button>
        <button
          type="button"
          role="tab"
          className={`tab${tab === 'questions' ? ' tab-on' : ''}`}
          aria-selected={tab === 'questions'}
          onClick={() => {
            setTab('questions')
            setQuestionsRequested(true)
          }}
        >
          题目预览
        </button>
      </div>

      {tab === 'questions' ? (
        <QuestionsTab
          state={questions}
          missing={missing}
          blueprint={record.blueprint}
          view={view}
          onJump={jumpToScript}
          selectedQuestion={
            questionAnchor?.type === 'question' ? questionAnchor.index : null
          }
          commentCounts={questionCommentCounts}
          onSelectQuestion={(index) => setQuestionAnchor({ type: 'question', index })}
          comments={questionComments}
          commentsState={comments}
          onNavigateComment={navigateComment}
          versionsState={questionVersions}
        />
      ) : (
        <>
          <MaterialReader
            view={view}
            height={640}
            playingTurn={playingTurn}
            onPlayTurn={playlist ? onPlayTurn : undefined}
            unplayableTurns={playlist?.unplayableTurnIndexes}
            jumpToTurn={jump}
            commentCounts={turnCommentCounts}
            onSelectCommentTurn={(index) => {
              setTurnAnchor({ type: 'turn', index })
              setScriptCommentsOpen(true)
            }}
          />

          <section className={`script-comments${scriptCommentsOpen ? ' open' : ''}`}>
            <button
              type="button"
              className="script-comments-toggle"
              aria-expanded={scriptCommentsOpen}
              onClick={() => setScriptCommentsOpen((open) => !open)}
            >
              <span>批注 ({turnComments.length})</span>
              <span aria-hidden="true">{scriptCommentsOpen ? '⌄' : '⌃'}</span>
            </button>
            {scriptCommentsOpen && (
              <div className="script-comments-body">
                {comments.loading ? (
                  <div className="comment-empty">正在读取批注…</div>
                ) : (
                  <CommentList
                    comments={turnComments}
                    saving={comments.saving}
                    onNavigate={navigateComment}
                    onDelete={comments.remove}
                  />
                )}
                {comments.error && (
                  <div className="comment-error">
                    {comments.error}
                    <button type="button" onClick={comments.reload}>
                      重试
                    </button>
                  </div>
                )}
                <CommentComposer
                  anchor={turnAnchor}
                  saving={comments.saving}
                  onSubmit={comments.create}
                />
              </div>
            )}
          </section>

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
        </>
      )}
    </div>
  )
}

/**
 * [题目预览] 的主体：题目包，或者一句说清「为什么现在没有」的话。
 *
 * 五种没有题的处境由 `domain/questionStatus.ts` 分辨（还在出题 / 停在 checkpoint / 名额用尽 /
 * 系统故障 / 从没出过），这里只管把它画出来。读题目本身失败（存储没配、S3 拒绝）是第六种，
 * 与前五种分开：那是这个页面读不到，不是这套材料没有题。
 */
function QuestionsTab({
  state,
  missing,
  blueprint,
  view,
  onJump,
  selectedQuestion,
  commentCounts,
  onSelectQuestion,
  comments,
  commentsState,
  onNavigateComment,
  versionsState,
}: {
  state: ReturnType<typeof useMaterialQuestions>
  missing: ReturnType<typeof explainMissingQuestions>
  blueprint: MaterialRecord['blueprint']
  view: ReturnType<typeof joinFromRecord> | null
  onJump: (turnIndex: number) => void
  selectedQuestion: number | null
  commentCounts: ReadonlyMap<number, number>
  onSelectQuestion: (questionNumber: number) => void
  comments: ReturnType<typeof useMaterialComments>['comments']
  commentsState: ReturnType<typeof useMaterialComments>
  onNavigateComment: (anchor: CommentAnchor) => void
  versionsState: ReturnType<typeof useQuestionVersions>
}) {
  if (state.loading) {
    return <div className="panel panel-pad">正在读取题目…</div>
  }

  if (state.error) {
    return (
      <div className="banner banner-bad">
        <strong>题目读取失败</strong>
        <div>
          {state.error}
          <button type="button" className="btn btn-sm" style={{ marginLeft: 8 }} onClick={state.reload}>
            重试
          </button>
        </div>
      </div>
    )
  }

  const pkg = state.data?.questions
  if (pkg) {
    const displayedPackage = versionsState.selectedVersion?.package ?? pkg
    const displayedBlueprint = versionsState.selectedVersion?.blueprint ?? blueprint
    return (
      <div className="question-version-view">
        <QuestionVersionBar state={versionsState} />
        <div className="question-comments-layout">
          <QuestionPreviewPanel
            pkg={displayedPackage}
            blueprint={displayedBlueprint}
            view={view}
            onJump={onJump}
            selectedQuestion={selectedQuestion}
            commentCounts={commentCounts}
            onSelectQuestion={onSelectQuestion}
          />
          <aside className="question-comments-panel">
            <div className="comment-panel-head">批注 ({comments.length})</div>
            {commentsState.loading ? (
              <div className="comment-empty">正在读取批注…</div>
            ) : (
              <CommentList
                comments={comments}
                saving={commentsState.saving}
                onNavigate={onNavigateComment}
                onDelete={commentsState.remove}
                resolvedVersionLabel={(versionId) => {
                  const version = versionsState.versions.find(
                    (candidate) => candidate.id === versionId,
                  )
                  return version ? `V${version.ordinal}` : null
                }}
              />
            )}
            {commentsState.error && (
              <div className="comment-error">
                {commentsState.error}
                <button type="button" onClick={commentsState.reload}>
                  重试
                </button>
              </div>
            )}
            <CommentComposer
              anchor={
                selectedQuestion === null
                  ? null
                  : { type: 'question', index: selectedQuestion }
              }
              saving={commentsState.saving}
              disabled={
                versionsState.selectedVersion?.id !== versionsState.activeVersionId
              }
              onSubmit={(comment) =>
                commentsState.create({
                  ...comment,
                  version_id: versionsState.selectedVersionId,
                })
              }
            />
            <QuestionRevisionAction state={versionsState} comments={comments} />
          </aside>
        </div>
      </div>
    )
  }

  if (!missing) {
    // 到不了：`explainMissingQuestions` 只在有题时返回 null，而有题走的是上面那一支。留着这一句
    // 是为了让「两处对同一件事的判断分了岔」有个看得见的落点，而不是一个空白页面。
    return <div className="panel panel-pad muted">暂无题目。</div>
  }

  return (
    <div className={`banner banner-${missing.tone === 'neutral' ? 'info' : missing.tone}`}>
      <strong>{missing.headline}</strong>
      <div>
        {missing.detail}
        {!missing.willResolveItself && (
          <button type="button" className="btn btn-sm" style={{ marginLeft: 8 }} onClick={state.reload}>
            重新查看
          </button>
        )}
      </div>
    </div>
  )
}
