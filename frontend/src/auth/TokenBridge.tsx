import { setTokenProvider } from '@/api/http'
import { useSession } from './useSession'

/** Feeds the current id_token into the http client (design.md §7.1). */
export function TokenBridge() {
  const { idToken } = useSession()
  setTokenProvider(() => idToken)
  return null
}
