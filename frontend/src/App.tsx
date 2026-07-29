import { lazy, Suspense } from 'react'
import { NavLink, Route, Routes } from 'react-router-dom'
import { LoginPage } from '@/auth/LoginPage'
import { RequireAuth } from '@/auth/RequireAuth'
import { useSession } from '@/auth/useSession'
import { ScenarioSelectPage } from '@/features/scenario-select/ScenarioSelectPage'
import { BatchProgressPage } from '@/features/batch-progress/BatchProgressPage'
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
          {/* 客户版式的三个页签：场景选择 / 生成结果 / 审核队列。
              「生成结果」在没有批次时也在位，只是不可点——页签列表跳着变会让人
              以为功能消失了。 */}
          {batchId ? (
            <NavLink
              to={`/batches/${batchId}`}
              className={({ isActive }) => (isActive ? 'active' : '')}
            >
              生成结果
            </NavLink>
          ) : (
            <span className="nav-disabled" title="提交一个批次后即可查看">
              生成结果
            </span>
          )}
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
