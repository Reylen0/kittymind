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

/** 全屏遮罩（rgba(0,0,0,.32)）盖住顶栏后，overlay 区域应呈现的等效混合色。
 *  WCO 是系统层，网页内容（含遮罩）永远画不到它上面——弹窗打开时若不改色，
 *  右上角会留一块亮色，观感是「窗口控制按钮浮在弹窗之上」。 */
const OVERLAY_DIMMED: Record<Theme, { color: string; symbolColor: string }> = {
  light: { color: '#a8a9aa', symbolColor: '#1d1d21' },  // #f7f8fa × (1-.32)
  dark:  { color: '#121317', symbolColor: '#e8e9ee' },  // #1b1c22 × (1-.32)
}

/** 打开/关闭全屏遮罩类弹窗时切换 overlay 配色；关闭时按当前主题还原 */
export function setOverlayDimmed(dimmed: boolean) {
  const t = getTheme()
  window.kitty?.setNativeTheme?.((dimmed ? OVERLAY_DIMMED : OVERLAY_COLORS)[t])
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
