import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { adminUsers } from '@/api/adminUsers'
import { UserManagerPage } from './UserManagerPage'

const session = vi.hoisted(() => ({ isAdmin: true, email: 'admin@example.com' }))
vi.mock('@/auth/useSession', () => ({ useSession: () => session }))
vi.mock('@/api/adminUsers', () => ({ adminUsers: { list: vi.fn(), role: vi.fn(), resetPassword: vi.fn() } }))
const teacher = { email: 'teacher@example.com', is_admin: false, created_at: 1 }
beforeEach(() => {
  session.isAdmin = true
  vi.mocked(adminUsers.list).mockResolvedValue({
    users: [{ email: session.email, is_admin: true, created_at: 1 }, teacher], history: [], history_total: 0,
  })
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: { configurable: true, value: function (this: HTMLDialogElement) { this.setAttribute('open', '') } },
    close: { configurable: true, value: function (this: HTMLDialogElement) { this.removeAttribute('open') } },
  })
})
afterEach(() => {
  delete (HTMLDialogElement.prototype as Partial<HTMLDialogElement>).showModal
  delete (HTMLDialogElement.prototype as Partial<HTMLDialogElement>).close
  vi.restoreAllMocks(); vi.resetAllMocks()
})

it('hides the page from ordinary users without fetching the directory', () => {
  session.isAdmin = false
  render(<UserManagerPage />)
  expect(screen.getByRole('alert')).toHaveTextContent('仅管理员')
  expect(adminUsers.list).not.toHaveBeenCalled()
})

it('searches registered users and requires confirmation before granting administrator', async () => {
  render(<UserManagerPage />)
  expect(await screen.findByRole('button', { name: '取消管理员：admin@example.com' })).toBeDisabled()
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'teacher' } })
  await userEvent.click(screen.getByRole('button', { name: '设为管理员：teacher@example.com' }))
  expect(adminUsers.role).not.toHaveBeenCalled()
  expect(screen.getByRole('dialog')).toHaveTextContent('也能管理其他用户')
  vi.mocked(adminUsers.role).mockResolvedValue({ user: { ...teacher, is_admin: true }, changed: true })
  await userEvent.click(screen.getByRole('button', { name: '确认变更' }))
  expect(adminUsers.role).toHaveBeenCalledWith(teacher)
  expect(await screen.findByRole('status')).toHaveTextContent('已设为管理员')
})

it('cancelling the dialog does not change permissions', async () => {
  render(<UserManagerPage />)
  await userEvent.click(await screen.findByRole('button', { name: '设为管理员：teacher@example.com' }))
  await userEvent.click(screen.getByRole('button', { name: '取消' }))
  expect(adminUsers.role).not.toHaveBeenCalled()
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

it('requires administrator password and shows temporary password only in the reset dialog', async () => {
  vi.mocked(adminUsers.resetPassword).mockResolvedValue({ user: teacher, temporary_password: 'temporary-example', expires_in: 86400 })
  render(<UserManagerPage />)
  await userEvent.click(await screen.findByRole('button', { name: '重置密码：teacher@example.com' }))
  expect(screen.getByRole('button', { name: '确认重置密码' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('你的管理员密码'), { target: { value: 'admin-password' } })
  await userEvent.click(screen.getByRole('button', { name: '确认重置密码' }))
  expect(adminUsers.resetPassword).toHaveBeenCalledWith(teacher.email, 'admin-password')
  expect(await screen.findByLabelText('临时密码')).toHaveValue('temporary-example')
  await userEvent.click(screen.getByRole('button', { name: '已记下，关闭' }))
  expect(screen.queryByDisplayValue('temporary-example')).not.toBeInTheDocument()
})
