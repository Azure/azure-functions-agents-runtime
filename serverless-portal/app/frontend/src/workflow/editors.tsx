// Per-component editors — every node type is fully editable here.
//
// Each editor receives the selected node and an `onChange(patch)` that merges a
// partial node (and/or config) into the workflow. The dispatcher `NodeEditor`
// picks the editor for the node's type.

import { useState } from 'react'
import { type WorkflowNode, type NodeType, NODE_META } from './types'
import { Field, TextInput, TextArea, Select, Toggle, StringList } from './fields'

type Patch = Partial<WorkflowNode>
export type GenerateResult = { code?: string; instructions?: string }
type EditorProps = {
  node: WorkflowNode
  onChange: (patch: Patch) => void
  onDelete: () => void
  onGenerate?: (node: WorkflowNode) => Promise<GenerateResult>
}

function cfg(node: WorkflowNode) {
  return (node.config ?? {}) as Record<string, unknown>
}
function str(v: unknown): string {
  return v == null ? '' : String(v)
}
function arr(v: unknown): string[] {
  return Array.isArray(v) ? (v as string[]) : []
}

// Shared name + a merge helper for config changes.
function useConfigMerge(node: WorkflowNode, onChange: (p: Patch) => void) {
  return (partial: Record<string, unknown>) => onChange({ config: { ...cfg(node), ...partial } })
}

function NameField({ node, onChange }: { node: WorkflowNode; onChange: (p: Patch) => void }) {
  return (
    <Field label="Name">
      <TextInput value={node.name} onChange={(v) => onChange({ name: v })} />
    </Field>
  )
}

// "✨ Generate" affordance — stage-2 codegen for a single node. Fills the tool
// body or agent instructions from the workflow intent via the configured model.
function GenerateButton({
  node,
  onGenerate,
  apply,
  label,
}: {
  node: WorkflowNode
  onGenerate?: (node: WorkflowNode) => Promise<GenerateResult>
  apply: (r: GenerateResult) => void
  label: string
}) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  if (!onGenerate) return null
  return (
    <div style={{ margin: '2px 0 10px' }}>
      <button
        type="button"
        className="btn sm"
        disabled={busy}
        onClick={async () => {
          setBusy(true)
          setErr(null)
          try {
            apply(await onGenerate(node))
          } catch (e) {
            setErr((e as Error).message)
          } finally {
            setBusy(false)
          }
        }}
      >
        {busy ? '✨ Generating…' : label}
      </button>
      {err && <div className="note" style={{ marginTop: 6 }}>{err}</div>}
    </div>
  )
}

const TRIGGER_KINDS = [
  'httpTrigger',
  'timerTrigger',
  'connectorTrigger',
  'queueTrigger',
  'serviceBusTrigger',
  'blobTrigger',
  'eventHubTrigger',
  'cosmosDBTrigger',
]

function TriggerEditor({ node, onChange }: EditorProps) {
  const c = cfg(node)
  const merge = useConfigMerge(node, onChange)
  return (
    <>
      <NameField node={node} onChange={onChange} />
      <Field label="Trigger kind" hint="How this workflow is invoked.">
        <Select
          value={node.kind}
          onChange={(v) => onChange({ kind: v })}
          options={TRIGGER_KINDS.map((k) => ({ value: k, label: k }))}
        />
      </Field>
      {node.kind === 'httpTrigger' && (
        <>
          <Field label="Route" hint="Served at https://<app>/<route> (routePrefix is empty).">
            <TextInput value={str(c.route)} onChange={(v) => merge({ route: v })} mono placeholder="run" />
          </Field>
          <Field label="Methods">
            <StringList values={arr(c.methods)} onChange={(v) => merge({ methods: v })} placeholder="POST" />
          </Field>
        </>
      )}
      {node.kind === 'timerTrigger' && (
        <Field label="Schedule (NCRONTAB)" hint="e.g. 0 0 8 * * 1-5 — weekdays at 08:00.">
          <TextInput value={str(c.schedule)} onChange={(v) => merge({ schedule: v })} mono />
        </Field>
      )}
      {node.kind === 'connectorTrigger' && (
        <>
          <Field label="Connector">
            <TextInput value={str(c.connector)} onChange={(v) => merge({ connector: v })} placeholder="outlook" />
          </Field>
          <Field label="Event">
            <TextInput value={str(c.event)} onChange={(v) => merge({ event: v })} placeholder="messageReceived" />
          </Field>
        </>
      )}
      {(node.kind === 'queueTrigger' || node.kind === 'serviceBusTrigger') && (
        <Field label="Queue">
          <TextInput value={str(c.queue)} onChange={(v) => merge({ queue: v })} mono />
        </Field>
      )}
      {node.kind === 'blobTrigger' && (
        <Field label="Path">
          <TextInput value={str(c.path)} onChange={(v) => merge({ path: v })} mono placeholder="incoming/{name}" />
        </Field>
      )}
    </>
  )
}

function AgentEditor({ node, onChange, onGenerate }: EditorProps) {
  const c = cfg(node)
  const merge = useConfigMerge(node, onChange)
  return (
    <>
      <NameField node={node} onChange={onChange} />
      <Field label="Source file" hint="The generated *.agent.md filename.">
        <TextInput value={str(c.sourceFile)} onChange={(v) => merge({ sourceFile: v })} mono />
      </Field>
      <Field label="Instructions" hint="The agent's system prompt / task.">
        <TextArea value={str(c.instructions)} onChange={(v) => merge({ instructions: v })} rows={6} />
      </Field>
      <GenerateButton
        node={node}
        onGenerate={onGenerate}
        label="✨ Generate instructions"
        apply={(r) => r.instructions && merge({ instructions: r.instructions })}
      />
      <Field label="Skills" hint="Skill nodes this agent grounds in.">
        <StringList values={arr(c.skills)} onChange={(v) => merge({ skills: v })} placeholder="skill name" />
      </Field>
      <Field label="Tools" hint="Tool nodes this agent may call.">
        <StringList values={arr(c.tools)} onChange={(v) => merge({ tools: v })} placeholder="tool name" />
      </Field>
      <Field label="Endpoints">
        <Toggle
          checked={!!c.builtinEndpoints}
          onChange={(v) => merge({ builtinEndpoints: v })}
          label="Expose built-in chat UI / API"
        />
      </Field>
      <Field label="Durable workflows">
        <Toggle
          checked={!!c.workflows}
          onChange={(v) => merge({ workflows: v })}
          label="Allow LLM-authored durable sub-workflows"
        />
      </Field>
    </>
  )
}

function ToolEditor({ node, onChange, onGenerate }: EditorProps) {
  const c = cfg(node)
  const merge = useConfigMerge(node, onChange)
  return (
    <>
      <NameField node={node} onChange={onChange} />
      <Field label="Signature" hint="Python function signature.">
        <TextInput value={str(c.signature)} onChange={(v) => merge({ signature: v })} mono />
      </Field>
      <Field label="Implementation" hint="The @tool function body.">
        <TextArea value={str(c.code)} onChange={(v) => merge({ code: v })} rows={9} mono />
      </Field>
      <GenerateButton
        node={node}
        onGenerate={onGenerate}
        label="✨ Generate implementation"
        apply={(r) => r.code && merge({ code: r.code })}
      />
    </>
  )
}

function SkillEditor({ node, onChange }: EditorProps) {
  const c = cfg(node)
  const merge = useConfigMerge(node, onChange)
  return (
    <>
      <NameField node={node} onChange={onChange} />
      <Field label="Path">
        <TextInput value={str(c.path)} onChange={(v) => merge({ path: v })} mono placeholder="skills/<name>/SKILL.md" />
      </Field>
      <Field label="Summary" hint="What knowledge this skill packages.">
        <TextArea value={str(c.summary)} onChange={(v) => merge({ summary: v })} rows={5} />
      </Field>
    </>
  )
}

function McpEditor({ node, onChange }: EditorProps) {
  const c = cfg(node)
  const merge = useConfigMerge(node, onChange)
  return (
    <>
      <NameField node={node} onChange={onChange} />
      <Field label="Server name">
        <TextInput value={str(c.server)} onChange={(v) => merge({ server: v })} />
      </Field>
      <Field label="URL">
        <TextInput value={str(c.url)} onChange={(v) => merge({ url: v })} mono placeholder="https://…" />
      </Field>
      <Field label="Allowed tools">
        <StringList values={arr(c.tools)} onChange={(v) => merge({ tools: v })} />
      </Field>
    </>
  )
}

function OutputEditor({ node, onChange }: EditorProps) {
  const c = cfg(node)
  const merge = useConfigMerge(node, onChange)
  return (
    <>
      <NameField node={node} onChange={onChange} />
      <Field label="Output kind">
        <Select
          value={node.kind}
          onChange={(v) => onChange({ kind: v })}
          options={[
            { value: 'connector', label: 'Connector action' },
            { value: 'email', label: 'Send email' },
            { value: 'queue', label: 'Enqueue message' },
            { value: 'blob', label: 'Store result' },
            { value: 'http_response', label: 'Return HTTP response' },
          ]}
        />
      </Field>
      {node.kind === 'connector' && (
        <>
          <Field label="Connector">
            <TextInput value={str(c.connector)} onChange={(v) => merge({ connector: v })} placeholder="servicenow" />
          </Field>
          <Field label="Action">
            <TextInput value={str(c.action)} onChange={(v) => merge({ action: v })} placeholder="createIncident" />
          </Field>
        </>
      )}
      {node.kind === 'blob' && (
        <Field label="Container">
          <TextInput value={str(c.container)} onChange={(v) => merge({ container: v })} mono />
        </Field>
      )}
      {node.kind === 'queue' && (
        <Field label="Queue">
          <TextInput value={str(c.queue)} onChange={(v) => merge({ queue: v })} mono />
        </Field>
      )}
    </>
  )
}

function RouterEditor({ node, onChange }: EditorProps) {
  const c = cfg(node)
  const merge = useConfigMerge(node, onChange)
  return (
    <>
      <NameField node={node} onChange={onChange} />
      <Field label="Branch on field">
        <TextInput value={str(c.on)} onChange={(v) => merge({ on: v })} mono placeholder="category" />
      </Field>
      <Field label="Cases" hint="value -> nodeId">
        <StringList values={arr(c.cases)} onChange={(v) => merge({ cases: v })} placeholder="billing -> n_agent_1" />
      </Field>
    </>
  )
}

const EDITORS: Record<NodeType, (p: EditorProps) => JSX.Element> = {
  trigger: TriggerEditor,
  agent: AgentEditor,
  tool: ToolEditor,
  skill: SkillEditor,
  mcp: McpEditor,
  output: OutputEditor,
  router: RouterEditor,
}

export function NodeEditor({ node, onChange, onDelete, onGenerate }: EditorProps) {
  const meta = NODE_META[node.type]
  const Editor = EDITORS[node.type] ?? AgentEditor
  return (
    <div>
      <div className="insp-title">
        <span className={`tag ${meta.cls}`}>{meta.label}</span>
        <span className="name">{node.name}</span>
        {node.source && <span className={`src ${node.source}`} style={{ marginLeft: 'auto' }}>{node.source}</span>}
      </div>
      <Editor node={node} onChange={onChange} onDelete={onDelete} onGenerate={onGenerate} />
      <div className="divider" />
      <button className="btn sm danger" onClick={onDelete}>🗑 Delete component</button>
    </div>
  )
}
