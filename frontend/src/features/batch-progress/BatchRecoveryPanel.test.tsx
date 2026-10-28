import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { api } from '@/api/endpoints'
import { BatchRecoveryPanel } from './BatchRecoveryPanel'
import type { BatchRecoveryPlan } from '@/contracts/batchRecovery'

const plan: BatchRecoveryPlan = { batch_id: 'web-test', delivered: 7, requested: 9, status: 'available', targets: [
  { slot_id: 'slot-8', scenario_key: 'booking-hotel', index: 7, action: 'resume' },
  { slot_id: 'slot-9', scenario_key: 'booking-hotel', index: 8, action: 'resume' },
] }
beforeEach(() => { sessionStorage.clear(); vi.spyOn(api, 'batchRecovery').mockResolvedValue(plan); vi.spyOn(api, 'recoverBatch').mockResolvedValue({ batch_id: 'web-test', execution_id: 'recovery1', status: 'running', reused: false }) })
afterEach(() => vi.restoreAllMocks())
it('offers explicit continuation on a reopened batch and invokes nothing until clicked', async () => {
 const onStarted=vi.fn()
 render(<BatchRecoveryPanel batchId="web-test" onStarted={onStarted} />)
 const button=await screen.findByRole('button',{name:'继续生成剩余 2 套'})
 expect(api.recoverBatch).not.toHaveBeenCalled()
 await userEvent.click(button)
 expect(api.recoverBatch).toHaveBeenCalledTimes(1)
 expect(onStarted).toHaveBeenCalledOnce()
})
it('retains a lost response request across remounts and retries the same ID', async () => {
 vi.mocked(api.recoverBatch).mockRejectedValueOnce(new Error('network lost')).mockResolvedValue({ batch_id:'web-test', execution_id:'recovery1', status:'running', reused:true })
 const page=render(<BatchRecoveryPanel batchId="web-test" onStarted={vi.fn()} />)
 await userEvent.click(await screen.findByRole('button',{name:'继续生成剩余 2 套'}))
 await screen.findByRole('button',{name:'确认同一次恢复'})
 const first=vi.mocked(api.recoverBatch).mock.calls[0]![1]
 page.unmount()
 render(<BatchRecoveryPanel batchId="web-test" onStarted={vi.fn()} />)
 await userEvent.click(await screen.findByRole('button',{name:'确认同一次恢复'}))
 expect(vi.mocked(api.recoverBatch).mock.calls[1]![1]).toBe(first)
})
it('shows a retryable read error instead of an empty or restart state', async () => {
 vi.mocked(api.batchRecovery).mockRejectedValueOnce(new Error('read failed')).mockResolvedValue(plan)
 render(<BatchRecoveryPanel batchId="web-test" onStarted={vi.fn()} />)
 expect(await screen.findByRole('alert')).toHaveTextContent('断点状态暂时读取失败')
 expect(screen.queryByRole('button',{name:/继续生成剩余/})).not.toBeInTheDocument()
 await userEvent.click(screen.getByRole('button',{name:'重新检查断点'}))
 expect(await screen.findByRole('button',{name:'继续生成剩余 2 套'})).toBeEnabled()
 expect(api.recoverBatch).not.toHaveBeenCalled()
})
it('does not allow repeated clicks during submission and shows failed startup', async () => {
 let finish!: (value: Awaited<ReturnType<typeof api.recoverBatch>>) => void
 vi.mocked(api.recoverBatch).mockImplementation(()=>new Promise(resolve=>{finish=resolve}))
 render(<BatchRecoveryPanel batchId="web-test" onStarted={vi.fn()} />)
 const button=await screen.findByRole('button',{name:'继续生成剩余 2 套'})
 await userEvent.click(button);fireEvent.click(button)
 expect(api.recoverBatch).toHaveBeenCalledTimes(1)
 finish({batch_id:'web-test',execution_id:'recovery1',status:'failed',reused:true})
 expect(await screen.findByRole('alert')).toHaveTextContent('上次恢复未能启动')
 await waitFor(()=>expect(screen.getByRole('button',{name:'继续生成剩余 2 套'})).toBeEnabled())
})
it.each(['running','readonly'] as const)('does not offer paid actions for %s', async status=>{
 vi.mocked(api.batchRecovery).mockResolvedValue({...plan,status})
 render(<BatchRecoveryPanel batchId="web-test" onStarted={vi.fn()} />)
 await screen.findByText(/已交付 7/)
 expect(screen.queryByRole('button',{name:/继续生成剩余/})).not.toBeInTheDocument()
 expect(api.recoverBatch).not.toHaveBeenCalled()
})
it('refreshes delivered results without generating when server is already complete', async () => {
 vi.mocked(api.batchRecovery).mockResolvedValue({...plan,status:'complete',delivered:9,targets:[]})
 const onStarted=vi.fn()
 render(<BatchRecoveryPanel batchId="web-test" onStarted={onStarted} />)
 await userEvent.click(await screen.findByRole('button',{name:'刷新已交付材料'}))
 expect(onStarted).toHaveBeenCalledOnce()
 expect(api.recoverBatch).not.toHaveBeenCalled()
})
it('offers explicit model failure retry and never starts from viewing the error', async () => {
 vi.mocked(api.batchRecovery).mockResolvedValue({...plan,delivered:8,targets:[{...plan.targets[0]!,action:'retry'}]})
 render(<BatchRecoveryPanel batchId="web-test" onStarted={vi.fn()} />)
 const button=await screen.findByRole('button',{name:'重试生成剩余 1 套'})
 expect(screen.getByText(/已通过的听力材料会保留/)).toBeInTheDocument()
 expect(api.recoverBatch).not.toHaveBeenCalled()
 await userEvent.click(button)
 expect(api.recoverBatch).toHaveBeenCalledTimes(1)
})
