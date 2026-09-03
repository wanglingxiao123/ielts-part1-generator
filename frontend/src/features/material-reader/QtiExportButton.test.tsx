/**
 * 导出按钮：导出的是页面上显示的那一版；成功与失败都在页面上说清楚。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ApiError } from '@/api/http'
import { QtiExportButton } from './QtiExportButton'

const MATERIAL = '20260808-booking-hotel-45425df4'

afterEach(() => vi.restoreAllMocks())

describe('QtiExportButton', () => {
  it('names the displayed version and exports exactly that version', async () => {
    const download = vi.fn().mockResolvedValue({
      filename: `ielts-${MATERIAL}-v2.zip`,
      itemIdentifier: `ielts-${MATERIAL}-v2`,
      reviewCount: 3,
      bytes: 100,
    })
    render(
      <QtiExportButton materialId={MATERIAL} versionId="ver-2" ordinal={2} download={download} />,
    )
    const button = screen.getByRole('button', { name: '导出 QTI 2.2（V2）' })
    await userEvent.click(button)
    await waitFor(() => expect(screen.getByRole('status')).toBeInTheDocument())
    expect(download).toHaveBeenCalledWith(MATERIAL, 'ver-2')
    expect(screen.getByRole('status')).toHaveTextContent(`ielts-${MATERIAL}-v2.zip`)
    expect(screen.getByRole('status')).toHaveTextContent('3 项待人工确认')
  })

  it('sends no version for the original, so the server exports the original delivery', async () => {
    const download = vi.fn().mockResolvedValue({
      filename: 'x.zip',
      itemIdentifier: 'x',
      reviewCount: 0,
      bytes: 1,
    })
    render(
      <QtiExportButton materialId={MATERIAL} versionId="original" ordinal={1} download={download} />,
    )
    await userEvent.click(screen.getByRole('button'))
    await waitFor(() => expect(download).toHaveBeenCalledWith(MATERIAL, undefined))
    expect(await screen.findByRole('status')).toHaveTextContent('容错集无待确认项')
  })

  it('shows the server sentence on failure and does not claim a download', async () => {
    const download = vi
      .fn()
      .mockRejectedValue(
        new ApiError(422, 'QTI_EXPORT_REJECTED', '这套题目未通过导出门禁，不能生成 QTI 包。 G2: Q3 两侧答案不一致'),
      )
    render(
      <QtiExportButton materialId={MATERIAL} versionId="original" ordinal={1} download={download} />,
    )
    await userEvent.click(screen.getByRole('button'))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('G2: Q3 两侧答案不一致')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('forgets the last result when the displayed version changes', async () => {
    const download = vi.fn().mockResolvedValue({
      filename: 'a.zip',
      itemIdentifier: 'a',
      reviewCount: 0,
      bytes: 1,
    })
    const view = render(
      <QtiExportButton materialId={MATERIAL} versionId="original" ordinal={1} download={download} />,
    )
    await userEvent.click(screen.getByRole('button'))
    await screen.findByRole('status')
    view.rerender(
      <QtiExportButton materialId={MATERIAL} versionId="ver-2" ordinal={2} download={download} />,
    )
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByRole('button')).toHaveTextContent('V2')
  })
})
