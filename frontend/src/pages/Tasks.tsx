import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock,
  Copy,
  Download,
  FileText,
  ListFilter,
  RefreshCw,
  Square,
  X,
} from 'lucide-react'

import { Button } from '@/components/ui/button'
import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import { apiDownload, apiFetch, triggerBrowserDownload } from '@/lib/utils'
import { isCancellableTaskStatus, isTerminalTaskStatus } from '@/lib/tasks'

const TYPE_LABELS: Record<string, string> = {
  register: '协议注册',
  refresh_token_check: '401 验活与恢复',
}

const STATUS_CONFIG: Record<string, { label: string; tone: string; dot: string }> = {
  running: {
    label: '运行中',
    tone: 'bg-sky-500/10 text-sky-400 border-sky-500/30',
    dot: 'bg-sky-400 animate-pulse',
  },
  succeeded: {
    label: '执行成功',
    tone: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
    dot: 'bg-emerald-400',
  },
  failed: {
    label: '执行失败',
    tone: 'bg-red-500/10 text-red-400 border-red-500/30',
    dot: 'bg-red-400',
  },
  cancelled: {
    label: '已取消',
    tone: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
    dot: 'bg-amber-400',
  },
  interrupted: {
    label: '已中断',
    tone: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
    dot: 'bg-amber-400',
  },
  pending: {
    label: '等待中',
    tone: 'bg-zinc-500/10 text-zinc-400 border-zinc-500/30',
    dot: 'bg-zinc-400',
  },
}

function formatDateTime(iso?: string) {
  if (!iso) return '-'
  try {
    const d = new Date(iso)
    return d.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    })
  } catch {
    return iso
  }
}

function calculateDuration(start?: string, end?: string) {
  if (!start) return '-'
  try {
    const s = new Date(start).getTime()
    const e = end ? new Date(end).getTime() : Date.now()
    const diffSec = Math.max(0, Math.floor((e - s) / 1000))
    if (diffSec < 60) return `${diffSec} 秒`
    const mins = Math.floor(diffSec / 60)
    const secs = diffSec % 60
    return `${mins} 分 ${secs} 秒`
  } catch {
    return '-'
  }
}

export default function Tasks() {
  const [tasks, setTasks] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState<'all' | 'active' | 'completed'>('all')
  const [stoppingId, setStoppingId] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [selectedTask, setSelectedTask] = useState<any | null>(null)
  const [expandedLogIds, setExpandedLogIds] = useState<Record<string, boolean>>({})
  const [copiedTaskId, setCopiedTaskId] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      // 默认拉取前 100 个任务（含历史完成的），方便用户随时回溯定位错误
      const data = await apiFetch('/tasks?limit=100')
      setTasks(Array.isArray(data?.items) ? data.items : [])
      setError('')
    } catch (err: any) {
      setError(err?.message || '读取任务列表失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
    // 只有在有正在运行的任务时，才高频轮询；否则低频轮询
    const timer = window.setInterval(() => {
      void load()
    }, 2000)
    return () => window.clearInterval(timer)
  }, [load])

  // 当查看的任务在列表刷新时，保持 selectedTask 数据同步
  useEffect(() => {
    if (!selectedTask) return
    const updated = tasks.find(t => t.task_id === selectedTask.task_id)
    if (updated) {
      setSelectedTask(updated)
    }
  }, [tasks, selectedTask?.task_id])

  const activeTasks = useMemo(() => tasks.filter(t => !isTerminalTaskStatus(t.status)), [tasks])
  const completedTasks = useMemo(() => tasks.filter(t => isTerminalTaskStatus(t.status)), [tasks])

  const filteredTasks = useMemo(() => {
    if (filter === 'active') return activeTasks
    if (filter === 'completed') return completedTasks
    return tasks
  }, [filter, activeTasks, completedTasks, tasks])

  const stop = async (taskId: string) => {
    setStoppingId(taskId)
    try {
      await apiFetch(`/tasks/${taskId}/cancel`, { method: 'POST' })
      await load()
    } catch (err: any) {
      setError(err?.message || '停止任务失败')
    } finally {
      setStoppingId(null)
    }
  }

  const toggleInlineLog = (taskId: string) => {
    setExpandedLogIds(prev => ({ ...prev, [taskId]: !prev[taskId] }))
  }

  const copyText = (text: string, id: string) => {
    navigator.clipboard?.writeText(text).then(() => {
      setCopiedTaskId(id)
      setTimeout(() => setCopiedTaskId(null), 1500)
    }).catch(() => {})
  }

  return (
    <div className="space-y-5">
      {/* 顶部标题与过滤器 */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold text-[var(--text-primary)]">任务管理与日志</h1>
          <p className="mt-1 text-sm text-[var(--text-muted)]">
            实时查看每次任务的执行状态、账号恢复情况及详细诊断日志，方便准确定位问题。
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {/* 筛选标签切换 */}
          <div className="flex items-center rounded-lg border border-[var(--border)] bg-[var(--bg-input)] p-1 text-xs">
            <button
              type="button"
              onClick={() => setFilter('all')}
              className={`rounded-md px-2.5 py-1 font-medium transition-colors ${
                filter === 'all'
                  ? 'bg-[var(--bg-hover)] text-[var(--text-primary)] shadow-sm'
                  : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'
              }`}
            >
              全部 ({tasks.length})
            </button>
            <button
              type="button"
              onClick={() => setFilter('active')}
              className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 font-medium transition-colors ${
                filter === 'active'
                  ? 'bg-[var(--bg-hover)] text-[var(--text-primary)] shadow-sm'
                  : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'
              }`}
            >
              {activeTasks.length > 0 && <span className="h-1.5 w-1.5 rounded-full bg-sky-400 animate-ping" />}
              运行中 ({activeTasks.length})
            </button>
            <button
              type="button"
              onClick={() => setFilter('completed')}
              className={`rounded-md px-2.5 py-1 font-medium transition-colors ${
                filter === 'completed'
                  ? 'bg-[var(--bg-hover)] text-[var(--text-primary)] shadow-sm'
                  : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'
              }`}
            >
              已完成 ({completedTasks.length})
            </button>
          </div>

          <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
            <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            刷新
          </Button>
        </div>
      </div>

      {error ? (
        <div className="flex items-center gap-2 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>{error}</span>
        </div>
      ) : null}

      {/* 任务列表容器 */}
      <div className="overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-card)]">
        {!loading && filteredTasks.length === 0 ? (
          <div className="px-5 py-16 text-center">
            <ListFilter className="mx-auto h-8 w-8 text-[var(--text-muted)] opacity-50" />
            <p className="mt-3 text-sm font-medium text-[var(--text-secondary)]">
              {filter === 'active' ? '当前没有正在运行的任务' : '暂无任务记录'}
            </p>
            {filter === 'active' && completedTasks.length > 0 && (
              <Button
                variant="ghost"
                size="sm"
                className="mt-3 text-xs text-sky-400 hover:text-sky-300"
                onClick={() => setFilter('completed')}
              >
                查看已完成的历史任务 ({completedTasks.length})
              </Button>
            )}
          </div>
        ) : (
          <div className="divide-y divide-[var(--border)]">
            {filteredTasks.map((task) => {
              const statusCfg = STATUS_CONFIG[task.status] || {
                label: task.status,
                tone: 'bg-zinc-500/10 text-zinc-300 border-zinc-500/30',
                dot: 'bg-zinc-400',
              }
              const isInlineLogOpen = !!expandedLogIds[task.task_id]
              const taskData = task.data || {}
              const hasRecovered = Number(taskData.recovered_count || 0) > 0
              const hasFailedLogins = Number(taskData.login_failed || 0) > 0
              const isRecoverCheck = task.type === 'refresh_token_check'

              return (
                <div key={task.task_id} className="space-y-3 px-4 py-4 transition-colors hover:bg-[var(--bg-hover)]/30">
                  {/* 卡片头部与状态 */}
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-semibold text-[var(--text-primary)]">
                          {TYPE_LABELS[task.type] || task.type}
                        </span>

                        {/* 状态徽章 */}
                        <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${statusCfg.tone}`}>
                          <span className={`h-1.5 w-1.5 rounded-full ${statusCfg.dot}`} />
                          {statusCfg.label}
                        </span>

                        {/* 恢复情况统计快捷标签 */}
                        {isRecoverCheck && (
                          <div className="flex flex-wrap items-center gap-1.5">
                            {hasRecovered && (
                              <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-0.5 text-xs font-medium text-emerald-300">
                                <CheckCircle2 className="h-3 w-3" />
                                成功解救 {taskData.recovered_count} 个
                              </span>
                            )}
                            {hasFailedLogins && (
                              <span className="inline-flex items-center gap-1 rounded-full border border-red-500/40 bg-red-500/10 px-2.5 py-0.5 text-xs font-medium text-red-300">
                                失败 {taskData.login_failed} 个
                              </span>
                            )}
                            {taskData.login_required > 0 && !hasRecovered && !hasFailedLogins && (
                              <span className="rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-xs text-sky-300">
                                需恢复 {taskData.login_required} 个
                              </span>
                            )}
                          </div>
                        )}
                      </div>

                      {/* 任务基础信息 */}
                      <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-[var(--text-secondary)]">
                        <span>
                          进度：<b className="text-[var(--text-primary)]">{task.progress || '0/0'}</b>
                        </span>
                        <span className="flex items-center gap-1">
                          <Clock className="h-3 w-3 text-[var(--text-muted)]" />
                          开始：{formatDateTime(task.started_at || task.created_at)}
                        </span>
                        <span>
                          耗时：{calculateDuration(task.started_at || task.created_at, task.finished_at)}
                        </span>
                        <div className="flex items-center gap-1">
                          <span className="font-mono text-[var(--text-muted)]">{task.task_id}</span>
                          <button
                            type="button"
                            onClick={() => copyText(task.task_id, task.task_id)}
                            className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                            title="复制任务 ID"
                          >
                            {copiedTaskId === task.task_id ? (
                              <Check className="h-3 w-3 text-emerald-400" />
                            ) : (
                              <Copy className="h-3 w-3" />
                            )}
                          </button>
                        </div>
                      </div>

                      {/* 关键错误定位提示 */}
                      {task.error && (
                        <div className="mt-2.5 flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-200">
                          <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-red-400 mt-0.5" />
                          <div className="break-words">
                            <span className="font-medium text-red-300">错误原因：</span>
                            {task.error}
                          </div>
                        </div>
                      )}
                    </div>

                    {/* 操作按钮组 */}
                    <div className="flex flex-wrap items-center gap-2 shrink-0">
                      {/* 查看日志核心按钮 */}
                      <Button
                        size="sm"
                        variant="outline"
                        className="border-sky-500/40 bg-sky-500/10 text-sky-300 hover:bg-sky-500/20 hover:text-sky-200"
                        onClick={() => setSelectedTask(task)}
                      >
                        <FileText className="mr-1.5 h-3.5 w-3.5" />
                        查看日志与详情
                      </Button>

                      {/* 内联日志展开/折叠 */}
                      <Button
                        size="sm"
                        variant="ghost"
                        className="text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
                        onClick={() => toggleInlineLog(task.task_id)}
                      >
                        {isInlineLogOpen ? (
                          <>
                            <ChevronUp className="mr-1 h-3.5 w-3.5" />
                            收起快速日志
                          </>
                        ) : (
                          <>
                            <ChevronDown className="mr-1 h-3.5 w-3.5" />
                            快速日志
                          </>
                        )}
                      </Button>

                      {/* 下载恢复账号 */}
                      {hasRecovered && (
                        <Button
                          size="sm"
                          variant="outline"
                          className="border-emerald-500/40 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20"
                          onClick={async () => {
                            try {
                              const { blob, filename } = await apiDownload(
                                `/accounts/tasks/${task.task_id}/export-recovered-sub2api`
                              )
                              triggerBrowserDownload(blob, filename)
                            } catch (err: any) {
                              alert(err?.message || '下载恢复账号失败')
                            }
                          }}
                        >
                          <Download className="mr-1.5 h-3.5 w-3.5" />
                          下载恢复账号 (Sub)
                        </Button>
                      )}

                      {/* 停止任务按钮 */}
                      {isCancellableTaskStatus(task.status) && (
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => void stop(task.task_id)}
                          disabled={stoppingId === task.task_id}
                          className="border-red-500/35 text-red-300 hover:bg-red-500/10 hover:text-red-200"
                        >
                          <Square className="mr-1.5 h-3.5 w-3.5" />
                          {stoppingId === task.task_id ? '停止中…' : '停止任务'}
                        </Button>
                      )}
                    </div>
                  </div>

                  {/* 内联快速日志面板 */}
                  {isInlineLogOpen && (
                    <div className="pt-2">
                      <TaskLogPanel taskId={task.task_id} compact onDone={() => void load()} />
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* 查看日志与恢复情况 弹窗详情 */}
      {selectedTask && (
        <TaskDetailModal
          task={selectedTask}
          onClose={() => setSelectedTask(null)}
          onRefresh={load}
        />
      )}
    </div>
  )
}

/**
 * 任务专属日志与情况定位弹窗
 */
function TaskDetailModal({
  task,
  onClose,
  onRefresh,
}: {
  task: any
  onClose: () => void
  onRefresh: () => void
}) {
  const taskData = task.data || {}
  const statusCfg = STATUS_CONFIG[task.status] || {
    label: task.status,
    tone: 'bg-zinc-500/10 text-zinc-300 border-zinc-500/30',
    dot: 'bg-zinc-400',
  }
  const isRecoverCheck = task.type === 'refresh_token_check'
  const recoveredEmails = Array.isArray(taskData.recovered_emails) ? taskData.recovered_emails : []
  const [copiedEmails, setCopiedEmails] = useState(false)

  const copyEmails = () => {
    if (!recoveredEmails.length) return
    navigator.clipboard?.writeText(recoveredEmails.join('\n')).then(() => {
      setCopiedEmails(true)
      setTimeout(() => setCopiedEmails(false), 2000)
    }).catch(() => {})
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="relative flex flex-col w-full max-w-4xl max-h-[90vh] rounded-2xl border border-[var(--border)] bg-[var(--bg-card)] shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
      >
        {/* 弹窗头部 */}
        <div className="flex items-center justify-between border-b border-[var(--border)] px-6 py-4 bg-[var(--bg-card)]">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-lg font-bold text-[var(--text-primary)]">
                {TYPE_LABELS[task.type] || task.type} - 任务日志与详情
              </h2>
              <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${statusCfg.tone}`}>
                <span className={`h-1.5 w-1.5 rounded-full ${statusCfg.dot}`} />
                {statusCfg.label}
              </span>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-4 text-xs text-[var(--text-muted)]">
              <span>任务 ID: <code className="text-[var(--text-secondary)]">{task.task_id}</code></span>
              <span>开始：{formatDateTime(task.started_at || task.created_at)}</span>
              <span>总耗时：{calculateDuration(task.started_at || task.created_at, task.finished_at)}</span>
            </div>
          </div>

          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-2 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]"
            title="关闭窗口"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* 弹窗内容区：恢复指标卡片 + 错误定位 + 完整日志 */}
        <div className="flex-1 overflow-y-auto p-6 space-y-5">
          {/* 401 恢复任务专有统计面板 */}
          {isRecoverCheck && (
            <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-hover)]/40 p-4 space-y-4">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold uppercase tracking-wider text-[var(--text-secondary)]">
                  401 账号验活与自动抢救结果
                </span>
                {recoveredEmails.length > 0 && (
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 text-xs border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/20"
                    onClick={async () => {
                      try {
                        const { blob, filename } = await apiDownload(
                          `/accounts/tasks/${task.task_id}/export-recovered-sub2api`
                        )
                        triggerBrowserDownload(blob, filename)
                      } catch (err: any) {
                        alert(err?.message || '下载失败')
                      }
                    }}
                  >
                    <Download className="mr-1 h-3 w-3" />
                    导出恢复账号 (Sub2API)
                  </Button>
                )}
              </div>

              {/* 指标卡片网格 */}
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-3 text-center">
                  <div className="text-xs text-[var(--text-muted)]">需恢复账号</div>
                  <div className="mt-1 text-lg font-bold text-[var(--text-primary)]">
                    {taskData.login_required || 0}
                  </div>
                </div>
                <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-3 text-center">
                  <div className="text-xs text-[var(--text-muted)]">尝试重新登录</div>
                  <div className="mt-1 text-lg font-bold text-[var(--text-primary)]">
                    {taskData.login_attempted || 0}
                  </div>
                </div>
                <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3 text-center">
                  <div className="text-xs text-emerald-300">成功抢救解救</div>
                  <div className="mt-1 text-lg font-bold text-emerald-400">
                    {taskData.recovered_count || 0}
                  </div>
                </div>
                <div className={`rounded-lg border p-3 text-center ${
                  Number(taskData.login_failed || 0) > 0
                    ? 'border-red-500/30 bg-red-500/10'
                    : 'border-[var(--border)] bg-[var(--bg-card)]'
                }`}>
                  <div className={`text-xs ${Number(taskData.login_failed || 0) > 0 ? 'text-red-300' : 'text-[var(--text-muted)]'}`}>
                    抢救登录失败
                  </div>
                  <div className={`mt-1 text-lg font-bold ${Number(taskData.login_failed || 0) > 0 ? 'text-red-400' : 'text-[var(--text-primary)]'}`}>
                    {taskData.login_failed || 0}
                  </div>
                </div>
              </div>

              {/* 成功恢复的邮箱列表 */}
              {recoveredEmails.length > 0 && (
                <div className="space-y-2">
                  <div className="flex items-center justify-between text-xs text-[var(--text-secondary)]">
                    <span>已解救并换取有效 Codex 凭证的账号：</span>
                    <button
                      type="button"
                      onClick={copyEmails}
                      className="flex items-center gap-1 text-sky-400 hover:text-sky-300 text-xs"
                    >
                      {copiedEmails ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                      {copiedEmails ? '已复制全部' : '复制邮箱列表'}
                    </button>
                  </div>
                  <div className="flex flex-wrap gap-1.5 max-h-24 overflow-y-auto rounded-lg border border-[var(--border)] bg-[var(--bg-card)] p-2">
                    {recoveredEmails.map((email: string) => (
                      <span
                        key={email}
                        className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 font-mono text-[11px] text-emerald-300"
                      >
                        {email}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* 任务错误定位展示 */}
          {task.error && (
            <div className="rounded-xl border border-red-500/40 bg-red-500/15 p-4 space-y-1.5">
              <div className="flex items-center gap-2 text-sm font-semibold text-red-300">
                <AlertTriangle className="h-4 w-4" />
                错误定位与失败原因
              </div>
              <div className="break-words text-xs text-red-200 leading-relaxed font-mono">
                {task.error}
              </div>
            </div>
          )}

          {/* 完整日志面板 */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-[var(--text-primary)]">
                完整执行与诊断日志
              </h3>
              <span className="text-xs text-[var(--text-muted)]">
                包含协议交互、浏览器渲染、OAuth 回调、TOTP 校验等
              </span>
            </div>

            <div className="min-h-[350px] rounded-xl border border-[var(--border)] bg-[var(--bg-card)] p-3">
              <TaskLogPanel taskId={task.task_id} onDone={onRefresh} />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
