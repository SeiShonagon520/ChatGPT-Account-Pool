import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { CheckCircle2, CheckSquare, ChevronLeft, ChevronRight, Copy, Download, Mail, Plus, RefreshCw, ShieldCheck, Trash2, Upload, X } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { apiDownload, apiFetch, apiForm, triggerBrowserDownload } from '@/lib/utils'

type AccountListItem = {
  id: number
  email: string
  password: string
  totp_secret: string
  refresh_token_status: string
  has_refresh_token: boolean
  at_expires_at?: number | null
  has_mailbox?: boolean
  mailbox_email?: string
  plan_name?: string
  created_at: string | null
}

type SurvivalStats = {
  platform: string
  alive_accounts: number
  historical_registered_emails: number
  survival_rate: number
}

type CreatedTask = {
  id: string
  title: string
}

type ProxyNode = {
  name: string
  type: string
  alive: boolean | null
  delay: number | null
  last_test: string
  udp: boolean
  selected: boolean
}

type ProxyNodesResponse = {
  available: boolean
  group: string
  selected: string
  nodes: ProxyNode[]
  error: string
}

function formatDate(value: string | null) {
  if (!value) return '-'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function statePill(value: string) {
  const state = String(value || 'unknown').toLowerCase()
  const isYes = state === 'invalid'
  const isNo = state === 'valid'
  const label = isYes
    ? '401'
    : isNo
      ? '正常'
      : state === 'checking'
        ? '校验中'
        : state === 'not_checked'
          ? '未校验'
          : state === 'missing'
            ? '待复验'
            : '未确认'
  const styles = isYes
    ? 'border-red-500/30 bg-red-500/10 text-red-400'
    : isNo
      ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400'
      : 'border-[var(--border)] bg-[var(--bg-pane)] text-[var(--text-muted)]'
  return <span className={`inline-flex min-w-8 justify-center rounded-full border px-2 py-0.5 text-xs ${styles}`}>{label}</span>
}

function formatAtExpiry(exp?: number | null) {
  if (!exp) return null
  const now = Math.floor(Date.now() / 1000)
  const diff = exp - now
  if (diff <= 0) {
    return <span className="inline-flex items-center gap-1 rounded bg-red-500/10 px-1.5 py-0.5 text-[11px] text-red-400 font-mono" title={new Date(exp * 1000).toLocaleString()}>AT已过期</span>
  }
  const days = Math.floor(diff / 86400)
  const hours = Math.floor((diff % 86400) / 3600)
  if (days >= 2) {
    return <span className="inline-flex items-center gap-1 rounded bg-emerald-500/10 px-1.5 py-0.5 text-[11px] text-emerald-400 font-mono" title={new Date(exp * 1000).toLocaleString()}>AT剩 {days}天</span>
  }
  if (days >= 1) {
    return <span className="inline-flex items-center gap-1 rounded bg-amber-500/10 px-1.5 py-0.5 text-[11px] text-amber-400 font-mono" title={new Date(exp * 1000).toLocaleString()}>AT剩 1天{hours}时</span>
  }
  return <span className="inline-flex items-center gap-1 rounded bg-rose-500/10 px-1.5 py-0.5 text-[11px] text-rose-400 font-mono" title={new Date(exp * 1000).toLocaleString()}>AT剩 {hours}时</span>
}

function RegisterDialog({ onClose, onCreated }: { onClose: () => void, onCreated: (task: CreatedTask) => void }) {
  const [count, setCount] = useState('1')
  const [concurrency, setConcurrency] = useState('1')
  const [proxyNodes, setProxyNodes] = useState<ProxyNode[]>([])
  const [proxyLoading, setProxyLoading] = useState(false)
  const [proxyError, setProxyError] = useState('')
  const [proxyMode, setProxyMode] = useState<'pool' | 'dynamic' | 'direct'>('pool')
  const [proxyApiUrl, setProxyApiUrl] = useState('')
  const [mailProvider, setMailProvider] = useState('')
  const [mailProviders, setMailProviders] = useState<Array<any>>([])
  const [splitRegister, setSplitRegister] = useState(true)
  const [harAvailable, setHarAvailable] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [pulse, setPulse] = useState(true)
  const [probeInterval, setProbeInterval] = useState('600')
  const [probeOtpTimeout, setProbeOtpTimeout] = useState('90')
  const [banAfter, setBanAfter] = useState('3')
  const [probeBatch, setProbeBatch] = useState('5')
  const [harCapture, setHarCapture] = useState(false)
  const [harCapture2fa, setHarCapture2fa] = useState(false)
  const [executorType, setExecutorType] = useState<'protocol' | 'headed' | 'headless'>('protocol')

  const loadProxyNodes = useCallback(async (refresh = false) => {
    setProxyLoading(true)
    try {
      const data = await apiFetch(`/proxy-nodes${refresh ? '?refresh=true' : ''}`) as ProxyNodesResponse
      const nodes = Array.isArray(data?.nodes) ? data.nodes : []
      setProxyNodes(nodes)
      setProxyError(data?.available ? '' : (data?.error || 'Mihomo 代理服务不可用'))
    } catch (err: any) {
      setProxyNodes([])
      setProxyError(err?.message || '代理节点读取失败')
    } finally {
      setProxyLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadProxyNodes()
    void Promise.all([
      apiFetch('/provider-settings?provider_type=mailbox').catch(() => []),
      apiFetch('/system/runtime').catch(() => ({ har_capture_available: false })),
    ]).then(([settings, runtime]) => {
      const enabled = Array.isArray(settings) ? settings.filter(item => item?.enabled !== false) : []
      setMailProviders(enabled)
      setMailProvider('')
      setHarAvailable(Boolean(runtime?.har_capture_available))
    })
  }, [loadProxyNodes])

  useEffect(() => {
    if (!harAvailable) setHarCapture(false)
  }, [harAvailable])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const numericCount = Number(count)
    const numericConcurrency = Number(concurrency)
    if (!Number.isInteger(numericCount) || numericCount < 0) {
      setError('注册数量必须是大于等于 0 的整数；0 代表无限注册。')
      return
    }
    if (!Number.isInteger(numericConcurrency) || numericConcurrency < 1 || numericConcurrency > 50) {
      setError('并发必须在 1 到 50 之间。')
      return
    }
    if (!mailProvider) {
      setError('请选择注册邮箱服务。')
      return
    }
    const numericProbeInterval = Number(probeInterval)
    const numericProbeOtpTimeout = Number(probeOtpTimeout)
    const numericBanAfter = Number(banAfter)
    const numericProbeBatch = Number(probeBatch)
    if (
      !Number.isInteger(numericProbeInterval) || numericProbeInterval < 30 ||
      !Number.isInteger(numericProbeOtpTimeout) || numericProbeOtpTimeout < 20 ||
      !Number.isInteger(numericBanAfter) || numericBanAfter < 1 ||
      !Number.isInteger(numericProbeBatch) || numericProbeBatch < 1 || numericProbeBatch > 20
    ) {
      setError('脉冲探测参数无效：探测间隔≥30s，验证码超时≥20s，封禁阈值≥1，探测批量 1-20。')
      return
    }
    const useDynamic = proxyMode === 'dynamic' && proxyApiUrl.trim().length > 0
    if (proxyMode === 'dynamic' && !proxyApiUrl.trim()) {
      setError('动态 IP 模式需要填写代理提取 API')
      return
    }
    setSubmitting(true)
    setError('')
    try {
      const task = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          count: harCapture ? 1 : numericCount,
          concurrency: numericConcurrency,
          proxy: null,
          proxy_node: null,
          proxy_pool: harCapture ? false : proxyMode === 'pool',
          proxy_api_url: useDynamic ? proxyApiUrl.trim() : null,
          executor_type: harCapture ? 'protocol' : executorType,
          pulse: harCapture ? false : (proxyMode === 'pool' ? pulse : false),
          pulse_interval_seconds: 0,
          probe_interval_seconds: numericProbeInterval,
          probe_otp_timeout_seconds: numericProbeOtpTimeout,
          ban_after_consecutive_no_email: numericBanAfter,
          probe_batch_size: numericProbeBatch,
          har_capture: harCapture,
          har_capture_2fa: harCapture && harCapture2fa,
          split_register: splitRegister,
          extra: {
            mail_provider: mailProvider,
            bind_totp_2fa: true,
          },
        }),
      })
      onCreated({
        id: task.task_id,
        title: harCapture
          ? harCapture2fa
            ? 'camoufox 抓包任务已创建（注册 + 绑定 2FA，请在浏览器手动操作）'
            : 'camoufox 抓包任务已创建（请在浏览器手动注册）'
          : proxyMode === 'dynamic' ? `${executorType === 'protocol' ? '协议' : executorType === 'headed' ? '有头浏览器' : '无头浏览器'}注册任务已创建（动态 IP）`
            : proxyMode === 'pool' ? `${executorType === 'protocol' ? '协议' : executorType === 'headed' ? '有头浏览器' : '无头浏览器'}注册任务已创建（Mihomo 代理池）`
              : `${executorType === 'protocol' ? '协议' : executorType === 'headed' ? '有头浏览器' : '无头浏览器'}注册任务已创建（直连）`,
      })
    } catch (err: any) {
      setError(err?.message || '创建注册任务失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <form onSubmit={submit} className="max-h-[calc(100vh-2rem)] w-full max-w-lg overflow-y-auto rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-5 shadow-xl">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">创建注册任务</h2>
            <p className="mt-1 text-xs text-[var(--text-muted)]">选择注册方式、邮箱来源和代理后创建注册任务。浏览器模式走 camoufox，密码 + 邮箱验证码 + 姓名生日全自动。</p>
          </div>
          <button type="button" onClick={onClose} className="rounded p-1 text-[var(--text-muted)] hover:bg-[var(--bg-hover)]"><X className="h-4 w-4" /></button>
        </div>
        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          <label className="grid gap-1.5 text-sm text-[var(--text-secondary)]">
            注册方式
            <select value={executorType} onChange={event => setExecutorType(event.target.value as 'protocol' | 'headed' | 'headless')} disabled={harCapture} className="rounded-md border border-[var(--border)] bg-[var(--bg-card)] px-3 py-2 text-sm text-[var(--text-primary)]">
              <option value="protocol">协议注册（curl 模拟）</option>
              <option value="headed">浏览器注册（有头 camoufox）</option>
              <option value="headless">浏览器注册（无头 camoufox）</option>
            </select>
            <span className="text-xs text-[var(--text-muted)]">有头模式可在服务器 VNC（:6080）实时观察浏览器操作。</span>
          </label>
          <label className="grid gap-1.5 text-sm text-[var(--text-secondary)]">
            注册数量
            <input value={count} onChange={event => setCount(event.target.value)} inputMode="numeric" className="rounded-md border border-[var(--border)] bg-transparent px-3 py-2 text-[var(--text-primary)]" />
            <span className="text-xs text-[var(--text-muted)]">0 = 无限，直到在任务页停止。</span>
          </label>
          <label className="grid gap-1.5 text-sm text-[var(--text-secondary)]">
            并发（最高 50）
            <input value={concurrency} onChange={event => setConcurrency(event.target.value)} inputMode="numeric" className="rounded-md border border-[var(--border)] bg-transparent px-3 py-2 text-[var(--text-primary)]" />
          </label>
          <label className="grid gap-1.5 text-sm text-[var(--text-secondary)] sm:col-span-2">
            注册邮箱服务
            <select required value={mailProvider} onChange={event => setMailProvider(event.target.value)} className="rounded-md border border-[var(--border)] bg-[var(--bg-card)] px-3 py-2 text-sm text-[var(--text-primary)]">
              <option value="" disabled>请选择邮箱服务</option>
              {mailProviders.map(provider => <option key={provider.provider_key} value={provider.provider_key}>{provider.provider_key === 'local_ms_pool' ? '微软邮箱池（每个邮箱注册 6 次）' : provider.display_name || provider.provider_key}</option>)}
              {!mailProviders.some(provider => provider.provider_key === 'local_ms_pool') && <option value="local_ms_pool" disabled>微软邮箱池（请先在设置中配置）</option>}
            </select>
            {mailProvider === 'local_ms_pool' && (
              <div className="flex flex-col gap-2">
                <span className="text-xs text-[var(--text-muted)]">每个微软邮箱最多可注册 {splitRegister ? 6 : 1} 次。</span>
                <label className="flex items-center gap-2 text-xs text-[var(--text-secondary)]">
                  <input type="checkbox" checked={splitRegister} onChange={event => setSplitRegister(event.target.checked)} className="accent-sky-500" />
                  分裂注册（每个父邮箱拆分 6 个子地址 +reg1~+reg6 注册）
                </label>
              </div>
            )}
          </label>
          <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-400 sm:col-span-2">
            自动注册固定要求远端密码，并在成功后绑定和激活 TOTP 2FA；任一步失败都不会保存账号。
          </div>
          {harAvailable && <div className="grid gap-2 rounded-md border border-[var(--border)] bg-[var(--bg-pane)]/40 p-3 sm:col-span-2">
            <label className="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
              <input type="checkbox" checked={harCapture} onChange={event => setHarCapture(event.target.checked)} className="accent-sky-500" />
              camoufox 抓包模式（手动注册，抓取 HAR）
            </label>
            {harCapture ? (
              <>
                <label className="flex items-center gap-2 text-xs text-[var(--text-muted)]">
                  <input type="checkbox" checked={harCapture2fa} onChange={event => setHarCapture2fa(event.target.checked)} className="accent-sky-500" />
                  注册完成后继续绑定 2FA（两步验证）
                </label>
                <span className="text-xs text-[var(--text-muted)]">
                  打开 camoufox 真实浏览器到 ChatGPT 注册页并录制 HAR；请在浏览器里手动完成注册，勾选 2FA 时注册完成后不自动跳转，你可继续打开 OpenAI 安全设置页手动绑定两步验证。完成后关闭浏览器窗口，HAR 自动保存。启用后自动关闭脉冲/动态 IP。
                </span>
              </>
            ) : null}
          </div>}
          <label className="grid gap-1.5 text-sm text-[var(--text-secondary)] sm:col-span-2">
            注册代理
            <select value={proxyMode} onChange={event => setProxyMode(event.target.value as 'pool' | 'dynamic' | 'direct')} className="rounded-md border border-[var(--border)] bg-[var(--bg-card)] px-3 py-2 text-sm text-[var(--text-primary)]">
              <option value="pool">Mihomo 代理池（自动选择节点）</option>
              <option value="dynamic">动态 IP（轮换住宅代理）</option>
              <option value="direct">无（本机直连）</option>
            </select>
          </label>
          {proxyMode === 'dynamic' ? (
            <div className="grid gap-2 rounded-md border border-[var(--border)] bg-[var(--bg-pane)]/40 p-3 sm:col-span-2">
              <label className="grid gap-1 text-xs text-[var(--text-muted)]">
                代理提取 API
                <input value={proxyApiUrl} onChange={event => setProxyApiUrl(event.target.value)} placeholder="http://us.rrp.bestgo.work:8089/gen?zone=...&sessType=rotating..." className="rounded-md border border-[var(--border)] bg-transparent px-3 py-2 text-sm text-[var(--text-primary)]" />
              </label>
              <span className="text-xs text-[var(--text-muted)]">
                每个 worker 从该 API 获取一个新的轮换住宅 IP；遇到 Cloudflare 挑战自动更换 IP。启用后自动关闭脉冲注册。
              </span>
            </div>
          ) : null}
          {proxyMode === 'pool' && <div className="grid gap-2 sm:col-span-2">
            <div className="flex items-center justify-between gap-3">
              <span className="text-sm text-[var(--text-secondary)]">Mihomo 代理池状态</span>
              <button
                type="button"
                onClick={() => void loadProxyNodes(true)}
                disabled={proxyLoading}
                className="inline-flex items-center text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)] disabled:opacity-50"
              >
                <RefreshCw className={`mr-1 h-3.5 w-3.5 ${proxyLoading ? 'animate-spin' : ''}`} />
                测速刷新
              </button>
            </div>
            <div className="rounded-md border border-[var(--border)] bg-[var(--bg-pane)]/50 px-3 py-2 text-xs text-[var(--text-muted)]">
              {proxyLoading ? '正在读取节点…' : proxyNodes.length > 0 ? `已发现 ${proxyNodes.length} 个节点，注册时自动按负载分配。` : '未发现可用节点，请先在设置中配置 Mihomo 订阅。'}
            </div>
            {proxyError ? <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">{proxyError}</div> : null}
          </div>}
          {proxyMode === 'pool' ? (
            <div className="grid gap-3 rounded-md border border-[var(--border)] bg-[var(--bg-pane)]/40 p-3 sm:col-span-2">
              <label className="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
                <input type="checkbox" checked={pulse} onChange={event => setPulse(event.target.checked)} className="accent-sky-500" />
                脉冲注册（每波所有健康节点并发；节点 IP 被封自动暂停并定时探测恢复）
              </label>
              {pulse ? (
                <div className="grid gap-3 sm:grid-cols-4">
                  <label className="grid gap-1 text-xs text-[var(--text-muted)]">
                    探测间隔（秒）
                    <input value={probeInterval} onChange={event => setProbeInterval(event.target.value)} inputMode="numeric" className="rounded-md border border-[var(--border)] bg-transparent px-2 py-1.5 text-sm text-[var(--text-primary)]" />
                  </label>
                  <label className="grid gap-1 text-xs text-[var(--text-muted)]">
                    探测验证码超时（秒）
                    <input value={probeOtpTimeout} onChange={event => setProbeOtpTimeout(event.target.value)} inputMode="numeric" className="rounded-md border border-[var(--border)] bg-transparent px-2 py-1.5 text-sm text-[var(--text-primary)]" />
                  </label>
                  <label className="grid gap-1 text-xs text-[var(--text-muted)]">
                    连续未收码封禁阈值
                    <input value={banAfter} onChange={event => setBanAfter(event.target.value)} inputMode="numeric" className="rounded-md border border-[var(--border)] bg-transparent px-2 py-1.5 text-sm text-[var(--text-primary)]" />
                  </label>
                  <label className="grid gap-1 text-xs text-[var(--text-muted)]">
                    每轮探测批量
                    <input value={probeBatch} onChange={event => setProbeBatch(event.target.value)} inputMode="numeric" className="rounded-md border border-[var(--border)] bg-transparent px-2 py-1.5 text-sm text-[var(--text-primary)]" />
                  </label>
                </div>
              ) : (
                <span className="text-xs text-[var(--text-muted)]">关闭后按现有并发滚动注册，不做节点封禁 / 探测。</span>
              )}
            </div>
          ) : null}
        </div>
        {error ? <div className="mt-4 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</div> : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onClose}>取消</Button>
          <Button type="submit" disabled={submitting}>{submitting ? '创建中…' : '开始注册'}</Button>
        </div>
      </form>
    </div>
  )
}

function ImportAccountsDialog({
  onClose,
  onSuccess,
  proxyNodes,
}: {
  onClose: () => void
  onSuccess: (task?: CreatedTask) => void
  proxyNodes: ProxyNode[]
}) {
  const [inputMode, setInputMode] = useState<'text' | 'file'>('text')
  const [text, setText] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [fileName, setFileName] = useState('')
  const [autoCheck, setAutoCheck] = useState(false)
  const [concurrency, setConcurrency] = useState('50')
  const [proxyNode, setProxyNode] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<{
    ok: boolean
    received: number
    parsed: number
    created: number
    updated: number
    mailboxes_saved: number
    failed: number
    task?: { task_id?: string }
  } | null>(null)

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = e.target.files?.[0]
    if (selected) {
      setFile(selected)
      setFileName(selected.name)
      setError('')
    }
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    const dropped = e.dataTransfer.files?.[0]
    if (dropped) {
      setFile(dropped)
      setFileName(dropped.name)
      setError('')
    }
  }

  const textLinesCount = text.split('\n').filter(line => {
    const trimmed = line.trim()
    return trimmed && !trimmed.startsWith('#') && !trimmed.startsWith('//')
  }).length

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    setSubmitting(true)
    try {
      let data: any
      if (inputMode === 'file') {
        if (!file) {
          setError('请先选择或拖入 TXT 文件')
          setSubmitting(false)
          return
        }
        const formData = new FormData()
        formData.append('file', file)
        formData.append('platform', 'chatgpt')
        formData.append('auto_check', String(autoCheck))
        formData.append('concurrency', concurrency)
        if (proxyNode) formData.append('proxy_node', proxyNode)

        data = await apiForm('/accounts/import-file', formData)
      } else {
        if (!text.trim()) {
          setError('请在文本框中输入或粘贴账号卡密')
          setSubmitting(false)
          return
        }
        data = await apiFetch('/accounts/import', {
          method: 'POST',
          body: JSON.stringify({
            platform: 'chatgpt',
            text: text,
            auto_check: autoCheck,
            concurrency: Number(concurrency),
            proxy_node: proxyNode || null,
          }),
        })
      }

      setResult(data)
    } catch (err: any) {
      setError(err?.message || '导入失败，请检查文件格式')
    } finally {
      setSubmitting(false)
    }
  }

  const handleDone = () => {
    let createdTask: CreatedTask | undefined
    if (result?.task?.task_id) {
      createdTask = {
        id: result.task.task_id,
        title: `导入后 401 自动验活任务已创建（${concurrency} 并发）`,
      }
    }
    onSuccess(createdTask)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="max-h-[calc(100vh-2rem)] w-full max-w-xl overflow-y-auto rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-5 shadow-xl">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">导入 ChatGPT 账号</h2>
            <p className="mt-1 text-xs text-[var(--text-muted)]">
              支持直接粘贴或上传卡密 TXT，自动识别微软长效邮箱、2FA 密钥并完成持久化绑定。
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded p-1 text-[var(--text-muted)] hover:bg-[var(--bg-hover)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {result ? (
          <div className="mt-5 space-y-4">
            <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-4 text-emerald-300">
              <div className="text-sm font-semibold">🎉 导入处理完成</div>
              <div className="mt-2 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
                <div className="rounded bg-black/20 p-2">
                  <div className="text-[var(--text-muted)]">解析行数</div>
                  <div className="mt-1 text-lg font-bold text-[var(--text-primary)]">{result.parsed}</div>
                </div>
                <div className="rounded bg-black/20 p-2">
                  <div className="text-[var(--text-muted)]">新添账号</div>
                  <div className="mt-1 text-lg font-bold text-emerald-400">+{result.created}</div>
                </div>
                <div className="rounded bg-black/20 p-2">
                  <div className="text-[var(--text-muted)]">更新已有</div>
                  <div className="mt-1 text-lg font-bold text-amber-400">{result.updated}</div>
                </div>
                <div className="rounded bg-black/20 p-2">
                  <div className="text-[var(--text-muted)]">绑定长效邮箱</div>
                  <div className="mt-1 text-lg font-bold text-sky-400">{result.mailboxes_saved}</div>
                </div>
              </div>
              {result.failed > 0 ? (
                <p className="mt-2 text-xs text-red-400">注意：有 {result.failed} 行格式解析或写入失败，请检查源数据。</p>
              ) : null}
              {result.task?.task_id ? (
                <p className="mt-2 text-xs text-emerald-300">已自动触发后台验活任务，可在任务页面查看进度。</p>
              ) : null}
            </div>
            <div className="flex justify-end">
              <Button type="button" onClick={handleDone}>完成并刷新列表</Button>
            </div>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="mt-5 space-y-4">
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
                <div className="flex items-center justify-between text-xs text-[var(--text-muted)]">
                  <span>卡密格式：每行一个账号</span>
                  <span>有效行数：{textLinesCount}</span>
                </div>
                <textarea
                  value={text}
                  onChange={e => setText(e.target.value)}
                  placeholder={`支持如下常见格式（自动识别 ----、制表符、逗号、冒号）：\n\n邮箱----密码----client_id----refresh_token（自动绑定微软长效邮箱）\n邮箱----密码----2FA密钥（自动绑定 TOTP）\n邮箱----密码`}
                  rows={8}
                  className="mt-1.5 w-full rounded-md border border-[var(--border)] bg-transparent p-3 font-mono text-xs text-[var(--text-primary)] focus:border-sky-500 focus:outline-none"
                />
              </div>
            ) : (
              <div>
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
                  <p className="mt-1 text-xs text-[var(--text-muted)]">支持标准文本或卡密导出文件（最大 10MB）</p>
                  <label className="mt-3 cursor-pointer">
                    <span className="rounded-md border border-[var(--border)] bg-[var(--bg-card)] px-3 py-1.5 text-xs font-medium text-[var(--text-primary)] hover:bg-[var(--bg-hover)]">
                      选择文件
                    </span>
                    <input
                      type="file"
                      accept=".txt,.csv"
                      onChange={handleFileChange}
                      className="hidden"
                    />
                  </label>
                </div>
              </div>
            )}

            <div className="rounded-md border border-[var(--border)] bg-[var(--bg-pane)]/20 p-3 text-xs text-[var(--text-muted)]">
              <div className="font-medium text-[var(--text-secondary)]">💡 格式说明：</div>
              <ul className="mt-1 list-inside list-disc space-y-0.5">
                <li><span className="text-sky-400">微软长效卡密</span>：<code>邮箱----密码----client_id----refresh_token</code>（持久化保存，免密码无限刷新收信）</li>
                <li><span className="text-emerald-400">2FA 账号</span>：<code>邮箱----密码----totp_secret</code></li>
                <li><span className="text-amber-400">普通账号</span>：<code>邮箱----密码</code></li>
              </ul>
            </div>

            <div className="rounded-md border border-[var(--border)] bg-[var(--bg-pane)]/20 p-3 space-y-3">
              <label className="flex items-center gap-2 text-sm text-[var(--text-secondary)] cursor-pointer">
                <input
                  type="checkbox"
                  checked={autoCheck}
                  onChange={e => setAutoCheck(e.target.checked)}
                  className="rounded border-[var(--border)] text-sky-500"
                />
                <span>导入成功后立即执行 401 验活</span>
              </label>

              {autoCheck ? (
                <div className="grid gap-3 pt-2 sm:grid-cols-2 border-t border-[var(--border)]/50">
                  <label className="grid gap-1 text-xs text-[var(--text-muted)]">
                    验活代理节点
                    <select
                      value={proxyNode}
                      onChange={e => setProxyNode(e.target.value)}
                      className="rounded border border-[var(--border)] bg-[var(--bg-card)] px-2 py-1 text-xs text-[var(--text-primary)]"
                    >
                      <option value="">自动</option>
                      {proxyNodes.map(node => (
                        <option key={node.name} value={node.name} disabled={node.alive === false}>
                          {node.name} · {node.delay ? `${node.delay}ms` : '未测速'}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="grid gap-1 text-xs text-[var(--text-muted)]">
                    验活并发数
                    <select
                      value={concurrency}
                      onChange={e => setConcurrency(e.target.value)}
                      className="rounded border border-[var(--border)] bg-[var(--bg-card)] px-2 py-1 text-xs text-[var(--text-primary)]"
                    >
                      <option value="20">并发 20</option>
                      <option value="50">并发 50</option>
                      <option value="100">并发 100</option>
                    </select>
                  </label>
                </div>
              ) : null}
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
              <Button
                type="submit"
                disabled={submitting || (inputMode === 'text' ? !text.trim() : !file)}
              >
                {submitting ? '导入中…' : '开始导入'}
              </Button>
            </div>
          </form>
        )}
      </div>
    </div>
  )
}

export default function Accounts() {
  const navigate = useNavigate()
  const [accounts, setAccounts] = useState<AccountListItem[]>([])
  const [total, setTotal] = useState(0)
  const [survivalStats, setSurvivalStats] = useState<SurvivalStats | null>(null)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState<'all' | 'valid' | 'invalid' | 'has_mailbox' | 'has_rt'>('all')
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [batchActionLoading, setBatchActionLoading] = useState(false)
  const [loading, setLoading] = useState(false)
  const [showRegister, setShowRegister] = useState(false)
  const [showImport, setShowImport] = useState(false)
  const [runningAction, setRunningAction] = useState('')
  const [maintenanceConcurrency, setMaintenanceConcurrency] = useState('100')
  const [maintenanceProxyNode, setMaintenanceProxyNode] = useState('')
  const [maintenanceProxyNodes, setMaintenanceProxyNodes] = useState<ProxyNode[]>([])
  const [createdTask, setCreatedTask] = useState<CreatedTask | null>(null)
  const [taskNotice, setTaskNotice] = useState('')
  const [completedRecovery, setCompletedRecovery] = useState<{
    taskId: string
    recoveredCount: number
    recoveredEmails: string[]
  } | null>(null)
  const [error, setError] = useState('')

  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({
        platform: 'chatgpt',
        page: String(page),
        page_size: String(pageSize),
      })
      if (debouncedSearch.trim()) params.set('email', debouncedSearch.trim())
      const hasRefreshTokenOnly = statusFilter === 'has_rt'
      if (statusFilter !== 'all') {
        params.set('status', statusFilter)
        if (hasRefreshTokenOnly) {
          params.set('has_refresh_token', 'true')
        }
      }
      const [data, stats] = await Promise.all([
        apiFetch(`/accounts?${params}`),
        apiFetch('/accounts/survival-stats?platform=chatgpt'),
      ])
      setAccounts(Array.isArray(data?.items) ? data.items : [])
      setTotal(Number(data?.total || 0))
      setSelectedIds([])
      setSurvivalStats({
        platform: String(stats?.platform || 'chatgpt'),
        alive_accounts: Number(stats?.alive_accounts || 0),
        historical_registered_emails: Number(stats?.historical_registered_emails || 0),
        survival_rate: Number(stats?.survival_rate || 0),
      })
      setError('')
    } catch (err: any) {
      setError(err?.message || '读取账号列表失败')
    } finally {
      setLoading(false)
    }
  }, [debouncedSearch, statusFilter, page, pageSize])

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search), 350)
    return () => window.clearTimeout(timer)
  }, [search])

  useEffect(() => { setPage(1) }, [debouncedSearch, statusFilter, pageSize])
  useEffect(() => { void load() }, [load])
  useEffect(() => {
    void apiFetch('/proxy-nodes')
      .then((data: ProxyNodesResponse) => {
        setMaintenanceProxyNodes(Array.isArray(data?.nodes) ? data.nodes : [])
      })
      .catch(() => setMaintenanceProxyNodes([]))
  }, [])

  const downloadRecoveredSub2Api = async (taskId: string) => {
    try {
      const { blob, filename } = await apiDownload(`/accounts/tasks/${taskId}/export-recovered-sub2api`)
      triggerBrowserDownload(blob, filename)
    } catch (err: any) {
      setError(err?.message || '下载已恢复账号 Sub 文件失败')
    }
  }

  // 监听验活/登录任务执行进度，完成后若有 401 恢复账号则自动触发下载并高亮提示
  useEffect(() => {
    if (!createdTask?.id) return
    const taskId = createdTask.id
    let cancelled = false

    const checkTaskStatus = async () => {
      try {
        const taskInfo = await apiFetch(`/tasks/${taskId}`)
        if (cancelled) return
        if (taskInfo?.terminal) {
          const recoveredCount = Number(taskInfo?.data?.recovered_count || 0)
          const recoveredEmails = Array.isArray(taskInfo?.data?.recovered_emails) ? taskInfo.data.recovered_emails : []

          if (recoveredCount > 0) {
            setCompletedRecovery({
              taskId,
              recoveredCount,
              recoveredEmails,
            })
            // 自动触发静默下载仅包含恢复账号的 Sub 格式 json 文件
            void downloadRecoveredSub2Api(taskId)
            setTaskNotice(`🎉 验活完成！成功解救 ${recoveredCount} 个 401 账号，已为您自动导出 Sub2API 文件。`)
          } else {
            setTaskNotice(`验活任务执行完毕，未发现可解救的 401 账号。`)
          }
          setCreatedTask(null)
          void load()
        }
      } catch {
        // ignore polling errors
      }
    }

    const interval = window.setInterval(() => {
      void checkTaskStatus()
    }, 2000)

    return () => {
      cancelled = true
      window.clearInterval(interval)
    }
  }, [createdTask?.id, load])

  const createRefreshCheckTask = async (browser = true) => {
    setRunningAction(browser ? 'refresh_browser' : 'refresh')
    try {
      const task = await apiFetch('/accounts/check-refresh-tokens', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          concurrency: Number(maintenanceConcurrency),
          proxy_node: maintenanceProxyNode || null,
          browser,
        }),
      })
      setCreatedTask({
        id: task.task_id,
        title: browser
          ? `浏览器验活任务已创建（${maintenanceConcurrency} 并发，camoufox）`
          : `401 验活任务已创建（${maintenanceConcurrency} 并发）`,
      })
      await load()
    } catch (err: any) {
      setError(err?.message || '创建任务失败')
    } finally {
      setRunningAction('')
    }
  }

  const [copiedId, setCopiedId] = useState<number | null>(null)

  const EXPORT_FORMATS: Array<{ value: string, label: string }> = [
    { value: 'json', label: 'JSON' },
    { value: 'csv', label: 'CSV' },
    { value: 'sub2api', label: 'Sub2API' },
    { value: 'sub2api-agent-identity', label: 'Sub2API Agent Identity' },
    { value: 'cpa', label: 'CPA' },
    { value: 'any2api', label: 'Any2API' },
    { value: 'cockpit', label: 'Cockpit' },
  ]
  const [exportFormat, setExportFormat] = useState('json')
  const [exporting, setExporting] = useState('')

  const exportAccounts = async () => {
    setExporting(exportFormat)
    try {
      const { blob, filename } = await apiDownload(`/accounts/export/${exportFormat}`, {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids: [],
          select_all: true,
        }),
      })
      triggerBrowserDownload(blob, filename)
    } catch (err: any) {
      setError(err?.message || '导出失败')
    } finally {
      setExporting('')
    }
  }

  const copyAccount = async (account: AccountListItem) => {
    const lines = [account.email, account.password || '']
    if (account.totp_secret) {
      lines.push(`https://2fa.live/tok/${account.totp_secret}`)
    }
    const text = lines.join('\n')
    let ok = false
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text)
        ok = true
      }
    } catch {
      ok = false
    }
    if (!ok) {
      // Fallback for plain-HTTP deployments where the Clipboard API is blocked:
      // select a hidden textarea and use the legacy execCommand('copy').
      try {
        const ta = document.createElement('textarea')
        ta.value = text
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.select()
        ta.setSelectionRange(0, text.length)
        ok = document.execCommand('copy')
        document.body.removeChild(ta)
      } catch {
        ok = false
      }
    }
    if (ok) {
      setCopiedId(account.id)
      setTimeout(() => setCopiedId(null), 1500)
    } else {
      setError('复制失败')
    }
  }

  const [deletingId, setDeletingId] = useState<number | null>(null)
  const deleteAccount = async (account: AccountListItem) => {
    if (!window.confirm(`确定删除账号「${account.email}」？此操作不可撤销。`)) return
    setDeletingId(account.id)
    try {
      await apiFetch(`/accounts/${account.id}`, { method: 'DELETE' })
      await load()
    } catch (err: any) {
      setError(err?.message || '删除失败')
    } finally {
      setDeletingId(null)
    }
  }

  const toggleSelectAll = () => {
    if (selectedIds.length === accounts.length && accounts.length > 0) {
      setSelectedIds([])
    } else {
      setSelectedIds(accounts.map(a => a.id))
    }
  }

  const toggleSelectOne = (id: number) => {
    setSelectedIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id])
  }

  const handleBatchDelete = async () => {
    if (selectedIds.length === 0) return
    if (!window.confirm(`确定批量删除选中的 ${selectedIds.length} 个账号？此操作不可撤销！`)) return
    setBatchActionLoading(true)
    try {
      await apiFetch('/accounts/batch-delete', {
        method: 'POST',
        body: JSON.stringify({ ids: selectedIds }),
      })
      setSelectedIds([])
      await load()
    } catch (err: any) {
      setError(err?.message || '批量删除失败')
    } finally {
      setBatchActionLoading(false)
    }
  }

  const handleBatchCheck = async () => {
    if (selectedIds.length === 0) return
    setBatchActionLoading(true)
    try {
      const task = await apiFetch('/accounts/check-refresh-tokens', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          concurrency: Number(maintenanceConcurrency),
          proxy_node: maintenanceProxyNode || null,
          browser: true,
          account_ids: selectedIds,
        }),
      })
      setCreatedTask({
        id: task.task_id,
        title: `已为选中的 ${selectedIds.length} 个账号创建验活任务`,
      })
      setSelectedIds([])
      await load()
    } catch (err: any) {
      setError(err?.message || '创建定向验活任务失败')
    } finally {
      setBatchActionLoading(false)
    }
  }

  const handleBatchExport = async () => {
    if (selectedIds.length === 0) return
    setBatchActionLoading(true)
    try {
      const { blob, filename } = await apiDownload(`/accounts/export/${exportFormat}`, {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids: selectedIds,
          select_all: false,
        }),
      })
      triggerBrowserDownload(blob, filename)
    } catch (err: any) {
      setError(err?.message || '批量导出失败')
    } finally {
      setBatchActionLoading(false)
    }
  }

  return (
    <div className="space-y-4">
      {showRegister ? <RegisterDialog onClose={() => setShowRegister(false)} onCreated={(task) => { setShowRegister(false); setCreatedTask(task) }} /> : null}
      {showImport ? (
        <ImportAccountsDialog
          onClose={() => setShowImport(false)}
          onSuccess={(task) => {
            setShowImport(false)
            if (task) setCreatedTask(task)
            void load()
          }}
          proxyNodes={maintenanceProxyNodes}
        />
      ) : null}
      <Card className="border border-[var(--border)] bg-[var(--bg-pane)]/40 p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <h1 className="text-xl font-semibold text-[var(--text-primary)]">ChatGPT 账号</h1>
            <p className="mt-1 text-sm text-[var(--text-muted)]">共 {total} 个账号；列表按服务端分页加载。</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <select
              value={maintenanceProxyNode}
              onChange={event => setMaintenanceProxyNode(event.target.value)}
              disabled={Boolean(runningAction)}
              aria-label="注册或登录代理节点"
              className="max-w-56 rounded-md border border-[var(--border)] bg-transparent px-2 py-1.5 text-sm text-[var(--text-primary)] disabled:opacity-50"
            >
              <option value="">登录代理：自动</option>
              {maintenanceProxyNodes.map(node => (
                <option key={node.name} value={node.name} disabled={node.alive === false}>
                  {node.name} · {node.delay ? `${node.delay}ms` : '未测速'}
                </option>
              ))}
            </select>
            <select
              value={maintenanceConcurrency}
              onChange={event => setMaintenanceConcurrency(event.target.value)}
              disabled={Boolean(runningAction)}
              aria-label="任务并发数"
              className="rounded-md border border-[var(--border)] bg-transparent px-2 py-1.5 text-sm text-[var(--text-primary)] disabled:opacity-50"
            >
              <option value="50">并发 50</option>
              <option value="100">并发 100</option>
              <option value="150">并发 150</option>
              <option value="200">并发 200</option>
            </select>
            <Button size="sm" variant="outline" disabled={Boolean(runningAction)} onClick={() => void createRefreshCheckTask(true)} title="先用 Camoufox 并行验活，失活账号再用协议登录恢复 AT">
              <ShieldCheck className="mr-1.5 h-4 w-4" />
              {runningAction === 'refresh_browser' ? '创建中…' : '401 验活'}
            </Button>
            <Button size="sm" onClick={() => setShowRegister(true)}>
              <Plus className="mr-1.5 h-4 w-4" />协议注册
            </Button>
            <Button size="sm" variant="outline" onClick={() => setShowImport(true)} title="导入卡密 TXT 或粘贴账号">
              <Upload className="mr-1.5 h-4 w-4" />导入账号
            </Button>
            <div className="flex items-center gap-2">
              <select
                value={exportFormat}
                onChange={event => setExportFormat(event.target.value)}
                disabled={Boolean(exporting)}
                aria-label="导出格式"
                className="rounded-md border border-[var(--border)] bg-transparent px-2 py-1.5 text-sm text-[var(--text-primary)] disabled:opacity-50"
              >
                {EXPORT_FORMATS.map(format => (
                  <option key={format.value} value={format.value}>{format.label}</option>
                ))}
              </select>
              <Button size="sm" variant="outline" disabled={Boolean(exporting)} onClick={() => void exportAccounts()} title="导出全部账号">
                <Download className="mr-1.5 h-4 w-4" />
                {exporting ? '导出中…' : '导出'}
              </Button>
            </div>
          </div>
        </div>
        <div className="mt-5 grid grid-cols-1 border-t border-[var(--border)] pt-4 sm:grid-cols-3">
          <div className="py-2 sm:pr-5">
            <div className="text-xs text-[var(--text-muted)]">存活账号</div>
            <div className="mt-1 text-2xl font-semibold text-emerald-400">{survivalStats?.alive_accounts ?? '-'}</div>
          </div>
          <div className="border-t border-[var(--border)] py-2 sm:border-l sm:border-t-0 sm:px-5">
            <div className="text-xs text-[var(--text-muted)]">历史注册成功邮箱</div>
            <div className="mt-1 text-2xl font-semibold text-[var(--text-primary)]">{survivalStats?.historical_registered_emails ?? '-'}</div>
          </div>
          <div className="border-t border-[var(--border)] py-2 sm:border-l sm:border-t-0 sm:pl-5">
            <div className="text-xs text-[var(--text-muted)]">存活率</div>
            <div className="mt-1 text-2xl font-semibold text-sky-400">{survivalStats ? `${survivalStats.survival_rate.toFixed(2)}%` : '-'}</div>
          </div>
        </div>
        {createdTask ? (
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-sm text-sky-200">
            <span className="flex items-center gap-2">
              <RefreshCw className="h-4 w-4 animate-spin text-sky-400" />
              <span>{createdTask.title}：<span className="font-mono text-xs">{createdTask.id}</span>（正在后台处理中，完成后将自动刷新…）</span>
            </span>
            <Button size="sm" variant="outline" onClick={() => navigate('/tasks')}>查看详情</Button>
          </div>
        ) : null}
        {completedRecovery ? (
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-300">
            <div className="flex items-center gap-2">
              <CheckCircle2 className="h-5 w-5 text-emerald-400 shrink-0" />
              <div>
                <div className="font-medium text-emerald-200">
                  🎉 验活完成！成功解救 {completedRecovery.recoveredCount} 个 401 账号已恢复有效状态
                </div>
                <div className="mt-0.5 text-xs text-emerald-400/90">
                  受影响账号：{completedRecovery.recoveredEmails.join(', ')}（已自动触发下载 Sub2API 单文件导入格式）
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                className="border-emerald-500/40 bg-emerald-500/20 text-emerald-200 hover:bg-emerald-500/30 text-xs font-semibold"
                onClick={() => void downloadRecoveredSub2Api(completedRecovery.taskId)}
              >
                <Download className="mr-1.5 h-3.5 w-3.5" />重新下载 Sub 格式
              </Button>
              <button
                type="button"
                onClick={() => setCompletedRecovery(null)}
                className="rounded p-1 text-emerald-400 hover:bg-emerald-500/20"
                title="关闭提示"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
        ) : taskNotice ? (
          <div className="mt-4 flex items-center justify-between rounded-md border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-xs text-sky-200">
            <span>{taskNotice}</span>
            <button type="button" onClick={() => setTaskNotice('')} className="rounded p-0.5 text-sky-400 hover:bg-sky-500/20">
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        ) : null}
        {error ? <div className="mt-4 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</div> : null}
      </Card>

      <Card className="overflow-hidden border border-[var(--border)] bg-[var(--bg-card)] p-0">
        <div className="flex flex-col gap-3 border-b border-[var(--border)] px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-1 flex-wrap items-center gap-2">
            <input
              value={search}
              onChange={event => setSearch(event.target.value)}
              placeholder="搜索账号 (邮箱)"
              className="w-full max-w-xs rounded-md border border-[var(--border)] bg-transparent px-3 py-1.5 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)]"
            />
            <select
              value={statusFilter}
              onChange={e => setStatusFilter(e.target.value as any)}
              className="rounded-md border border-[var(--border)] bg-transparent px-3 py-1.5 text-sm text-[var(--text-primary)]"
            >
              <option value="all">全部状态</option>
              <option value="valid">✅ 正常有效</option>
              <option value="invalid">❌ 401 失效</option>
              <option value="has_mailbox">📧 微软长效托管</option>
              <option value="has_rt">🔑 包含 RT（仅看有 RT）</option>
            </select>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => void load()} disabled={loading}>
              <RefreshCw className={`mr-1.5 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />刷新
            </Button>
          </div>
        </div>

        {/* 浮动批量操作条 */}
        {selectedIds.length > 0 ? (
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-sky-500/30 bg-sky-500/10 px-4 py-2.5 text-sm text-sky-200">
            <div className="flex items-center gap-2 font-medium">
              <CheckSquare className="h-4 w-4 text-sky-400" />
              <span>已选中 <strong className="text-white">{selectedIds.length}</strong> 个账号</span>
            </div>
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                className="border-sky-500/40 bg-sky-500/20 text-sky-200 hover:bg-sky-500/30"
                disabled={batchActionLoading}
                onClick={() => void handleBatchCheck()}
                title="针对选中的账号创建 401 验活任务"
              >
                <ShieldCheck className="mr-1.5 h-3.5 w-3.5" />
                {batchActionLoading ? '处理中…' : '定向验活'}
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="border-sky-500/40 bg-sky-500/20 text-sky-200 hover:bg-sky-500/30"
                disabled={batchActionLoading}
                onClick={() => void handleBatchExport()}
                title="导出选中账号"
              >
                <Download className="mr-1.5 h-3.5 w-3.5" />
                {batchActionLoading ? '处理中…' : `导出选中 (${exportFormat.toUpperCase()})`}
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="border-red-500/40 bg-red-500/20 text-red-300 hover:bg-red-500/30"
                disabled={batchActionLoading}
                onClick={() => void handleBatchDelete()}
                title="批量删除选中账号"
              >
                <Trash2 className="mr-1.5 h-3.5 w-3.5" />
                {batchActionLoading ? '删除中…' : '批量删除'}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="text-[var(--text-muted)] hover:text-white"
                onClick={() => setSelectedIds([])}
              >
                取消选择
              </Button>
            </div>
          </div>
        ) : null}

        <div className="overflow-x-auto">
          <table className="w-full min-w-[700px] text-sm">
            <thead className="bg-[var(--bg-pane)]/60 text-left text-xs uppercase tracking-wide text-[var(--text-muted)]">
              <tr>
                <th className="w-10 px-4 py-3 text-center">
                  <input
                    type="checkbox"
                    checked={accounts.length > 0 && selectedIds.length === accounts.length}
                    onChange={toggleSelectAll}
                    aria-label="全选当前页"
                    className="rounded border-[var(--border)] text-sky-500 cursor-pointer"
                  />
                </th>
                <th className="px-4 py-3 font-medium">账号</th>
                <th className="px-4 py-3 font-medium">密码</th>
                <th className="px-4 py-3 font-medium">Token 状态</th>
                <th className="px-4 py-3 font-medium">401 状态</th>
                <th className="px-4 py-3 font-medium">注册时间</th>
                <th className="px-4 py-3 font-medium text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border)]">
              {accounts.map(account => {
                const isSelected = selectedIds.includes(account.id)
                return (
                  <tr
                    key={account.id}
                    className={`transition-colors ${
                      isSelected ? 'bg-sky-500/10 hover:bg-sky-500/15' : 'hover:bg-[var(--bg-hover)]/60'
                    }`}
                  >
                    <td className="w-10 px-4 py-3 text-center">
                      <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={() => toggleSelectOne(account.id)}
                        aria-label={`选择账号 ${account.email}`}
                        className="rounded border-[var(--border)] text-sky-500 cursor-pointer"
                      />
                    </td>
                    <td className="px-4 py-3 font-mono text-[var(--text-primary)]">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <span>{account.email}</span>
                        {account.has_mailbox ? (
                          <span
                            className="inline-flex items-center gap-1 rounded bg-sky-500/10 px-1.5 py-0.5 text-[11px] font-sans text-sky-400 border border-sky-500/20"
                            title={`绑定微软长效邮箱：${account.mailbox_email || '已托管'}`}
                          >
                            <Mail className="h-3 w-3" />
                            长效托管
                          </span>
                        ) : null}
                      </div>
                    </td>
                    <td className="px-4 py-3 font-mono text-[var(--text-secondary)]">{account.password || '-'}</td>
                    <td className="px-4 py-3">
                      <div className="flex flex-col gap-1 items-start">
                        <span className={account.has_refresh_token ? 'text-emerald-400 font-medium text-xs' : 'text-[var(--text-muted)] text-xs'}>
                          {account.has_refresh_token ? '🔑 有 RT' : '无 RT'}
                        </span>
                        {formatAtExpiry(account.at_expires_at)}
                      </div>
                    </td>
                    <td className="px-4 py-3">{statePill(account.refresh_token_status)}</td>
                    <td className="px-4 py-3 text-[var(--text-secondary)] text-xs">{formatDate(account.created_at)}</td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-1.5">
                        <button
                          type="button"
                          onClick={() => void copyAccount(account)}
                          title="复制 账号 / 密码 / 2FA 查看链接"
                          className="inline-flex items-center gap-1 rounded border border-[var(--border)] bg-[var(--bg-pane)]/40 px-2 py-1 text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                        >
                          <Copy className="h-3.5 w-3.5" />
                          {copiedId === account.id ? '已复制' : '复制'}
                        </button>
                        <button
                          type="button"
                          onClick={() => void deleteAccount(account)}
                          disabled={deletingId === account.id}
                          title="删除账号"
                          className="inline-flex items-center gap-1 rounded border border-red-500/30 bg-red-500/10 px-2 py-1 text-xs text-red-400 hover:bg-red-500/20 disabled:opacity-50"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                          {deletingId === account.id ? '删除中' : '删除'}
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
              {!loading && accounts.length === 0 ? (
                <tr><td colSpan={7} className="px-4 py-16 text-center text-[var(--text-muted)]">暂无账号</td></tr>
              ) : null}
            </tbody>
          </table>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--border)] px-4 py-3 text-sm text-[var(--text-muted)]">
          <span>第 {page} / {totalPages} 页</span>
          <div className="flex items-center gap-2">
            <select value={pageSize} onChange={event => setPageSize(Number(event.target.value))} className="rounded-md border border-[var(--border)] bg-transparent px-2 py-1 text-[var(--text-primary)]">
              <option value={20}>20 / 页</option>
              <option value={50}>50 / 页</option>
              <option value={100}>100 / 页</option>
            </select>
            <Button variant="outline" size="sm" disabled={page <= 1 || loading} onClick={() => setPage(value => value - 1)}><ChevronLeft className="h-4 w-4" /></Button>
            <Button variant="outline" size="sm" disabled={page >= totalPages || loading} onClick={() => setPage(value => value + 1)}><ChevronRight className="h-4 w-4" /></Button>
          </div>
        </div>
      </Card>
    </div>
  )
}
