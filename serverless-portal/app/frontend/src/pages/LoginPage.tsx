// Sign-in gate shown to unauthenticated users. Sign-in is only ever started by
// the user clicking the button (never automatically). A fallback lets a user
// paste an ARM access token to try the portal without the MSAL redirect.
//
// Migrated to the CoreAI Design System (Fluent v9): Button / Text / Textarea
// from the @coreai/fluentui-react barrel, styled with makeStyles + Fluent
// tokens so it inherits the CoreAI brand ramp, neutrals, and Aptos typography.

import { useState } from 'react'
import { Button, Text, Textarea, makeStyles, shorthands, tokens } from '@coreai/fluentui-react'
import { signIn, setManualToken, validateArmToken } from '../auth'
import { CopyButton } from '../components/SourceEditor'
import { Icon } from '../components/ui'

const useStyles = makeStyles({
  root: {
    minHeight: '100vh',
    display: 'grid',
    placeItems: 'center',
    ...shorthands.padding('24px'),
    backgroundImage: `radial-gradient(1000px 520px at 50% -10%, ${tokens.colorBrandBackground} 0%, ${tokens.colorNeutralBackground2} 58%)`,
  },
  card: {
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'stretch',
    rowGap: '10px',
    width: '100%',
    maxWidth: '400px',
    boxSizing: 'border-box',
    ...shorthands.padding('40px', '36px'),
    backgroundColor: tokens.colorNeutralBackground1,
    ...shorthands.border('1px', 'solid', tokens.colorNeutralStroke2),
    ...shorthands.borderRadius(tokens.borderRadiusXLarge),
    boxShadow: tokens.shadow16,
  },
  mark: {
    alignSelf: 'center',
    width: '56px',
    height: '56px',
    marginBottom: '8px',
    display: 'grid',
    placeItems: 'center',
    color: tokens.colorNeutralForegroundOnBrand,
    backgroundColor: tokens.colorBrandBackground,
    ...shorthands.borderRadius(tokens.borderRadiusXLarge),
    boxShadow: tokens.shadow8,
  },
  title: { textAlign: 'center' },
  subtitle: { textAlign: 'center', marginBottom: '14px', color: tokens.colorNeutralForeground2 },
  altRow: { display: 'flex', justifyContent: 'center', marginTop: '6px' },
  tokenPanel: {
    marginTop: '6px',
    textAlign: 'left',
    ...shorthands.borderTop('1px', 'solid', tokens.colorNeutralStroke2),
    paddingTop: '14px',
    display: 'flex',
    flexDirection: 'column',
    rowGap: '10px',
  },
  hint: { color: tokens.colorNeutralForeground2, fontSize: tokens.fontSizeBase200 },
  cmdRow: { display: 'flex', alignItems: 'center', columnGap: '6px' },
  cmd: {
    flexGrow: 1,
    minWidth: '0',
    fontFamily: tokens.fontFamilyMonospace,
    fontSize: tokens.fontSizeBase100,
    backgroundColor: tokens.colorNeutralBackground3,
    ...shorthands.border('1px', 'solid', tokens.colorNeutralStroke2),
    ...shorthands.borderRadius(tokens.borderRadiusMedium),
    ...shorthands.padding('8px', '10px'),
    overflowX: 'auto',
    whiteSpace: 'nowrap',
    color: tokens.colorNeutralForeground1,
  },
  error: { color: tokens.colorPaletteRedForeground1, fontSize: tokens.fontSizeBase200 },
  warn: { color: tokens.colorNeutralForeground3, fontSize: tokens.fontSizeBase100 },
})

export default function LoginPage() {
  const styles = useStyles()
  const [busy, setBusy] = useState(false)
  const [showToken, setShowToken] = useState(false)
  const [token, setToken] = useState('')
  const [tokenError, setTokenError] = useState<string | null>(null)

  const onSignIn = async () => {
    setBusy(true)
    try {
      await signIn()
    } catch {
      // A failed redirect kick-off leaves us on the login page; re-enable.
      setBusy(false)
    }
  }

  const onUseToken = () => {
    const result = validateArmToken(token)
    if (!result.ok) {
      setTokenError(result.error)
      return
    }
    setTokenError(null)
    // Flips the auth gate (useSyncExternalStore) → the app loads with this token.
    setManualToken(token)
  }

  const cmd = 'az account get-access-token --resource https://management.azure.com --query accessToken -o tsv'

  return (
    <div className={styles.root}>
      <div className={styles.card}>
        <div className={styles.mark}>
          <Icon name="zap" size={24} />
        </div>
        <Text as="h1" size={600} weight="semibold" className={styles.title}>
          Hosted Skills
        </Text>
        <Text as="p" size={300} className={styles.subtitle}>
          Sign in with your Microsoft account to discover serverless agents in your subscriptions.
        </Text>
        <Button appearance="primary" size="large" onClick={() => void onSignIn()} disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </Button>

        <div className={styles.altRow}>
          <Button appearance="subtle" size="small" onClick={() => setShowToken((s) => !s)}>
            {showToken ? 'Hide token option' : 'No sign-in? Use an ARM token'}
          </Button>
        </div>

        {showToken && (
          <div className={styles.tokenPanel}>
            <Text as="p" className={styles.hint}>
              Paste an Azure Resource Manager token to try the portal without signing in. Get one with:
            </Text>
            <div className={styles.cmdRow}>
              <code className={styles.cmd}>{cmd}</code>
              <CopyButton text={cmd} title="Copy the command" />
            </div>
            <Textarea
              value={token}
              onChange={(_, data) => {
                setToken(data.value)
                setTokenError(null)
              }}
              placeholder="Paste the eyJ… access token"
              resize="vertical"
              textarea={{
                'aria-label': 'ARM access token',
                spellCheck: false,
                style: { minHeight: '92px', fontFamily: tokens.fontFamilyMonospace },
              }}
            />
            <Button appearance="primary" disabled={!token.trim()} onClick={onUseToken}>
              Continue with token
            </Button>
            {tokenError && (
              <Text as="p" className={styles.error}>
                {tokenError}
              </Text>
            )}
            <Text as="p" className={styles.warn}>
              <Icon name="alert" size={12} style={{ verticalAlign: '-2px' }} /> An ARM token grants full access
              as you for ~1 hour. It’s kept only in this browser tab and sent only to this portal’s backend.
              Don’t paste tokens into sites you don’t trust.
            </Text>
          </div>
        )}
      </div>
    </div>
  )
}
