// App chrome: top appbar + collapsible left sidebar. Migrated to the CoreAI
// Design System (Fluent v9) — Button / Avatar / Tooltip from the
// @coreai/fluentui-react barrel, styled with makeStyles + semantic tokens,
// following the CoreAI `templates-shell-and-navigation` patterns. Behavior
// (collapse + persist, theme toggle, nav, sign out) and a11y are preserved;
// product glyphs stay on the local Icon set.

import { type ReactNode, useState } from 'react'
import { NavLink, Link, useNavigate } from 'react-router-dom'
import { Avatar, Button, Tooltip, makeStyles, mergeClasses, shorthands, tokens } from '@coreai/fluentui-react'
import { useIdentity } from '../identity'
import { signOut } from '../auth'
import { useTheme } from '../theme'
import { Icon, type IconName } from './ui'

const useStyles = makeStyles({
  app: { minHeight: '100vh', display: 'flex', flexDirection: 'column', backgroundColor: tokens.colorNeutralBackground2 },
  appbar: {
    position: 'sticky',
    top: 0,
    zIndex: 20,
    height: '56px',
    display: 'flex',
    alignItems: 'center',
    gap: tokens.spacingHorizontalM,
    ...shorthands.padding('0', tokens.spacingHorizontalXL),
    backgroundColor: tokens.colorNeutralBackground1,
    ...shorthands.borderBottom('1px', 'solid', tokens.colorNeutralStroke2),
    '@media (max-width: 700px)': {
      gap: tokens.spacingHorizontalXS,
      ...shorthands.padding('0', tokens.spacingHorizontalS),
    },
  },
  brand: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: tokens.spacingHorizontalS,
    color: tokens.colorNeutralForeground1,
    textDecoration: 'none',
    fontWeight: tokens.fontWeightSemibold,
  },
  brandMark: {
    width: '28px',
    height: '28px',
    display: 'grid',
    placeItems: 'center',
    color: tokens.colorNeutralForegroundOnBrand,
    backgroundColor: tokens.colorBrandBackground,
    ...shorthands.borderRadius(tokens.borderRadiusMedium),
  },
  brandName: {
    fontSize: tokens.fontSizeBase300,
    '@media (max-width: 520px)': { display: 'none' },
  },
  spacer: { flexGrow: 1 },
  actions: {
    display: 'flex',
    alignItems: 'center',
    gap: tokens.spacingHorizontalS,
    '@media (max-width: 700px)': { gap: tokens.spacingHorizontalXXS },
  },
  mobileOptional: { '@media (max-width: 620px)': { display: 'none' } },
  body: {
    flexGrow: 1,
    display: 'flex',
    alignItems: 'flex-start',
    minWidth: 0,
    '@media (max-width: 700px)': { flexDirection: 'column', width: '100%' },
  },
  sidebar: {
    position: 'sticky',
    top: '56px',
    height: 'calc(100vh - 56px)',
    flexShrink: 0,
    width: '236px',
    backgroundColor: tokens.colorNeutralBackground1,
    ...shorthands.borderRight('1px', 'solid', tokens.colorNeutralStroke2),
    display: 'flex',
    flexDirection: 'column',
    overflow: 'hidden',
    transitionProperty: 'width',
    transitionDuration: tokens.durationNormal,
    transitionTimingFunction: tokens.curveEasyEase,
    '@media (max-width: 700px)': {
      position: 'static',
      width: '100%',
      height: 'auto',
      ...shorthands.borderRight('0', 'none', 'transparent'),
      ...shorthands.borderBottom('1px', 'solid', tokens.colorNeutralStroke2),
    },
  },
  sidebarCollapsed: { width: '64px', '@media (max-width: 700px)': { width: '100%' } },
  sidenav: {
    flexGrow: 1,
    ...shorthands.padding(tokens.spacingVerticalM, tokens.spacingHorizontalM),
    display: 'flex',
    flexDirection: 'column',
    gap: tokens.spacingVerticalXXS,
    overflowY: 'auto',
    '@media (max-width: 700px)': {
      flexDirection: 'row',
      flexGrow: 0,
      overflowX: 'auto',
      overflowY: 'hidden',
      ...shorthands.padding(tokens.spacingVerticalS, tokens.spacingHorizontalS),
    },
  },
  groupLabel: {
    fontSize: tokens.fontSizeBase100,
    textTransform: 'uppercase',
    letterSpacing: '0.5px',
    color: tokens.colorNeutralForeground3,
    fontWeight: tokens.fontWeightSemibold,
    ...shorthands.padding(tokens.spacingVerticalS, tokens.spacingHorizontalS, tokens.spacingVerticalXS),
    whiteSpace: 'nowrap',
    '@media (max-width: 700px)': { display: 'none' },
  },
  navLink: {
    display: 'flex',
    alignItems: 'center',
    gap: tokens.spacingHorizontalM,
    ...shorthands.padding(tokens.spacingVerticalSNudge, tokens.spacingHorizontalMNudge),
    ...shorthands.borderRadius(tokens.borderRadiusMedium),
    color: tokens.colorNeutralForeground2,
    fontSize: tokens.fontSizeBase300,
    fontWeight: tokens.fontWeightMedium,
    textDecoration: 'none',
    whiteSpace: 'nowrap',
    ':hover': { backgroundColor: tokens.colorNeutralBackground3, color: tokens.colorNeutralForeground1 },
  },
  navLinkCollapsed: { justifyContent: 'center', gap: 0 },
  navLinkActive: {
    backgroundColor: tokens.colorNeutralBackground3,
    color: tokens.colorNeutralForeground1,
    fontWeight: tokens.fontWeightSemibold,
  },
  navIcon: {
    width: '22px',
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    flexShrink: 0,
    color: tokens.colorNeutralForeground3,
  },
  navIconActive: { color: tokens.colorBrandForeground1 },
  navLabel: { overflow: 'hidden', textOverflow: 'ellipsis' },
  collapseBtn: {
    margin: tokens.spacingHorizontalS,
    justifyContent: 'flex-start',
    '@media (max-width: 700px)': { display: 'none' },
  },
  main: {
    flexGrow: 1,
    minWidth: 0,
    ...shorthands.padding('30px', 'clamp(22px, 4vw, 56px)', '72px'),
    '@media (max-width: 700px)': {
      width: '100%',
      ...shorthands.padding(tokens.spacingVerticalL, tokens.spacingHorizontalM, '56px'),
    },
  },
})

interface NavDef {
  to: string
  icon: IconName
  label: string
}

const NAV_ITEMS: NavDef[] = [
  { to: '/agents', icon: 'grid', label: 'Hosted Skills' },
  { to: '/playground', icon: 'message', label: 'Playground' },
]

function getCollapsed(): boolean {
  try {
    return localStorage.getItem('sap-sidebar') === 'collapsed'
  } catch {
    return false
  }
}

export default function Shell({ children }: { children: ReactNode }) {
  const styles = useStyles()
  const navigate = useNavigate()
  const { identity } = useIdentity()
  const { theme, toggle } = useTheme()
  const [collapsed, setCollapsed] = useState<boolean>(getCollapsed)
  const user = identity?.user
  const userName = user?.name || user?.username
  const userTitle = user ? `${user.name} · ${user.username}` : 'Not signed in'

  const toggleSidebar = () =>
    setCollapsed((c) => {
      const next = !c
      try {
        localStorage.setItem('sap-sidebar', next ? 'collapsed' : 'expanded')
      } catch {
        /* ignore */
      }
      return next
    })

  const renderNav = ({ to, icon, label }: NavDef) => {
    const link = (
      <NavLink
        key={to}
        to={to}
        className={({ isActive }) =>
          mergeClasses(styles.navLink, collapsed && styles.navLinkCollapsed, isActive && styles.navLinkActive)
        }
        title={label}
        aria-label={label}
      >
        {({ isActive }) => (
          <>
            <span className={mergeClasses(styles.navIcon, isActive && styles.navIconActive)}>
              <Icon name={icon} size={17} />
            </span>
            {!collapsed && <span className={styles.navLabel}>{label}</span>}
          </>
        )}
      </NavLink>
    )
    return collapsed ? (
      <Tooltip key={to} content={label} relationship="label" positioning="after">
        {link}
      </Tooltip>
    ) : (
      link
    )
  }

  return (
    <div className={styles.app}>
      <header className={styles.appbar}>
        <Tooltip content={collapsed ? 'Expand sidebar' : 'Collapse sidebar'} relationship="label">
          <Button
            appearance="subtle"
            icon={<Icon name="menu" size={18} />}
            aria-label="Toggle sidebar"
            onClick={toggleSidebar}
          />
        </Tooltip>
        <Link to="/agents" className={styles.brand} title="Hosted Skills">
          <span className={styles.brandMark}>
            <Icon name="zap" size={18} />
          </span>
          <span className={styles.brandName}>Hosted Skills</span>
        </Link>

        <div className={styles.spacer} />

        <div className={styles.actions}>
          <Button
            appearance="primary"
            icon={<Icon name="plus" size={16} />}
            onClick={() => navigate('/create-agent')}
          >
            New Skill
          </Button>
          <Tooltip content={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'} relationship="label">
            <Button
              appearance="subtle"
              icon={theme === 'dark' ? <Icon name="sun" size={17} /> : <Icon name="moon" size={17} />}
              aria-label="Toggle color theme"
              onClick={toggle}
            />
          </Tooltip>
          <Tooltip content={userTitle} relationship="label">
            <Avatar name={userName} size={28} />
          </Tooltip>
          <Button className={styles.mobileOptional} appearance="subtle" onClick={() => void signOut()}>
            Sign out
          </Button>
        </div>
      </header>

      <div className={styles.body}>
        <aside className={mergeClasses(styles.sidebar, collapsed && styles.sidebarCollapsed)}>
          <nav className={styles.sidenav} aria-label="Primary navigation">
            {!collapsed && <div className={styles.groupLabel}>Build</div>}
            {NAV_ITEMS.map(renderNav)}
          </nav>
          <Tooltip content={collapsed ? 'Expand sidebar' : 'Collapse sidebar'} relationship="label" positioning="after">
            <Button
              appearance="subtle"
              className={styles.collapseBtn}
              icon={<Icon name={collapsed ? 'arrowRight' : 'arrowLeft'} size={16} />}
              aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
              aria-expanded={!collapsed}
              onClick={toggleSidebar}
            >
              {!collapsed ? 'Collapse' : undefined}
            </Button>
          </Tooltip>
        </aside>

        <main className={styles.main}>{children}</main>
      </div>
    </div>
  )
}
