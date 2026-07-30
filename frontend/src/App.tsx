import { lazy, Suspense } from 'react'
import { NavLink, Route, Routes } from 'react-router-dom'
import { LoginPage } from '@/auth/LoginPage'
import { RequireAuth } from '@/auth/RequireAuth'
import { useSession } from '@/auth/useSession'
import { ScenarioSelectPage } from '@/features/scenario-select/ScenarioSelectPage'
import { BatchProgressPage } from '@/features/batch-progress/BatchProgressPage'
import { LatestBatchRoute } from '@/features/batch-progress/LatestBatchRoute'
import { MaterialPage } from '@/features/material-reader/MaterialPage'
import { ComparePage } from '@/features/compare/ComparePage'
import { ReviewQueuePage } from '@/features/review-queue/ReviewQueuePage'
import { useBatchStore } from '@/stores/batchStore'

/**
 * Fixture gallery: a DEVELOPMENT harness, not a product page.
 *
 * It reached the client as a top-level tab called 夹具对照 — a word from our
 * test vocabulary, on a page that shows four hand-built fixtures rather than
 * anything they generated. Reviewers reach a preview through
 * 场景选择 → 批次 → 阅读/对比; a separate tab competes with that flow and
 * suggests the fixtures are their materials.
 *
 * So: out of the nav, and reachable only under VITE_MOCK=1, which is how the
 * fixture-backed dev server and scripts/shots.mjs already run.
 *
 * `import.meta.env.VITE_MOCK` is inlined as a literal at build time, so this
 * whole branch — and with it the fixtures' script text — is tree-shaken out of
 * the production bundle rather than merely being unreachable in it.
 */
const DEV_FIXTURES = import.meta.env.VITE_MOCK === '1'
const FixtureGalleryPage = DEV_FIXTURES
  ? lazy(() =>
      import('@/features/material-reader/FixtureGalleryPage').then((m) => ({
        default: m.FixtureGalleryPage,
      })),
    )
  : () => null

function TopBar() {
  const session = useSession()
  const batchId = useBatchStore((s) => s.batchId)
  return (
    <div className="topbar">
      <h1>IELTS Part 1 材料生成</h1>
      {/* Nav only once there is a session: every destination is behind
          RequireAuth, so offering them to an anonymous visitor on /login just
          bounces them back to the form they are already looking at. */}
      {session.isAuthenticated && (
        <nav>
          <NavLink to="/" end className={({ isActive }) => (isActive ? 'active' : '')}>
            场景选择
          </NavLink>
          {/* 客户版式的三个页签：场景选择 / 生成结果 / 审核队列。三个都永远可点。
              「生成结果」原来在没有活批次时是灰的，判据是 `store.batchId`——那是**本页会话**的
              批次，刷新即空。于是 S3 里躺着十几个历史批次，用户打开页面却看到它是灰的，必须先勾一个
              场景、提交一次生成才能看见以前的东西。客户的原话：「三个 Tab 应该始终都能切换，不存在
              『灰置不可点』的情况。」
              href 是静态的 `/batches`：「最近一批是哪一批」要发请求才知道，而顶栏不该有状态。
              那个问题由 `LatestBatchRoute` 回答。活批次仍优先，见那个文件。 */}
          <NavLink
            to={batchId ? `/batches/${batchId}` : '/batches'}
            className={({ isActive }) => (isActive ? 'active' : '')}
          >
            生成结果
          </NavLink>
          <NavLink to="/review-queue" className={({ isActive }) => (isActive ? 'active' : '')}>
            审核队列
          </NavLink>
          {/* /dev/fixtures is deliberately NOT linked: dev harness, see above. */}
        </nav>
      )}
      <div className="spacer" />
      <span className="who">
        {session.email || '未登录'}
        {session.isAdmin && ' · 管理员'}
      </span>
      {/* Gated on "is there a session", not on an auth mode: there is one login
          path now, so 退出 is available whenever it can do anything. */}
      {session.isAuthenticated && (
        <button type="button" className="btn btn-sm" onClick={() => void session.signOut()}>
          退出
        </button>
      )}
    </div>
  )
}

export function App() {
  return (
    <div className="app">
      <TopBar />
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/"
          element={
            <RequireAuth>
              <ScenarioSelectPage />
            </RequireAuth>
          }
        />
        {/* 无 id 的落地页，「生成结果」页签指向它。它自己去找最近一批。 */}
        <Route
          path="/batches"
          element={
            <RequireAuth>
              <LatestBatchRoute />
            </RequireAuth>
          }
        />
        <Route
          path="/batches/:batchId"
          element={
            <RequireAuth>
              <BatchProgressPage />
            </RequireAuth>
          }
        />
        <Route
          path="/materials/:materialId"
          element={
            <RequireAuth>
              <MaterialPage />
            </RequireAuth>
          }
        />
        <Route
          path="/compare/:scenarioKey"
          element={
            <RequireAuth>
              <ComparePage />
            </RequireAuth>
          }
        />
        <Route
          path="/review-queue"
          element={
            <RequireAuth>
              <ReviewQueuePage />
            </RequireAuth>
          }
        />
        {DEV_FIXTURES && (
          <Route
            path="/dev/fixtures"
            element={
              <RequireAuth>
                <Suspense fallback={<div className="page">加载中…</div>}>
                  <FixtureGalleryPage />
                </Suspense>
              </RequireAuth>
            }
          />
        )}
        <Route path="*" element={<div className="page">页面不存在</div>} />
      </Routes>
    </div>
  )
}
