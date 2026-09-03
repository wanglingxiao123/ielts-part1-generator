/**
 * 「导出 QTI 2.2」——把当前正在看的这一版题目交给下游系统。
 *
 * 导出的永远是**页面上显示的那一版**（版本选择器选中的 `selectedVersionId`），不是「当前采用」
 * 的那一版。两者多数时候相同；不同的时候，用户看着 V1 点导出却拿到 V3 的包，是这一页最容易
 * 埋下的事故——包里没有任何可见标记能让他发现。按钮文字里把 V 号写出来，就是为了让这件事
 * 在点之前就看得见。
 *
 * 成功后说三件事：文件名、进了标识符的版本号、包内 review.txt 有几条待人工确认。最后一条
 * 是容错集生成器（qti_export/accept_sets.py）无法自动决定的判分口径，比如某个短语的简写算不算对。
 * 下游导入之前应该有人看过它，所以这里不把它藏在下载里悄悄带走。
 *
 * 失败时把服务端的原因逐条渲染出来。422 的原因是门禁失败（如「G2: Q3 两侧答案不一致」），这是
 * 上游审核数据自身的问题，重试没有用；写明是为了让命题人知道该去改题还是去找开发。
 */
import { useEffect, useState } from 'react'
import type { ApiError } from '@/api/http'
import { userMessage } from '@/api/http'
import { downloadQtiPackage, type QtiDownload } from '@/api/qtiExport'

export interface QtiExportButtonProps {
  materialId: string
  /** 版本选择器当前选中的版本；`'original'` 或一个版本 id。 */
  versionId: string
  /** 显示用的 V 号；没有版本服务时是 1。 */
  ordinal: number
  /** 注入点，组件测试用。默认真下载。 */
  download?: typeof downloadQtiPackage
}

export function QtiExportButton({
  materialId,
  versionId,
  ordinal,
  download = downloadQtiPackage,
}: QtiExportButtonProps) {
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState<QtiDownload | null>(null)
  const [error, setError] = useState<string | null>(null)

  // 切了版本，上一次的结果就不再描述这一版；清掉而不是留着误导。
  useEffect(() => {
    setDone(null)
    setError(null)
  }, [materialId, versionId])

  const run = async () => {
    setBusy(true)
    setError(null)
    setDone(null)
    try {
      const result = await download(materialId, versionId === 'original' ? undefined : versionId)
      setDone(result)
    } catch (err) {
      setError(userMessage(err as ApiError, '导出 QTI 包失败，请稍后重试'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="qti-export">
      <button
        type="button"
        className="btn btn-sm"
        disabled={busy}
        title="按 IMS QTI 2.2.4 打包：imsmanifest.xml + assessmentItem，另附容错集与待确认项"
        onClick={() => void run()}
      >
        {busy ? '正在生成…' : `导出 QTI 2.2（V${ordinal}）`}
      </button>
      {done && (
        <span className="qti-export-note" role="status">
          已下载 <span className="mono">{done.filename}</span>
          {done.reviewCount > 0 ? (
            <>
              ，包内 review.txt 有 {done.reviewCount} 项待人工确认的判分口径
            </>
          ) : (
            <>，容错集无待确认项</>
          )}
        </span>
      )}
      {error && (
        <span className="comment-error qti-export-error" role="alert">
          {error}
        </span>
      )}
    </div>
  )
}
