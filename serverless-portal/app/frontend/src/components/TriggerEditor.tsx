import { useEffect, useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Button, Input, Textarea } from '@coreai/fluentui-react'
import { api } from '../api'
import {
  SCHEDULE_PRESETS,
  applyInstructionsToMarkdown,
  applyTriggerToMarkdown,
  buildTriggerYaml,
  readAgentTriggerSettings,
} from '../capabilities'
import { Icon } from './ui'

type TriggerMode = 'http' | 'timer'

interface TriggerEditorProps {
  subscription: string
  app: string
  agentName: string
  content: string
  source: 'draft' | 'deployed' | 'none'
  queryKey: unknown[]
}

export function TriggerEditor({ subscription, app, agentName, content, source, queryKey }: TriggerEditorProps) {
  const queryClient = useQueryClient()
  const current = useMemo(() => readAgentTriggerSettings(content), [content])
  const [mode, setMode] = useState<TriggerMode>('http')
  const [route, setRoute] = useState(agentName)
  const [methods, setMethods] = useState('POST')
  const [authLevel, setAuthLevel] = useState('function')
  const [schedule, setSchedule] = useState('0 0 9 * * *')
  const [instructions, setInstructions] = useState('')
  const [touched, setTouched] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    setMode(current.type === 'timer_trigger' ? 'timer' : 'http')
    setRoute(current.args.route || agentName)
    setMethods(current.args.methods || 'POST')
    setAuthLevel(current.args.auth_level || 'function')
    setSchedule(current.args.schedule || '0 0 9 * * *')
    setInstructions(current.instructions)
    setTouched(false)
    setError('')
    setSaved(false)
  }, [agentName, current])

  const nextContent = useMemo(() => {
    const trigger = mode === 'timer'
      ? buildTriggerYaml('timer', { schedule }, current.type === 'timer_trigger' ? current.args : {})
      : buildTriggerYaml('http', { route, methods, auth_level: authLevel }, current.type === 'http_trigger' ? current.args : {})
    const withTrigger = applyTriggerToMarkdown(content, trigger)
    return mode === 'timer' ? applyInstructionsToMarkdown(withTrigger, instructions) : withTrigger
  }, [authLevel, content, current, instructions, methods, mode, route, schedule])

  const dirty = touched && nextContent !== content
  const incomplete = mode === 'timer' ? !schedule.trim() || !instructions.trim() : !route.trim()

  const save = async () => {
    setBusy(true)
    setError('')
    setSaved(false)
    try {
      const validation = await api.validateAgentMd(nextContent)
      if (!validation.ok || validation.errors.length) {
        setError(validation.errors.map((issue) => `${issue.path}: ${issue.message}`).join(' '))
        return
      }
      await api.saveAgentDefinition({ subscription, app, name: agentName, content: nextContent })
      await queryClient.invalidateQueries({ queryKey })
      setSaved(true)
    } catch (caught) {
      setError((caught as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="trigger-editor card">
      <div className="trigger-editor-head">
        <div>
          <h3>Trigger definition</h3>
          <p>Choose how this Hosted Skill starts. Changes are saved to <span className="mono">{agentName}.agent.md</span> as a draft.</p>
        </div>
        <span className={'badge ' + (source === 'draft' ? 'amber' : 'green')}>
          {source === 'draft' ? 'Draft' : 'Deployed'}
        </span>
      </div>

      <div className="trigger-mode-picker" role="radiogroup" aria-label="Trigger type">
        <button type="button" role="radio" aria-checked={mode === 'http'} className={mode === 'http' ? 'active' : ''} disabled={busy} onClick={() => { if (mode !== 'http') { setMode('http'); setTouched(true) } }}>
          <Icon name="globe" size={18} />
          <span><strong>HTTP endpoint</strong><small>Run when a caller sends a request.</small></span>
        </button>
        <button type="button" role="radio" aria-checked={mode === 'timer'} className={mode === 'timer' ? 'active' : ''} disabled={busy} onClick={() => { if (mode !== 'timer') { setMode('timer'); setTouched(true) } }}>
          <Icon name="clock" size={18} />
          <span><strong>Timer schedule</strong><small>Run automatically on a UTC schedule.</small></span>
        </button>
      </div>

      {mode === 'http' ? (
        <div className="trigger-form-grid">
          <div className="field trigger-route-field">
            <label>Route</label>
            <Input disabled={busy} value={route} onChange={(_, data) => { setRoute(data.value); setTouched(data.value !== route || touched) }} />
            <div className="hint">Callers send requests to this path.</div>
          </div>
          <div className="field">
            <label>Methods</label>
            <Input disabled={busy} value={methods} onChange={(_, data) => { setMethods(data.value); setTouched(data.value !== methods || touched) }} />
            <div className="hint">Comma-separated; defaults to POST.</div>
          </div>
          <div className="field">
            <label>Auth level</label>
            <select disabled={busy} value={authLevel} onChange={(event) => { setAuthLevel(event.target.value); setTouched(event.target.value !== authLevel || touched) }}>
              <option value="function">Function key</option>
              <option value="anonymous">Anonymous</option>
              <option value="admin">Admin key</option>
            </select>
          </div>
        </div>
      ) : (
        <div className="timer-trigger-form">
          <div className="field">
            <label>Schedule preset</label>
            <div className="schedule-grid">
              {SCHEDULE_PRESETS.map((preset) => (
                <button type="button" className={'schedule-option' + (schedule === preset.cron ? ' active' : '')} disabled={busy} key={preset.cron} onClick={() => { if (schedule !== preset.cron) { setSchedule(preset.cron); setTouched(true) } }}>
                  {preset.label}
                </button>
              ))}
            </div>
          </div>
          <div className="field">
            <label>Custom schedule (UTC)</label>
            <Input disabled={busy} value={schedule} onChange={(_, data) => { setSchedule(data.value); setTouched(true) }} placeholder="0 0 9 * * *" />
            <div className="hint">NCRONTAB: second minute hour day month weekday.</div>
          </div>
          <div className="field">
            <label>What should this skill do each time it runs?</label>
            <Textarea disabled={busy} resize="vertical" rows={10} value={instructions} onChange={(_, data) => { setInstructions(data.value); setTouched(true) }} />
            <div className="hint">Required. Describe inputs, actions, output destination, empty results, and failure behavior.</div>
          </div>
          <div className="note"><strong>Scheduled runs have no HTTP response.</strong><br />Instructions should produce an outcome such as sending a report, writing data, or posting a message. Each run starts a new session.</div>
        </div>
      )}

      {error && <div className="gh-err" role="alert">{error}</div>}
      {saved && <div className="note ok" role="status">Trigger draft saved. Deploy the app to make it live.</div>}
      <div className="trigger-editor-actions">
        <span className="muted">{dirty ? 'Unsaved trigger changes' : 'No changes'}</span>
        <Button appearance="primary" disabled={busy || incomplete || !dirty} onClick={() => void save()}>
          {busy ? 'Saving trigger…' : 'Save trigger draft'}
        </Button>
      </div>
    </div>
  )
}