import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App } from './App'
import { AuthProvider } from './auth/AuthProvider'
import { loadRuntimeConfig } from './config/runtimeConfig'
import { setTransport, setUnauthorizedHandler } from './api/http'
import './styles.css'

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
})

async function bootstrap() {
  // Runtime config first: apiBaseUrl, thresholds and flags all come from
  // /config.json so one image runs in every environment (design.md §9).
  await loadRuntimeConfig()

  if (import.meta.env.VITE_MOCK === '1') {
    const { installMocks } = await import('./mocks/handlers')
    installMocks()
  } else {
    // The real backend is an AgentCore Runtime with one POST /invocations, not
    // the §8 REST surface. The adapter translates between them; nothing above
    // api/http.ts knows the difference.
    //
    // No synthetic-audio branch: preview_audio / select / audio_status / presign_audio all exist
    // and produce real Polly clips, so the tone-clip scaffold that stood in for them is gone.
    const { installAgentCoreAdapter } = await import('./api/agentcore')
    setTransport(installAgentCoreAdapter().transport)
  }

  setUnauthorizedHandler(() => {
    // The session cookie is gone or expired. A full reload rather than a router
    // push: it re-runs /api/auth/me, so the provider reaches the anonymous state
    // and RequireAuth lands on /login. Never leave a blank page (prd R1).
    //
    // Skipped when already on /login, where a 401 is the expected answer and a
    // reload would wipe the error the user needs to read.
    if (window.location.pathname !== '/login') window.location.assign('/login')
  })

  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <AuthProvider>
            <App />
          </AuthProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </StrictMode>,
  )
}

void bootstrap()
