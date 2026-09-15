import { useCallback, useEffect, useState, type ChangeEvent, type DragEvent, type FormEvent } from 'react'
import {
  AlertOctagon,
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Inbox,
  Mail,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Trash2,
  Upload,
  X,
  XCircle,
  Zap,
} from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { apiFetch, apiForm } from '@/lib/utils'

type MailboxStats = {
  total: number
  capacity: number
  used: number
  reserved?: number
  remaining: number
  exhausted: number
  disabled?: number
  exhaustion_rate?: number
  alert_level?: 'critical' | 'warning' | 'healthy'
}

type MailboxItem = {
  id: number
  email: string
  use_count: number
  max_uses: number
  status: string
  source_format: string
  updated_at: string
}

type Message = {
  id: string
  subject?: string
  bodyPreview?: string
  body?: string
  receivedDateTime?: string
  from?: { emailAddress?: { name?: string; address?: string } } | string
  to?: string
  toRecipients?: Array<{ emailAddress?: { name?: string; address?: string } }>
}

function formatTime(value?: string) {
  if (!value) return ''
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString()
}

function fromLabel(msg: Message): string {
  const f = msg.from as any
  if (typeof f === 'string') return f
  return f?.emailAddress?.address || f?.emailAddress?.name || ''
}

function ImportMailboxDialog({
  onClose,
  onSuccess,
}: {
  onClose: () => void
  onSuccess: () => void
}) {
  const [inputMode, setInputMode] = useState<'text' | 'file'>('text')
  const [text, setText] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [fileName, setFileName] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [resultMsg, setResultMsg] = useState('')

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    const selected = event.target.files?.[0]
    if (selected) {
      setFile(selected)
      setFileName(selected.name)
      setError('')
    }
  }

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    const dropped = event.dataTransfer.files?.[0]
    if (dropped) {
      setFile(dropped)
      setFileName(dropped.name)
      setError('')
    }
  }

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    setResultMsg('')
    setSubmitting(true)

    try {
      if (inputMode === 'text') {
        const trimmed = text.trim()
        if (!trimmed) {
          setError('请输入邮箱卡密内容')
          setSubmitting(false)
          return
        }
        const res = await apiFetch('/microsoft-mailboxes/import-text', {
          method: 'POST',
          body: JSON.stringify({ text: trimmed }),
        })
        setResultMsg(`导入完成！解析到 ${res.parsed} 个，新增 ${res.created} 个，更新 ${res.updated} 个`)
        setTimeout(() => {
          onSuccess()
        }, 1200)
      } else {
        if (!file) {
          setError('请选择要上传的文件')
          setSubmitting(false)
          return
        }
        const formData = new FormData()
        formData.append('files', file)
        const res = await apiForm('/microsoft-mailboxes/import', formData)
        setResultMsg(`导入完成！解析到 ${res.parsed} 个，新增 ${res.created} 个，更新 ${res.updated} 个`)
        setTimeout(() => {
          onSuccess()
        }, 1200)
      }
    } catch (err: any) {
      setError(err?.message || '导入失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm">
      <div className="w-full max-w-lg rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-6 shadow-2xl">
        <div className="flex items-center justify-between pb-3 border-b border-[var(--border)]">
          <h2 className="text-base font-semibold text-[var(--text-primary)]">📥 导入微软邮箱卡密</h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded p-1 text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {resultMsg ? (
          <div className="my-6 rounded-md border border-emerald-500/30 bg-emerald-500/10 p-4 text-center text-sm text-emerald-300">
            {resultMsg}
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="mt-4 space-y-4">
            <div className="flex border-b border-[var(--border)] text-sm">
              <button
                type="button"
                onClick={() => setInputMode('text')}
                className={`pb-2 font-medium transition-colors ${
                  inputMode === 'text'
                    ? 'border-b-2 border-sky-500 text-sky-400'
                    : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'
                }`}
              >
                ✏️ 文本粘贴
              </button>
              <button
                type="button"
                onClick={() => setInputMode('file')}
                className={`ml-6 pb-2 font-medium transition-colors ${
                  inputMode === 'file'
                    ? 'border-b-2 border-sky-500 text-sky-400'
                    : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'
                }`}
              >
                📄 文件上传 (.txt)
              </button>
            </div>

            {inputMode === 'text' ? (
              <div>
                <textarea
                  value={text}
                  onChange={e => setText(e.target.value)}
                  placeholder={`每行一个邮箱，支持如下标准卡密格式：\n\n邮箱----密码----client_id----refresh_token\n邮箱----密码----client_id----refresh_token----tenant_id`}
                  rows={8}
                  className="w-full rounded-md border border-[var(--border)] bg-transparent p-3 font-mono text-xs text-[var(--text-primary)] focus:border-sky-500 focus:outline-none"
                />
              </div>
            ) : (
              <div
                onDragOver={e => e.preventDefault()}
                onDrop={handleDrop}
                className="flex flex-col items-center justify-center rounded-lg border-2 border-dashed border-[var(--border)] bg-[var(--bg-pane)]/30 p-6 text-center hover:border-sky-500/50"
              >
                <Upload className="h-8 w-8 text-[var(--text-muted)]" />
                <p className="mt-2 text-sm text-[var(--text-primary)]">
                  {fileName ? (
                    <span className="font-semibold text-emerald-400">{fileName}</span>
                  ) : (
                    '拖拽 .txt 文件到此处，或点击浏览上传'
                  )}
                </p>
                <label className="mt-3 cursor-pointer">
                  <span className="rounded-md border border-[var(--border)] bg-[var(--bg-card)] px-3 py-1.5 text-xs font-medium text-[var(--text-primary)] hover:bg-[var(--bg-hover)]">
                    选择文件
                  </span>
                  <input
                    type="file"
                    accept=".txt"
                    onChange={handleFileChange}
                    className="hidden"
                  />
                </label>
              </div>
            )}

            <div className="rounded-md border border-[var(--border)] bg-[var(--bg-pane)]/20 p-3 text-xs text-[var(--text-muted)]">
              💡 导入后的微软母体邮箱将自动纳入邮箱池（每个母体邮箱默认最多裂变支持 6 个 OpenAI 账号），并支持免密收信与一键授权探活。
            </div>

            {error ? (
              <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-400">
                {error}
              </div>
            ) : null}

            <div className="flex justify-end gap-2 pt-2">
              <Button type="button" variant="outline" onClick={onClose}>
                取消
              </Button>
              <Button type="submit" disabled={submitting || (inputMode === 'text' ? !text.trim() : !file)}>
                {submitting ? '导入中…' : '确认导入'}
              </Button>
            </div>
          </form>
        )}
      </div>
    </div>
  )
}

export default function MicrosoftMailboxes() {
  const [stats, setStats] = useState<MailboxStats | null>(null)
  const [mailboxes, setMailboxes] = useState<MailboxItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [listLoading, setListLoading] = useState(false)

  const [query, setQuery] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [queryEmail, setQueryEmail] = useState('')
  const [msgLoading, setMsgLoading] = useState(false)
  const [error, setError] = useState('')

  const [showImport, setShowImport] = useState(false)
  const [testingEmail, setTestingEmail] = useState<string | null>(null)
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; msg: string }>>({})
  const [deletingEmail, setDeletingEmail] = useState<string | null>(null)
  const [batchTesting, setBatchTesting] = useState(false)
  const [batchTestSummary, setBatchTestSummary] = useState<{
    tested: number
    valid: number
    invalid: number
  } | null>(null)
  const [clearingDisabled, setClearingDisabled] = useState(false)

  const loadStats = () => {
    apiFetch('/microsoft-mailboxes/stats')
      .then((d: MailboxStats) => setStats(d))
      .catch((e: any) => setError(e?.message || '加载统计失败'))
  }

  const loadMailboxes = useCallback(async () => {
    setListLoading(true)
    try {
      const params = new URLSearchParams({
        page: String(page),
        page_size: String(pageSize),
      })
      if (debouncedSearch.trim()) params.set('search', debouncedSearch.trim())
      if (statusFilter) params.set('status', statusFilter)
      const data = await apiFetch(`/microsoft-mailboxes?${params}`)
      setMailboxes(Array.isArray(data?.items) ? data.items : [])
      setTotal(Number(data?.total || 0))
    } catch (err: any) {
      setError(err?.message || '加载邮箱列表失败')
    } finally {
      setListLoading(false)
    }
  }, [debouncedSearch, statusFilter, page, pageSize])

  useEffect(() => {
    loadStats()
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search), 350)
    return () => window.clearTimeout(timer)
  }, [search])

  useEffect(() => {
    setPage(1)
  }, [debouncedSearch, statusFilter, pageSize])

  useEffect(() => {
    void loadMailboxes()
  }, [loadMailboxes])

  const searchMessages = async (event?: FormEvent, targetEmail?: string) => {
    if (event) event.preventDefault()
    const email = (targetEmail || query).trim()
    if (!email) return
    setMsgLoading(true)
    setError('')
    try {
      const d = await apiFetch(`/microsoft-mailboxes/${encodeURIComponent(email)}/messages`)
      setMessages(d.messages || [])
      setQueryEmail(d.email || email)
      if (targetEmail) setQuery(targetEmail)
    } catch (err: any) {
      setMessages([])
      setQueryEmail('')
      setError(err?.message || '查询邮件失败')
    } finally {
      setMsgLoading(false)
    }
  }

  const handleTestMailbox = async (email: string) => {
    setTestingEmail(email)
    try {
      const res = await apiFetch(`/microsoft-mailboxes/${encodeURIComponent(email)}/test`, {
        method: 'POST',
      })
      if (res.ok) {
        setTestResults(prev => ({ ...prev, [email]: { ok: true, msg: '授权有效' } }))
      } else {
        setTestResults(prev => ({ ...prev, [email]: { ok: false, msg: res.error || '授权失败' } }))
      }
      loadStats()
    } catch (err: any) {
      setTestResults(prev => ({ ...prev, [email]: { ok: false, msg: err?.message || '探活异常' } }))
    } finally {
      setTestingEmail(null)
    }
  }

  const handleBatchTest = async () => {
    if (batchTesting) return
    setBatchTesting(true)
    setError('')
    setBatchTestSummary(null)
    try {
      const res = await apiFetch('/microsoft-mailboxes/batch-test', {
        method: 'POST',
        body: JSON.stringify({ concurrency: 10 }),
      })
      if (res.ok) {
        setBatchTestSummary({
          tested: res.tested,
          valid: res.valid,
          invalid: res.invalid,
        })
        if (res.results) {
          const mapped: Record<string, { ok: boolean; msg: string }> = {}
          for (const [email, r] of Object.entries<any>(res.results)) {
            mapped[email] = { ok: r.ok, msg: r.message }
          }
          setTestResults(prev => ({ ...prev, ...mapped }))
        }
        if (res.stats) {
          setStats(res.stats)
        }
        await loadMailboxes()
      }
    } catch (err: any) {
      setError(err?.message || '批量测活失败')
    } finally {
      setBatchTesting(false)
    }
  }

  const handleClearDisabled = async () => {
    if (!window.confirm(`确定清理所有已失效/禁用的母体邮箱（共 ${stats?.disabled || 0} 个）？`)) return
    setClearingDisabled(true)
    try {
      const res = await apiFetch('/microsoft-mailboxes/clear-disabled', {
        method: 'POST',
      })
      if (res.stats) {
        setStats(res.stats)
      }
      await loadMailboxes()
    } catch (err: any) {
      setError(err?.message || '清理失效邮箱失败')
    } finally {
      setClearingDisabled(false)
    }
  }

  const handleDeleteMailbox = async (email: string) => {
    if (!window.confirm(`确定从邮箱池中删除「${email}」？`)) return
    setDeletingEmail(email)
    try {
      await apiFetch(`/microsoft-mailboxes/${encodeURIComponent(email)}`, {
        method: 'DELETE',
      })
      await loadMailboxes()
      loadStats()
    } catch (err: any) {
      setError(err?.message || '删除邮箱失败')
    } finally {
      setDeletingEmail(null)
    }
  }

  const remainingColor =
    (stats?.remaining ?? 0) === 0
      ? 'text-rose-400'
      : (stats?.remaining ?? 0) <= 10
      ? 'text-amber-400'
      : 'text-emerald-400'

  const statCards = [
    { label: '邮箱总数', value: stats?.total ?? '-', key: 'total' },
    { label: '剩余可用裂变', value: stats?.remaining ?? '-', key: 'remaining', color: remainingColor },
    { label: '裂变用量', value: `${stats?.used ?? 0} / ${stats?.capacity ?? 0}`, key: 'used' },
    { label: '池耗尽率', value: stats?.exhaustion_rate != null ? `${stats.exhaustion_rate}%` : '-', key: 'exhaustion_rate' },
    { label: '已耗尽邮箱', value: stats?.exhausted ?? '-', key: 'exhausted' },
    {
      label: '失效/已禁用',
      value: stats?.disabled ?? '-',
      key: 'disabled',
      color: (stats?.disabled ?? 0) > 0 ? 'text-rose-400' : undefined,
    },
  ]

  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  return (
    <div className="space-y-4">
      {showImport ? (
        <ImportMailboxDialog
          onClose={() => setShowImport(false)}
          onSuccess={() => {
            setShowImport(false)
            loadStats()
            void loadMailboxes()
          }}
        />
      ) : null}

      {/* 容量告警与余量预警 */}
      {stats?.alert_level === 'critical' ? (
        <div className="flex flex-col gap-3 rounded-xl border border-rose-500/40 bg-rose-500/10 p-4 sm:flex-row sm:items-center sm:justify-between shadow-lg shadow-rose-950/20">
          <div className="flex items-center gap-3">
            <div className="rounded-full bg-rose-500/20 p-2 text-rose-400 shrink-0">
              <AlertOctagon className="h-5 w-5" />
            </div>
            <div>
              <div className="font-semibold text-rose-300">
                🚨 邮箱池容量告警：可用裂变次数已耗尽（剩余 0 次）
              </div>
              <div className="mt-0.5 text-xs text-rose-200/80">
                当前没有可用于注册的母体邮箱，自动注册服务将无法分配新邮箱。请立即导入微软邮箱卡密！
              </div>
            </div>
          </div>
          <Button size="sm" className="bg-rose-600 hover:bg-rose-500 text-white shrink-0" onClick={() => setShowImport(true)}>
            <Plus className="mr-1.5 h-4 w-4" /> 立即补充邮箱
          </Button>
        </div>
      ) : stats?.alert_level === 'warning' ? (
        <div className="flex flex-col gap-3 rounded-xl border border-amber-500/40 bg-amber-500/10 p-4 sm:flex-row sm:items-center sm:justify-between shadow-lg shadow-amber-950/20">
          <div className="flex items-center gap-3">
            <div className="rounded-full bg-amber-500/20 p-2 text-amber-400 shrink-0">
              <AlertTriangle className="h-5 w-5" />
            </div>
            <div>
              <div className="font-semibold text-amber-300">
                ⚠️ 邮箱池余量偏低预警：剩余可用裂变次数仅剩 {stats.remaining} 次
              </div>
              <div className="mt-0.5 text-xs text-amber-200/80">
                池中可用裂变额度即将耗尽，请及时补充微软邮箱卡密，以免影响批量注册任务。
              </div>
            </div>
          </div>
          <Button size="sm" variant="outline" className="border-amber-500/50 text-amber-300 hover:bg-amber-500/20 shrink-0" onClick={() => setShowImport(true)}>
            <Plus className="mr-1.5 h-4 w-4" /> 补充邮箱
          </Button>
        </div>
      ) : null}

      {/* 批量测活结果通知 */}
      {batchTestSummary ? (
        <div className="flex items-center justify-between rounded-xl border border-sky-500/40 bg-sky-500/10 p-4 text-sm text-sky-200">
          <div className="flex items-center gap-2.5">
            <Zap className="h-4 w-4 text-sky-400 shrink-0" />
            <span>
              ⚡ 批量测活完成：共检测 <strong>{batchTestSummary.tested}</strong> 个邮箱，正常有效 <strong className="text-emerald-400">{batchTestSummary.valid}</strong> 个，失效/异常 <strong className="text-rose-400">{batchTestSummary.invalid}</strong> 个（失效邮箱已自动标记为禁用隔离）。
            </span>
          </div>
          <button
            type="button"
            onClick={() => setBatchTestSummary(null)}
            className="rounded p-1 text-sky-300/70 hover:bg-sky-500/20 hover:text-sky-200"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      ) : null}

      {/* 头部统计与操作 */}
      <Card className="border border-[var(--border)] bg-[var(--bg-pane)]/40 p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <div className="flex items-center gap-2">
              <Inbox className="h-5 w-5 text-sky-400" />
              <h1 className="text-xl font-semibold text-[var(--text-primary)]">微软邮箱池</h1>
            </div>
            <p className="mt-1 text-sm text-[var(--text-muted)]">
              集中管理用于裂变注册与长效收信的微软母体邮箱，支持 OAuth 授权探活与即时邮件查验。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              className="border-amber-500/40 text-amber-400 hover:bg-amber-500/10 hover:text-amber-300"
              onClick={handleBatchTest}
              disabled={batchTesting || listLoading}
            >
              <Zap className={`mr-1.5 h-3.5 w-3.5 ${batchTesting ? 'animate-pulse text-amber-300' : ''}`} />
              {batchTesting ? '正在批量测活…' : '⚡ 一键批量测活'}
            </Button>
            {stats && (stats.disabled ?? 0) > 0 ? (
              <Button
                size="sm"
                variant="outline"
                className="border-rose-500/40 text-rose-400 hover:bg-rose-500/10 hover:text-rose-300"
                onClick={handleClearDisabled}
                disabled={clearingDisabled || listLoading}
              >
                <Trash2 className="mr-1.5 h-3.5 w-3.5" />
                {clearingDisabled ? '清理中…' : `清理失效邮箱 (${stats.disabled})`}
              </Button>
            ) : null}
            <Button size="sm" onClick={() => setShowImport(true)}>
              <Plus className="mr-1.5 h-4 w-4" /> 导入邮箱卡密
            </Button>
            <Button size="sm" variant="outline" onClick={() => { loadStats(); void loadMailboxes(); }}>
              <RefreshCw className="mr-1.5 h-3.5 w-3.5" /> 刷新
            </Button>
          </div>
        </div>

        <div className="mt-5 grid gap-3 grid-cols-2 sm:grid-cols-3 lg:grid-cols-6">
          {statCards.map(card => (
            <div key={card.key} className="rounded-md border border-[var(--border)] bg-[var(--bg-pane)]/50 px-4 py-3">
              <div className="text-xs text-[var(--text-muted)]">{card.label}</div>
              <div className={`mt-1 text-2xl font-semibold ${card.color || 'text-[var(--text-primary)]'}`}>
                {card.value}
              </div>
            </div>
          ))}
        </div>
      </Card>

      {/* 邮箱池列表 */}
      <Card className="overflow-hidden border border-[var(--border)] bg-[var(--bg-card)] p-0">
        <div className="flex flex-col gap-3 border-b border-[var(--border)] px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-1 flex-wrap items-center gap-2">
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="搜索邮箱地址"
              className="w-full max-w-xs rounded-md border border-[var(--border)] bg-transparent px-3 py-1.5 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)]"
            />
            <select
              value={statusFilter}
              onChange={e => setStatusFilter(e.target.value)}
              className="rounded-md border border-[var(--border)] bg-transparent px-3 py-1.5 text-sm text-[var(--text-primary)]"
            >
              <option value="">全部状态</option>
              <option value="available">可用 (未满 6 次)</option>
              <option value="exhausted">已耗尽 (满 6 次)</option>
              <option value="disabled">已禁用</option>
            </select>
          </div>
          <div className="text-xs text-[var(--text-muted)]">
            共 {total} 个母体邮箱
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full min-w-[700px] text-sm">
            <thead className="bg-[var(--bg-pane)]/60 text-left text-xs uppercase tracking-wide text-[var(--text-muted)]">
              <tr>
                <th className="px-4 py-3 font-medium">母体邮箱</th>
                <th className="px-4 py-3 font-medium">裂变用量</th>
                <th className="px-4 py-3 font-medium">状态</th>
                <th className="px-4 py-3 font-medium">授权状态</th>
                <th className="px-4 py-3 font-medium">更新时间</th>
                <th className="px-4 py-3 font-medium text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border)]">
              {mailboxes.map(mb => {
                const test = testResults[mb.email]
                return (
                  <tr key={mb.id} className="hover:bg-[var(--bg-hover)]/60">
                    <td className="px-4 py-3 font-mono text-[var(--text-primary)]">
                      <div className="flex items-center gap-1.5">
                        <Mail className="h-3.5 w-3.5 text-sky-400" />
                        <span>{mb.email}</span>
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <span className={`font-mono text-xs font-semibold ${
                          mb.status === 'disabled'
                            ? 'text-zinc-500'
                            : mb.use_count >= mb.max_uses
                            ? 'text-rose-400'
                            : 'text-emerald-400'
                        }`}>
                          {mb.use_count} / {mb.max_uses}
                        </span>
                        <div className="h-1.5 w-16 overflow-hidden rounded-full bg-[var(--border)]">
                          <div
                            className={`h-full ${
                              mb.status === 'disabled'
                                ? 'bg-zinc-600'
                                : mb.use_count >= mb.max_uses
                                ? 'bg-rose-500'
                                : 'bg-emerald-500'
                            }`}
                            style={{ width: `${Math.min(100, (mb.use_count / mb.max_uses) * 100)}%` }}
                          />
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${
                        mb.status === 'disabled'
                          ? 'bg-rose-500/15 text-rose-400'
                          : mb.status === 'available'
                          ? 'bg-emerald-500/10 text-emerald-400'
                          : 'bg-amber-500/10 text-amber-400'
                      }`}>
                        {mb.status === 'disabled' ? '已禁用' : mb.status === 'available' ? '可用' : '已耗尽'}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-xs">
                      {test ? (
                        test.ok ? (
                          <span className="inline-flex items-center gap-1 text-emerald-400">
                            <CheckCircle2 className="h-3.5 w-3.5" /> 正常
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-rose-400" title={test.msg}>
                            <XCircle className="h-3.5 w-3.5" /> 失效
                          </span>
                        )
                      ) : mb.status === 'disabled' ? (
                        <span className="inline-flex items-center gap-1 text-rose-400/80">
                          <XCircle className="h-3.5 w-3.5" /> 已隔离
                        </span>
                      ) : (
                        <span className="text-[var(--text-muted)]">未测</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-xs text-[var(--text-secondary)]">
                      {formatTime(mb.updated_at)}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-1.5">
                        <button
                          type="button"
                          onClick={() => void handleTestMailbox(mb.email)}
                          disabled={testingEmail === mb.email}
                          title="测试微软 Graph OAuth 授权"
                          className="inline-flex items-center gap-1 rounded border border-[var(--border)] bg-[var(--bg-pane)]/40 px-2 py-1 text-xs text-sky-400 hover:bg-sky-500/10 disabled:opacity-50"
                        >
                          <ShieldCheck className="h-3.5 w-3.5" />
                          {testingEmail === mb.email ? '测试中' : '测授权'}
                        </button>
                        <button
                          type="button"
                          onClick={() => void searchMessages(undefined, mb.email)}
                          title="查看该邮箱的最近收件"
                          className="inline-flex items-center gap-1 rounded border border-[var(--border)] bg-[var(--bg-pane)]/40 px-2 py-1 text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                        >
                          <Search className="h-3.5 w-3.5" />
                          查信
                        </button>
                        <button
                          type="button"
                          onClick={() => void handleDeleteMailbox(mb.email)}
                          disabled={deletingEmail === mb.email}
                          title="删除邮箱"
                          className="inline-flex items-center gap-1 rounded border border-red-500/30 bg-red-500/10 px-2 py-1 text-xs text-red-400 hover:bg-red-500/20 disabled:opacity-50"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                          {deletingEmail === mb.email ? '…' : '删除'}
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
              {!listLoading && mailboxes.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-4 py-16 text-center text-[var(--text-muted)]">
                    暂无微软邮箱，请点击上方「导入邮箱卡密」
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--border)] px-4 py-3 text-sm text-[var(--text-muted)]">
          <span>第 {page} / {totalPages} 页</span>
          <div className="flex items-center gap-2">
            <select
              value={pageSize}
              onChange={e => setPageSize(Number(e.target.value))}
              className="rounded-md border border-[var(--border)] bg-transparent px-2 py-1 text-[var(--text-primary)]"
            >
              <option value={20}>20 / 页</option>
              <option value={50}>50 / 页</option>
              <option value={100}>100 / 页</option>
            </select>
            <Button variant="outline" size="sm" disabled={page <= 1 || listLoading} onClick={() => setPage(p => p - 1)}>
              <ChevronLeft className="h-4 w-4" />
            </Button>
            <Button variant="outline" size="sm" disabled={page >= totalPages || listLoading} onClick={() => setPage(p => p + 1)}>
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </Card>

      {/* 邮件查阅区域 */}
      <Card className="border border-[var(--border)] bg-[var(--bg-pane)]/40 p-5">
        <form onSubmit={e => void searchMessages(e)} className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <label className="flex-1">
            <span className="mb-1 block text-xs text-[var(--text-muted)]">实时查验邮箱收件</span>
            <input
              value={query}
              onChange={event => setQuery(event.target.value)}
              placeholder="输入母体邮箱或裂变子邮箱地址，如 xxx@outlook.com 或 xxx+sub1@outlook.com"
              className="w-full rounded-md border border-[var(--border)] bg-transparent px-3 py-2 text-sm text-[var(--text-primary)]"
            />
          </label>
          <Button type="submit" size="sm" disabled={msgLoading || !query.trim()}>
            {msgLoading ? '查询中…' : (
              <>
                <Search className="mr-1.5 h-3.5 w-3.5" /> 查询信件
              </>
            )}
          </Button>
        </form>
        {error ? <div className="mt-3 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-400">{error}</div> : null}
        {queryEmail ? (
          <div className="mt-4">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-medium text-[var(--text-primary)]">{queryEmail} 的收件箱（{messages.length} 封）</h2>
            </div>
            <div className="mt-2 divide-y divide-[var(--border)]">
              {messages.length === 0 ? (
                <p className="py-4 text-center text-sm text-[var(--text-muted)]">暂无信件</p>
              ) : messages.map((msg, idx) => (
                <div key={msg.id || idx} className="flex flex-col gap-1 py-3">
                  <div className="flex items-center gap-2">
                    <Mail className="h-3.5 w-3.5 shrink-0 text-[var(--text-muted)]" />
                    <span className="font-medium text-[var(--text-primary)]">{msg.subject || '(无主题)'}</span>
                    <span className="ml-auto shrink-0 text-xs text-[var(--text-muted)]">{formatTime(msg.receivedDateTime)}</span>
                  </div>
                  <div className="text-xs text-[var(--text-secondary)]">发件人：{fromLabel(msg) || '-'}</div>
                  {(msg.bodyPreview || msg.body) ? (
                    <div className="max-h-24 overflow-y-auto whitespace-pre-wrap break-words rounded bg-[var(--bg-pane)]/40 px-2 py-1.5 text-xs text-[var(--text-muted)]">
                      {(msg.bodyPreview || msg.body || '').slice(0, 500)}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </Card>
    </div>
  )
}
