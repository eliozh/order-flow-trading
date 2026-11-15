/** The settings section index shared by the shell and every settings screen. */

export type SettingsSectionId = 'scan'

interface SettingsSectionNavProps {
  active: 'overview' | SettingsSectionId
}

const ITEMS: { id: 'overview' | SettingsSectionId; href: string; label: string }[] = [
  { id: 'overview', href: '/settings', label: '概览 / Overview' },
  { id: 'scan', href: '/settings/scan', label: '扫描与排程 / Scan' },
]

export function SettingsSectionNav({ active }: SettingsSectionNavProps) {
  return (
    <nav className="settings-nav" aria-label="Settings sections">
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
