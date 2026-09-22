import { useCallback, useEffect, useState } from 'react'
import { Check, CircleAlert, LoaderCircle, Pencil, Plus, Power, RefreshCw, Trash2, X } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { apiFetch } from '@/lib/utils'
import { MaskedField } from '@/lib/privacy-context'

type ProxySource = {
  name: string
  url: string
  interval: number
  node_count: number
  updated_at: string
  runtime_available: boolean
}

type ProxyNode = {
  name: string
  type: string
  alive: boolean | null
  delay: number | null
  last_test: string
  udp: boolean
  selected: boolean
  enabled: boolean
  provider: string
}

type SourceForm = { name: string; url: string; interval: string }

function formatDate(value: string) {
  if (!value) return '-'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString()
}

export default function ProxyPoolSettings() {
  const [tab, setTab] = useState<'sources' | 'nodes'>('sources')
  const [sources, setSources] = useState<ProxySource[]>([])
  const [nodes, setNodes] = useState<ProxyNode[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [sourceForm, setSourceForm] = useState<SourceForm | null>(null)
  const [editingSource, setEditingSource] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const loadSources = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const data = await apiFetch('/proxy-nodes/sources')
      setSources(Array.isArray(data?.sources) ? data.sources : [])
      if (data?.runtime_error) setError(`Mihomo 控制器：${data.runtime_error}`)
    } catch (err: any) {
      setError(err?.message || '读取代理订阅失败')
    } finally {
      setLoading(false)
    }
  }, [])

  const loadNodes = useCallback(async (refresh = false) => {
    setLoading(true)
    setError('')
    try {
      const data = await apiFetch(`/proxy-nodes${refresh ? '?refresh=true' : ''}`)
      setNodes(Array.isArray(data?.nodes) ? data.nodes : [])
      if (!data?.available) setError(data?.error || 'Mihomo 控制器不可用')
    } catch (err: any) {
      setError(err?.message || '读取代理节点失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadSources()
  }, [loadSources])

  useEffect(() => {
    if (tab === 'nodes') void loadNodes()
  }, [tab, loadNodes])

  const openCreate = () => {
    setEditingSource(null)
    setSourceForm({ name: `subscription-${sources.length + 1}`, url: '', interval: '3600' })
    setError('')
  }

  const openEdit = (source: ProxySource) => {
    setEditingSource(source.name)
    setSourceForm({ name: source.name, url: source.url, interval: String(source.interval || 3600) })
    setError('')
  }

  const saveSource = async () => {
    if (!sourceForm?.name.trim() || !sourceForm.url.trim()) {
      setError('请填写来源名称和订阅 URL')
      return
    }
    setSaving(true)
    setError('')
    try {
      const payload = { ...sourceForm, name: sourceForm.name.trim(), url: sourceForm.url.trim(), interval: Number(sourceForm.interval) || 3600 }
      const data = await apiFetch(editingSource ? `/proxy-nodes/sources/${encodeURIComponent(editingSource)}` : '/proxy-nodes/sources', {
        method: editingSource ? 'PUT' : 'POST',
        body: JSON.stringify(payload),
      })
      setSourceForm(null)
      setNotice(data?.reload_error ? `已保存，但热重载失败：${data.reload_error}` : '代理订阅已保存并热重载')
      await loadSources()
    } catch (err: any) {
      setError(err?.message || '保存代理订阅失败')
    } finally {
      setSaving(false)
    }
  }

  const deleteSource = async (source: ProxySource) => {
    if (!window.confirm(`确定删除代理订阅“${source.name}”？`)) return
    setError('')
    try {
      const data = await apiFetch(`/proxy-nodes/sources/${encodeURIComponent(source.name)}`, { method: 'DELETE' })
      setNotice(data?.reload_error ? `已删除，但热重载失败：${data.reload_error}` : '代理订阅已删除')
      await loadSources()
    } catch (err: any) {
      setError(err?.message || '删除代理订阅失败')
    }
  }

  const refreshSource = async (source: ProxySource) => {
    setError('')
    try {
      await apiFetch(`/proxy-nodes/sources/${encodeURIComponent(source.name)}/refresh`, { method: 'POST' })
      setNotice(`已请求刷新“${source.name}”`)
      await loadSources()
    } catch (err: any) {
      setError(err?.message || '刷新订阅失败')
    }
  }

  const setNodeEnabled = async (node: ProxyNode) => {
    try {
      await apiFetch(`/proxy-nodes/nodes/${encodeURIComponent(node.name)}`, {
        method: 'PUT',
        body: JSON.stringify({ enabled: !node.enabled }),
      })
      await loadNodes()
    } catch (err: any) {
      setError(err?.message || '更新节点状态失败')
    }
  }

  const activateNode = async (node: ProxyNode) => {
    try {
      await apiFetch(`/proxy-nodes/nodes/${encodeURIComponent(node.name)}/activate`, { method: 'POST' })
      setNotice(`已切换到节点“${node.name}”`)
      await loadNodes()
    } catch (err: any) {
      setError(err?.message || '切换节点失败')
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-sm text-[var(--text-muted)]">管理 Mihomo 订阅来源，以及订阅同步后的全部代理节点。</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => tab === 'sources' ? void loadSources() : void loadNodes(true)} disabled={loading}>
          <RefreshCw className={`mr-1.5 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />刷新
        </Button>
      </div>

      <div className="inline-flex border-b border-[var(--border)]">
        <button className={`border-b-2 px-4 py-2 text-sm ${tab === 'sources' ? 'border-[var(--accent)] text-[var(--accent)]' : 'border-transparent text-[var(--text-muted)]'}`} onClick={() => setTab('sources')}>
          代理 URL 管理
        </button>
        <button className={`border-b-2 px-4 py-2 text-sm ${tab === 'nodes' ? 'border-[var(--accent)] text-[var(--accent)]' : 'border-transparent text-[var(--text-muted)]'}`} onClick={() => setTab('nodes')}>
          全部节点管理
        </button>
      </div>

      {notice && <div className="flex items-center gap-2 break-all border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-400"><Check className="h-4 w-4 shrink-0" />{notice}</div>}
      {error && <div className="flex items-start gap-2 break-all border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300"><CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />{error}</div>}

      {tab === 'sources' ? (
        <section className="overflow-hidden border border-[var(--border)] bg-[var(--bg-card)]">
          <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-3">
            <div className="text-sm font-medium text-[var(--text-primary)]">订阅来源 <span className="text-[var(--text-muted)]">{sources.length}</span></div>
            <Button size="sm" onClick={openCreate}><Plus className="mr-1.5 h-4 w-4" />添加 URL</Button>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead className="bg-[var(--bg-pane)]/60 text-left text-xs text-[var(--text-muted)]"><tr><th className="px-4 py-3">名称</th><th className="px-4 py-3">订阅 URL</th><th className="px-4 py-3">更新间隔</th><th className="px-4 py-3">节点数</th><th className="px-4 py-3">状态</th><th className="px-4 py-3 text-right">操作</th></tr></thead>
              <tbody className="divide-y divide-[var(--border)]">
                {sources.map(source => <tr key={source.name} className="hover:bg-[var(--bg-hover)]/50">
                  <td className="px-4 py-3 font-medium text-[var(--text-primary)]">{source.name}</td>
                  <td className="max-w-[420px] break-all px-4 py-3 font-mono text-xs text-[var(--text-secondary)]">
                    <MaskedField value={source.url} type="url" canCopy copyLabel="复制完整订阅 URL" />
                  </td>
                  <td className="px-4 py-3 text-[var(--text-secondary)]">{source.interval}s</td>
                  <td className="px-4 py-3 text-[var(--text-secondary)]">{source.node_count}</td>
                  <td className="px-4 py-3">{source.runtime_available ? <span className="text-emerald-400">已加载</span> : <span className="text-[var(--text-muted)]">待加载</span>}</td>
                  <td className="px-4 py-3"><div className="flex justify-end gap-1"><Button variant="ghost" size="sm" title="刷新订阅" onClick={() => void refreshSource(source)}><RefreshCw className="h-4 w-4" /></Button><Button variant="ghost" size="sm" title="编辑" onClick={() => openEdit(source)}><Pencil className="h-4 w-4" /></Button><Button variant="ghost" size="sm" title="删除" onClick={() => void deleteSource(source)}><Trash2 className="h-4 w-4 text-red-400" /></Button></div></td>
                </tr>)}
                {!loading && sources.length === 0 && <tr><td colSpan={6} className="px-4 py-14 text-center text-[var(--text-muted)]">暂无代理订阅 URL</td></tr>}
              </tbody>
            </table>
          </div>
        </section>
      ) : (
        <section className="overflow-hidden border border-[var(--border)] bg-[var(--bg-card)]">
          <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-3"><div className="text-sm font-medium text-[var(--text-primary)]">全部节点 <span className="text-[var(--text-muted)]">{nodes.length}</span></div><span className="text-xs text-[var(--text-muted)]">停用节点不会被注册代理池分配</span></div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[860px] text-sm">
              <thead className="bg-[var(--bg-pane)]/60 text-left text-xs text-[var(--text-muted)]"><tr><th className="px-4 py-3">节点</th><th className="px-4 py-3">来源</th><th className="px-4 py-3">协议</th><th className="px-4 py-3">状态</th><th className="px-4 py-3">延迟</th><th className="px-4 py-3">最近测速</th><th className="px-4 py-3 text-right">操作</th></tr></thead>
              <tbody className="divide-y divide-[var(--border)]">
                {nodes.map(node => <tr key={node.name} className={`${node.enabled ? '' : 'opacity-50'} hover:bg-[var(--bg-hover)]/50`}>
                  <td className="px-4 py-3 font-medium text-[var(--text-primary)]">{node.name}{node.selected && <span className="ml-2 text-xs text-[var(--accent)]">当前</span>}</td>
                  <td className="px-4 py-3 text-xs text-[var(--text-muted)]">{node.provider || '-'}</td><td className="px-4 py-3 text-[var(--text-secondary)]">{node.type}</td>
                  <td className="px-4 py-3">{!node.enabled ? <span className="text-[var(--text-muted)]">已停用</span> : node.alive === false ? <span className="text-red-400">离线</span> : node.alive === true ? <span className="text-emerald-400">可用</span> : <span className="text-amber-400">未测速</span>}</td>
                  <td className="px-4 py-3 text-[var(--text-secondary)]">{node.delay ? `${node.delay}ms` : '-'}</td><td className="px-4 py-3 text-xs text-[var(--text-muted)]">{formatDate(node.last_test)}</td>
                  <td className="px-4 py-3"><div className="flex justify-end gap-1"><Button variant="ghost" size="sm" title={node.enabled ? '停用节点' : '启用节点'} onClick={() => void setNodeEnabled(node)}><Power className={`h-4 w-4 ${node.enabled ? 'text-emerald-400' : 'text-[var(--text-muted)]'}`} /></Button><Button variant="ghost" size="sm" title="切换为当前节点" disabled={!node.enabled || node.alive === false || node.selected} onClick={() => void activateNode(node)}>切换</Button></div></td>
                </tr>)}
                {!loading && nodes.length === 0 && <tr><td colSpan={7} className="px-4 py-14 text-center text-[var(--text-muted)]">暂无节点或 Mihomo 控制器不可用</td></tr>}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {sourceForm && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={() => setSourceForm(null)}><div className="w-full max-w-lg border border-[var(--border)] bg-[var(--bg-card)] p-5 shadow-xl" onClick={event => event.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between"><h2 className="text-base font-semibold text-[var(--text-primary)]">{editingSource ? '编辑代理订阅' : '添加代理订阅'}</h2><button onClick={() => setSourceForm(null)} title="关闭"><X className="h-4 w-4 text-[var(--text-muted)]" /></button></div>
        <div className="space-y-3"><label className="grid gap-1 text-sm text-[var(--text-secondary)]">名称<input value={sourceForm.name} onChange={event => setSourceForm(form => form && ({ ...form, name: event.target.value }))} className="control-surface" /></label><label className="grid gap-1 text-sm text-[var(--text-secondary)]">订阅 URL<input value={sourceForm.url} onChange={event => setSourceForm(form => form && ({ ...form, url: event.target.value }))} placeholder="https://example.com/subscription" className="control-surface" /></label><label className="grid gap-1 text-sm text-[var(--text-secondary)]">自动更新间隔（秒）<input type="number" min={60} max={86400} value={sourceForm.interval} onChange={event => setSourceForm(form => form && ({ ...form, interval: event.target.value }))} className="control-surface" /></label></div>
        <div className="mt-5 flex justify-end gap-2"><Button variant="outline" onClick={() => setSourceForm(null)}>取消</Button><Button onClick={() => void saveSource()} disabled={saving}>{saving && <LoaderCircle className="mr-1.5 h-4 w-4 animate-spin" />}{saving ? '保存中' : '保存'}</Button></div>
      </div></div>}
    </div>
  )
}
