import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { expect, it, vi } from 'vitest'
import { SessionContext, type Session } from './sessionContext'
import { AccountMenu } from './AccountMenu'

it('keeps account actions in the disclosure, dismisses it and signs out', async () => {
  const signOut = vi.fn()
  const session: Session = {
    isAuthenticated: true, isLoading: false, isAdmin: true,
    email: 'long-administrator@example.com', user: null, error: null,
    signIn: vi.fn(), signUp: vi.fn(), signOut,
  }
  render(<MemoryRouter><SessionContext.Provider value={session}><AccountMenu /><button>其他操作</button></SessionContext.Provider></MemoryRouter>)
  const user = userEvent.setup()
  const trigger = screen.getByRole('button', { name: '账号菜单' })
  expect(screen.queryByRole('link', { name: '修改密码' })).not.toBeInTheDocument()
  await user.click(trigger)
  expect(screen.getByText(session.email)).toBeVisible()
  expect(screen.getByText('管理员')).toBeVisible()
  await user.tab()
  expect(screen.getByRole('link', { name: '修改密码' })).toHaveFocus()
  await user.keyboard('{Escape}')
  expect(trigger).toHaveFocus()
  expect(trigger).toHaveAttribute('aria-expanded', 'false')
  await user.click(trigger)
  fireEvent.pointerDown(document.body)
  expect(trigger).toHaveAttribute('aria-expanded', 'false')
  await user.click(trigger)
  await user.click(screen.getByRole('link', { name: '修改密码' }))
  expect(trigger).toHaveAttribute('aria-expanded', 'false')
  await user.click(trigger)
  await user.click(screen.getByRole('button', { name: '退出登录' }))
  expect(signOut).toHaveBeenCalledOnce()
})
