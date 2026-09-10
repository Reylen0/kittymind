/** 主题管理：浅/深切换，localStorage 持久化，默认跟随系统 */
export type Theme = 'light' | 'dark'

const KEY = 'kittymind.theme'

export function resolveInitialTheme(): Theme {
  try {
    const saved = localStorage.getItem(KEY)
    if (saved === 'light' || saved === 'dark') return saved
  } catch { /* localStorage 不可用时走系统偏好 */ }
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

/** 原生窗口控制按钮（titleBarOverlay）在各主题下的配色，需与顶栏背景一致 */
const OVERLAY_COLORS: Record<Theme, { color: string; symbolColor: string }> = {
  light: { color: '#f7f8fa', symbolColor: '#1d1d21' },
  dark:  { color: '#1b1c22', symbolColor: '#e8e9ee' },
}

export function applyTheme(t: Theme) {
  if (t === 'dark') document.documentElement.dataset.theme = 'dark'
  else delete document.documentElement.dataset.theme
  // 同步原生最小化/最大化/关闭按钮区域颜色
  window.kitty?.setNativeTheme?.(OVERLAY_COLORS[t])
}

export function getTheme(): Theme {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'
}

export function toggleTheme(): Theme {
  const next: Theme = getTheme() === 'dark' ? 'light' : 'dark'
  applyTheme(next)
  try { localStorage.setItem(KEY, next) } catch { /* ignore */ }
  return next
}
