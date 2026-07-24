// Starter workflows seeded into an empty store so the gallery is not blank on
// first run. Kept small; users regenerate/edit from here.

export const seedWorkflows = [
  {
    id: 'wf_support_triage',
    name: 'Support Inbox → Ticket Triage',
    description: 'Classify an incoming support email, look up the customer, draft a reply, and open a ticket.',
    version: 3,
    status: 'published',
    createdBy: 'demo',
    createdAt: '2026-07-18T09:12:00Z',
    updatedAt: '2026-07-22T16:40:00Z',
    prompt:
      'When a support email arrives in our Outlook shared mailbox, classify it, look up the customer by sender email, draft a friendly reply grounded in our support taxonomy, and open a ticket with the category and a short summary.',
    target: { functionApp: 'func-agents-lm5hnf57csop2', resourceGroup: 'rg-ynabcategorizer', provider: 'foundry', model: '$FOUNDRY_MODEL' },
    inputs: [
      { id: 'email', label: 'Incoming email (subject + body)', type: 'textarea', required: true, placeholder: 'Paste an email to test…' },
      { id: 'sender', label: 'Sender address', type: 'text', required: true, placeholder: 'customer@example.com' },
    ],
    nodes: [
      { id: 'n_trigger', type: 'trigger', kind: 'connectorTrigger', name: 'New email (Outlook)', source: 'generated', position: { x: 0, y: 1 }, config: { connector: 'outlook', event: 'messageReceived', mailbox: 'support@contoso.com' } },
      { id: 'n_skill', type: 'skill', kind: 'skill', name: 'support-taxonomy', source: 'reused', position: { x: 1, y: 2 }, config: { path: 'skills/support-taxonomy/SKILL.md', summary: 'Category definitions, tone guide, and canned-response patterns.' } },
      { id: 'n_classify', type: 'agent', kind: 'agent', name: 'Classifier', source: 'generated', position: { x: 1, y: 1 }, config: { sourceFile: 'classify.agent.md', instructions: 'Classify the support email into billing, bug, how-to, or other and give a one-sentence rationale.', skills: ['support-taxonomy'], tools: [], builtinEndpoints: false } },
      { id: 'n_lookup', type: 'tool', kind: 'tool', name: 'lookup_customer', source: 'generated', position: { x: 2, y: 2 }, config: { signature: 'def lookup_customer(email: str) -> dict', code: '@tool\ndef lookup_customer(email: str) -> dict:\n    """Look up a customer by email."""\n    return crm.find(email)' } },
      { id: 'n_draft', type: 'agent', kind: 'agent', name: 'Reply Drafter', source: 'generated', position: { x: 2, y: 1 }, config: { sourceFile: 'draft.agent.md', instructions: 'Draft a friendly, concise reply using the classification and customer record. Follow the taxonomy tone guide.', skills: ['support-taxonomy'], tools: ['lookup_customer'], builtinEndpoints: true } },
      { id: 'n_ticket', type: 'output', kind: 'connector', name: 'Open ticket', source: 'generated', position: { x: 3, y: 1 }, config: { connector: 'servicenow', action: 'createIncident', fields: ['category', 'summary', 'customerId'] } },
    ],
    edges: [
      { id: 'e1', from: 'n_trigger', to: 'n_classify', label: 'email subject + body' },
      { id: 'e2', from: 'n_skill', to: 'n_classify', label: 'taxonomy' },
      { id: 'e3', from: 'n_classify', to: 'n_draft', label: 'category + rationale' },
      { id: 'e4', from: 'n_lookup', to: 'n_draft', label: 'customer record' },
      { id: 'e5', from: 'n_draft', to: 'n_ticket', label: 'reply + category + summary' },
    ],
    generation: { model: 'gpt-4o', provider: 'openai', skill: 'composer-plan', generatedAt: '2026-07-22T16:39:00Z', generatedNodeIds: ['n_trigger', 'n_classify', 'n_lookup', 'n_draft', 'n_ticket'], reusedNodeIds: ['n_skill'] },
    deployment: null,
  },
  {
    id: 'wf_daily_cost_digest',
    name: 'Daily Azure Cost Digest',
    description: "Every weekday at 8am, summarize yesterday's spend anomalies and post to Teams.",
    version: 1,
    status: 'draft',
    createdBy: 'demo',
    createdAt: '2026-07-23T08:02:00Z',
    updatedAt: '2026-07-23T08:05:00Z',
    prompt:
      "Every weekday at 8am, pull yesterday's Azure cost across my subscriptions, call out anything that spiked more than 20% versus the prior day, write a short digest, and post it to our #finops Teams channel.",
    target: { functionApp: 'func-agents-new', resourceGroup: 'rg-finops', provider: 'foundry', model: '$FOUNDRY_MODEL' },
    inputs: [{ id: 'date', label: 'Report date (test run)', type: 'text', required: false, placeholder: 'defaults to yesterday' }],
    nodes: [
      { id: 'n_timer', type: 'trigger', kind: 'timerTrigger', name: 'Weekdays 08:00', source: 'generated', position: { x: 0, y: 1 }, config: { schedule: '0 0 8 * * 1-5' } },
      { id: 'n_cost', type: 'tool', kind: 'tool', name: 'get_daily_cost', source: 'generated', position: { x: 1, y: 1 }, config: { signature: 'def get_daily_cost(date: str) -> list[dict]', code: '@tool\ndef get_daily_cost(date: str) -> list[dict]:\n    """Per-service Azure cost for the date."""\n    return cost_management.query(date)' } },
      { id: 'n_analyst', type: 'agent', kind: 'agent', name: 'Cost Analyst', source: 'generated', position: { x: 2, y: 1 }, config: { sourceFile: 'analyst.agent.md', instructions: "Compare today's per-service cost to the prior day. Flag any >20% increase and write a concise digest.", skills: [], tools: ['get_daily_cost'], builtinEndpoints: false, workflows: true } },
      { id: 'n_post', type: 'output', kind: 'connector', name: 'Post to Teams', source: 'generated', position: { x: 3, y: 1 }, config: { connector: 'teams', action: 'postMessage', channel: '#finops' } },
    ],
    edges: [
      { id: 'e1', from: 'n_timer', to: 'n_cost', label: 'date' },
      { id: 'e2', from: 'n_cost', to: 'n_analyst', label: 'per-service cost rows' },
      { id: 'e3', from: 'n_analyst', to: 'n_post', label: 'digest markdown' },
    ],
    generation: { model: 'gpt-4o', provider: 'openai', skill: 'composer-plan', generatedAt: '2026-07-23T08:04:00Z', generatedNodeIds: ['n_timer', 'n_cost', 'n_analyst', 'n_post'], reusedNodeIds: [] },
    deployment: null,
  },
]
