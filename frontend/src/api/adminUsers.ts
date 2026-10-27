import { realTransport } from './http'
import type { AuthUser } from '@/auth/authApi'

export interface UserDirectory {
  users: AuthUser[]
  history: Array<{ id: string; actor: string; email: string; before?: boolean; after?: boolean; kind?: "password_change" | "password_reset"; at: number }>
  history_total: number
}
export const adminUsers = {
  list: (signal?: AbortSignal) =>
    realTransport({ method: 'GET', path: '/admin/users', signal }) as Promise<UserDirectory>,
  resetPassword: (email: string, current_password: string) =>
    realTransport({ method: "POST", path: "/admin/users/reset-password", body: { email, current_password } }) as Promise<{ user: AuthUser; temporary_password: string; expires_in: number }>,
  role: (user: AuthUser) =>
    realTransport({ method: 'POST', path: '/admin/users/role', body: {
      email: user.email, is_admin: !user.is_admin, expected_admin: user.is_admin,
    } }) as Promise<{ user: AuthUser; changed: boolean }>,
}
