import { StrictMode, useCallback, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { ConfigProvider, App as AntdApp, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { App } from './App'
import './index.css'

type ThemeName = 'light' | 'dark'

function resolveInitialTheme(): ThemeName {
  const saved = localStorage.getItem('config-check-theme')
  if (saved === 'dark' || saved === 'light') return saved
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

/**
 * 主题放在这里而不是 App 内部：antd 的算法（defaultAlgorithm / darkAlgorithm）
 * 必须在 ConfigProvider 这一层切换，页面组件只消费 token，不自己判断颜色。
 */
function Root() {
  const [themeName, setThemeName] = useState<ThemeName>(resolveInitialTheme)

  const toggleTheme = useCallback(() => {
    setThemeName((current) => {
      const next: ThemeName = current === 'dark' ? 'light' : 'dark'
      localStorage.setItem('config-check-theme', next)
      document.documentElement.dataset.theme = next
      return next
    })
  }, [])

  // 首帧之前就把 data-theme 写进根节点，避免深色模式下闪白
  if (document.documentElement.dataset.theme !== themeName) {
    document.documentElement.dataset.theme = themeName
  }

  const dark = themeName === 'dark'

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: {
          colorPrimary: '#4f46e5',
          borderRadius: 10,
          fontFamily:
            '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
        },
        components: {
          Card: { borderRadiusLG: 16 },
          Table: { headerBg: dark ? '#151d2e' : '#f8fafc' },
        },
      }}
    >
      <AntdApp>
        <App themeName={themeName} onToggleTheme={toggleTheme} />
      </AntdApp>
    </ConfigProvider>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)
