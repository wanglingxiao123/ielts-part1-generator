import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { PasswordPage } from './PasswordPage'
import { RequireAuth } from './RequireAuth'
import { changePassword } from './authApi'
const session = vi.hoisted(() => ({ email: 'teacher@example.com', isAuthenticated: true, isLoading: false, error: null, user: { must_change_password: true }, signOut: vi.fn() }))
vi.mock('./useSession', () => ({ useSession: () => session }))
vi.mock('./authApi', () => ({ changePassword: vi.fn() }))
beforeEach(() => { vi.clearAllMocks() })
it('redirects temporary-password users before mounting business pages', () => {
  render(<MemoryRouter initialEntries={['/']}><Routes><Route path="/" element={<RequireAuth><p>业务页面</p></RequireAuth>} /><Route path="/account/password" element={<PasswordPage />} /></Routes></MemoryRouter>)
  expect(screen.queryByText('业务页面')).not.toBeInTheDocument()
  expect(screen.getByLabelText('临时密码')).toBeInTheDocument()
})
it('requires matching new passwords and clears password fields after success', async () => {
  vi.mocked(changePassword).mockResolvedValue()
  render(<PasswordPage />)
  expect(screen.getByRole('button', { name: '保存新密码' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('临时密码'), { target: { value: 'temporary123' } })
  fireEvent.change(screen.getByLabelText('新密码'), { target: { value: 'newpassword456' } })
  fireEvent.change(screen.getByLabelText('确认新密码'), { target: { value: 'mismatch' } })
  expect(screen.getByRole('button', { name: '保存新密码' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('确认新密码'), { target: { value: 'newpassword456' } })
  await userEvent.click(screen.getByRole('button', { name: '保存新密码' }))
  expect(changePassword).toHaveBeenCalledWith('temporary123', 'newpassword456')
  expect(await screen.findByRole('status')).toHaveTextContent('旧登录会话已失效')
  expect(screen.queryByDisplayValue('temporary123')).not.toBeInTheDocument()
})

it('accepts a legacy current password longer than the new-password limit', async () => {
  vi.mocked(changePassword).mockResolvedValue()
  render(<PasswordPage />)
  const current = screen.getByLabelText('临时密码')
  expect(current).not.toHaveAttribute('maxlength')
  const legacy = 'x'.repeat(1025)
  fireEvent.change(current, { target: { value: legacy } })
  fireEvent.change(screen.getByLabelText('新密码'), { target: { value: 'replacement123' } })
  fireEvent.change(screen.getByLabelText('确认新密码'), { target: { value: 'replacement123' } })
  await userEvent.click(screen.getByRole('button', { name: '保存新密码' }))
  expect(changePassword).toHaveBeenCalledWith(legacy, 'replacement123')
})
