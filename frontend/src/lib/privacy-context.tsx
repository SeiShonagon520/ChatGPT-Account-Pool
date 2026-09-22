import React, { createContext, useContext, useEffect, useMemo, useState } from 'react'
import { Check, Copy, Eye, EyeOff } from 'lucide-react'

interface PrivacyContextType {
  privacyMode: boolean
  setPrivacyMode: (v: boolean) => void
  togglePrivacyMode: () => void
  maskEmail: (email?: string, forceReveal?: boolean) => string
  maskUrl: (url?: string, forceReveal?: boolean) => string
  maskSecret: (secret?: string, forceReveal?: boolean) => string
  maskPassword: (pw?: string, forceReveal?: boolean) => string
}

const PrivacyContext = createContext<PrivacyContextType | null>(null)

const STORAGE_KEY = 'chatgpt_account_pool_privacy_mode'

export function maskEmail(email?: string, forceReveal = false): string {
  if (!email) return '-'
  if (forceReveal) return email
  const parts = email.split('@')
  if (parts.length !== 2) return '••••••••'
  const [name, domain] = parts
  if (name.length <= 4) {
    return `${name.slice(0, 1)}****@${domain}`
  }
  const prefix = name.slice(0, 2)
  const suffix = name.slice(-2)
  return `${prefix}****${suffix}@${domain}`
}

export function maskUrl(url?: string, forceReveal = false): string {
  if (!url) return '-'
  if (forceReveal) return url
  try {
    const parsed = new URL(url)
    const domain = parsed.host
    const pathSegments = parsed.pathname.split('/').filter(Boolean)

    // Check query params token
    if (parsed.search) {
      return `${parsed.protocol}//${domain}${parsed.pathname}?token=••••••••`
    }
    // Check path token like /mp4/755fdc40d3ae9b998dd7bcb4cd7ae75b
    if (pathSegments.length > 0) {
      const lastSeg = pathSegments[pathSegments.length - 1]
      if (lastSeg.length >= 10) {
        const maskedSeg = `${lastSeg.slice(0, 4)}••••${lastSeg.slice(-4)}`
        const leading = pathSegments.slice(0, -1).join('/')
        return `${parsed.protocol}//${domain}/${leading ? leading + '/' : ''}${maskedSeg}`
      }
    }
    return `${parsed.protocol}//${domain}/••••••••`
  } catch {
    if (url.length <= 16) return '••••••••'
    return `${url.slice(0, 8)}••••${url.slice(-6)}`
  }
}

export function maskSecret(secret?: string, forceReveal = false): string {
  if (!secret) return '-'
  if (forceReveal) return secret
  if (secret.length <= 8) return '••••••••'
  return `${secret.slice(0, 4)}••••${secret.slice(-4)}`
}

export function maskPassword(pw?: string, forceReveal = false): string {
  if (!pw) return '-'
  if (forceReveal) return pw
  return '••••••••'
}

export function PrivacyProvider({ children }: { children: React.ReactNode }) {
  const [privacyMode, setPrivacyModeState] = useState<boolean>(() => {
    const saved = localStorage.getItem(STORAGE_KEY)
    // 默认开启隐私保护模式；若用户之前显式保存过 'false' 则遵循用户选择
    return saved === null ? true : saved === 'true'
  })

  const setPrivacyMode = (v: boolean) => {
    setPrivacyModeState(v)
    localStorage.setItem(STORAGE_KEY, String(v))
  }

  const togglePrivacyMode = () => {
    setPrivacyMode(!privacyMode)
  }

  const value = useMemo(
    () => ({
      privacyMode,
      setPrivacyMode,
      togglePrivacyMode,
      maskEmail,
      maskUrl,
      maskSecret,
      maskPassword,
    }),
    [privacyMode]
  )

  return <PrivacyContext.Provider value={value}>{children}</PrivacyContext.Provider>
}

export function usePrivacy() {
  const context = useContext(PrivacyContext)
  if (!context) {
    throw new Error('usePrivacy must be used within PrivacyProvider')
  }
  return context
}

/**
 * 智能防泄露文本组件：
 * 支持自动脱敏、单项眼睛图标临时揭开、以及复制真实完整内容。
 */
export function MaskedField({
  value,
  type = 'text',
  className = '',
  canCopy = false,
  copyLabel = '',
}: {
  value?: string
  type?: 'email' | 'url' | 'password' | 'secret' | 'text'
  className?: string
  canCopy?: boolean
  copyLabel?: string
}) {
  const { privacyMode } = usePrivacy()
  const [revealed, setRevealed] = useState(false)
  const [copied, setCopied] = useState(false)

  // 当全局隐私模式开启时，收起任何单项临时明文
  useEffect(() => {
    if (privacyMode) setRevealed(false)
  }, [privacyMode])

  const displayed = useMemo(() => {
    if (!privacyMode || revealed) return value || '-'
    switch (type) {
      case 'email':
        return maskEmail(value)
      case 'url':
        return maskUrl(value)
      case 'password':
        return maskPassword(value)
      case 'secret':
        return maskSecret(value)
      default:
        return value ? '••••••••' : '-'
    }
  }, [privacyMode, revealed, value, type])

  const handleCopy = (e: React.MouseEvent) => {
    e.stopPropagation()
    if (!value) return
    navigator.clipboard?.writeText(value).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }).catch(() => {})
  }

  return (
    <span className={`inline-flex items-center gap-1.5 ${className}`}>
      <span className="font-mono select-all break-all">{displayed}</span>
      {privacyMode && value && (
        <button
          type="button"
          onClick={() => setRevealed(!revealed)}
          className="p-0.5 text-[var(--text-muted)] transition-colors hover:text-[var(--text-primary)] cursor-pointer"
          title={revealed ? '隐藏明文' : '查看明文'}
        >
          {revealed ? <EyeOff className="h-3.5 w-3.5 text-sky-400" /> : <Eye className="h-3.5 w-3.5" />}
        </button>
      )}
      {canCopy && value && (
        <button
          type="button"
          onClick={handleCopy}
          className="p-0.5 text-[var(--text-muted)] transition-colors hover:text-[var(--text-primary)] cursor-pointer"
          title={copyLabel || '复制完整内容'}
        >
          {copied ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
        </button>
      )}
    </span>
  )
}
