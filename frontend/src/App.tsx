import { NavLink, Route, Routes } from 'react-router-dom'
import { RequireAuth } from '@/auth/RequireAuth'
import { useSession } from '@/auth/useSession'
import { ScenarioSelectPage } from '@/features/scenario-select/ScenarioSelectPage'
import { BatchProgressPage } from '@/features/batch-progress/BatchProgressPage'
import { MaterialPage } from '@/features/material-reader/MaterialPage'
import { ComparePage } from '@/features/compare/ComparePage'
import { QuarantinePage } from '@/features/quarantine/QuarantinePage'
import { FixtureGalleryPage } from '@/features/material-reader/FixtureGalleryPage'
import { useBatchStore } from '@/stores/batchStore'

function TopBar() {
  const session = useSession()
  const batchId = useBatchStore((s) => s.batchId)
  return (
    <div className="topbar">
      <h1>IELTS Part 1 材料生成与审阅</h1>
      <nav>
        <NavLink to="/" end className={({ isActive }) => (isActive ? 'active' : '')}>
          场景选择
        </NavLink>
        {batchId && (
          <NavLink
            to={`/batches/${batchId}`}
            className={({ isActive }) => (isActive ? 'active' : '')}
          >
            当前批次
          </NavLink>
        )}
        <NavLink to="/quarantine" className={({ isActive }) => (isActive ? 'active' : '')}>
          隔离区
        </NavLink>
        <NavLink to="/gallery" className={({ isActive }) => (isActive ? 'active' : '')}>
          夹具对照
        </NavLink>
      </nav>
      <div className="spacer" />
      <span className="who">
        {session.username || '未登录'}
        {session.roles.length > 0 && ` · ${session.roles.join('/')}`}
      </span>
      {session.mode === 'dev-bypass' && (
        <span className="badge-dev" title="config.json auth.devBypass=true；线上必须为 false">
          dev bypass · 未接 Cognito
        </span>
      )}
      {session.mode === 'cognito' && (
        <button type="button" className="btn btn-sm" onClick={session.signOut}>
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
          path="/quarantine"
          element={
            <RequireAuth>
              <QuarantinePage />
            </RequireAuth>
          }
        />
        <Route
          path="/gallery"
          element={
            <RequireAuth>
              <FixtureGalleryPage />
            </RequireAuth>
          }
        />
        <Route path="/auth/callback" element={<div className="page">登录回调处理中…</div>} />
        <Route path="*" element={<div className="page">页面不存在</div>} />
      </Routes>
    </div>
  )
}
