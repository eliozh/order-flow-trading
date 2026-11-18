/** The settings section index shared by the shell and every settings screen. */

export type SettingsSectionId = 'scan' | 'risk' | 'budget' | 'llm' | 'telegram' | 'prompts'

interface SettingsSectionNavProps {
  active: 'overview' | SettingsSectionId
}

const ITEMS: { id: 'overview' | SettingsSectionId; href: string; label: string }[] = [
  { id: 'overview', href: '/settings', label: '概览 / Overview' },
  { id: 'scan', href: '/settings/scan', label: '扫描与排程 / Scan' },
  { id: 'risk', href: '/settings/risk', label: '风险上限 / Risk' },
  { id: 'budget', href: '/settings/budget', label: '预算阈值 / Budget' },
  { id: 'llm', href: '/settings/llm', label: 'LLM providers / LLM' },
  { id: 'telegram', href: '/settings/telegram', label: 'Telegram / Telegram' },
  { id: 'prompts', href: '/settings/prompts', label: 'Prompt 模板 / Prompts' },
]

export function SettingsSectionNav({ active }: SettingsSectionNavProps) {
  return (
    <nav className="settings-nav" aria-label="设置分区导航">
      <ul>
        {ITEMS.map((item) => (
          <li key={item.id}>
            <a href={item.href} aria-current={active === item.id ? 'page' : undefined}>
              {item.label}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  )
}
