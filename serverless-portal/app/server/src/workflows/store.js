// Workflow persistence layer for the Composer.
//
// Holds the versioned workflow documents that stitch runtime components
// together. This local-filesystem implementation stands in for the production
// store (a `workflows` container in the Function App's storage account); the
// surface is deliberately small so it can be swapped for Azure Blob without
// touching callers.
//
// Each workflow is one JSON document keyed by id. Saves bump `version` and
// stamp `updatedAt`; an ETag (derived from the version) gives optimistic
// concurrency so concurrent editors don't clobber each other.

import { fileURLToPath } from 'node:url'
import path from 'node:path'
import fs from 'node:fs/promises'
import crypto from 'node:crypto'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const DATA_DIR = path.resolve(__dirname, '..', '..', '.data', 'workflows')

function etagFor(doc) {
  return `"v${doc.version}-${doc.id}"`
}

async function ensureDir() {
  await fs.mkdir(DATA_DIR, { recursive: true })
}

function safeId(id) {
  // Guard against path traversal — ids are slugs only.
  return String(id).replace(/[^a-zA-Z0-9_-]/g, '')
}

function docPath(id) {
  return path.join(DATA_DIR, `${safeId(id)}.json`)
}

// Version snapshots live in a per-workflow subfolder (mirrors "version items in
// the workflow's partition" once this moves to Cosmos). They're excluded from the
// top-level listing because they aren't `*.json` at the DATA_DIR root.
function versionsDir(id) {
  return path.join(DATA_DIR, safeId(id), 'versions')
}
function versionPath(id, version) {
  return path.join(versionsDir(id), `v${version}.json`)
}

async function writeSnapshot(doc) {
  const dir = versionsDir(doc.id)
  await fs.mkdir(dir, { recursive: true })
  await fs.writeFile(versionPath(doc.id, doc.version), JSON.stringify(doc, null, 2), 'utf-8')
}

// Append a capped history entry (working-copy edit log).
function appendHistory(history, entry) {
  return [...(Array.isArray(history) ? history : []), entry].slice(-100)
}

// Derive the lifecycle status from published vs working-copy versions, unless the
// caller set it explicitly (e.g. a publish).
function deriveStatus(doc) {
  if (doc.publishedVersion == null) return 'draft'
  return doc.version > doc.publishedVersion ? 'published_with_changes' : 'published'
}

/** Raised for concurrency / not-found conditions the router maps to HTTP codes. */
export class StoreError extends Error {
  constructor(code, message) {
    super(message)
    this.code = code // 'not_found' | 'conflict'
  }
}

export function newWorkflowId(name) {
  const slug = String(name || 'workflow')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 40)
  const suffix = crypto.randomBytes(3).toString('hex')
  return `wf_${slug || 'workflow'}_${suffix}`
}

export function newNodeId(kind) {
  return `n_${String(kind || 'node').replace(/[^a-z0-9]/gi, '').slice(0, 12)}_${crypto.randomBytes(2).toString('hex')}`
}

export function newEdgeId() {
  return `e_${crypto.randomBytes(3).toString('hex')}`
}

export async function listWorkflows() {
  await ensureDir()
  const files = await fs.readdir(DATA_DIR)
  const docs = []
  for (const file of files) {
    if (!file.endsWith('.json')) continue
    try {
      const raw = await fs.readFile(path.join(DATA_DIR, file), 'utf-8')
      docs.push(JSON.parse(raw))
    } catch {
      /* skip unreadable/corrupt entries */
    }
  }
  docs.sort((a, b) => String(b.updatedAt || '').localeCompare(String(a.updatedAt || '')))
  // Summaries only — the list view doesn't need full graphs.
  return docs.map((d) => ({
    id: d.id,
    name: d.name,
    description: d.description,
    status: d.status,
    version: d.version,
    publishedVersion: d.publishedVersion ?? null,
    updatedAt: d.updatedAt,
    target: d.target ?? {},
    nodeCount: (d.nodes ?? []).length,
    triggerKinds: [...new Set((d.nodes ?? []).filter((n) => n.type === 'trigger').map((n) => n.kind))],
    componentCounts: (d.nodes ?? []).reduce((acc, n) => {
      acc[n.type] = (acc[n.type] ?? 0) + 1
      return acc
    }, {}),
  }))
}

export async function getWorkflow(id) {
  await ensureDir()
  try {
    const raw = await fs.readFile(docPath(id), 'utf-8')
    return JSON.parse(raw)
  } catch {
    throw new StoreError('not_found', `Workflow '${id}' not found.`)
  }
}

async function writeDoc(doc) {
  await ensureDir()
  doc.etag = etagFor(doc)
  await fs.writeFile(docPath(doc.id), JSON.stringify(doc, null, 2), 'utf-8')
  return doc
}

export async function createWorkflow(partial) {
  const now = new Date().toISOString()
  const doc = {
    id: partial.id || newWorkflowId(partial.name),
    name: partial.name || 'Untitled workflow',
    description: partial.description || '',
    version: 1,
    status: 'draft',
    createdBy: partial.createdBy || 'portal',
    createdAt: now,
    updatedAt: now,
    prompt: partial.prompt || '',
    target: partial.target || {},
    inputs: partial.inputs || [],
    nodes: partial.nodes || [],
    edges: partial.edges || [],
    generation: partial.generation || null,
    // Working-copy + version model.
    publishedVersion: null, // which version is currently deployed (null = never)
    publishedSnapshot: null, // frozen graph that is running
    deployment: partial.deployment || null,
    history: [{ version: 1, updatedAt: now, message: 'Created' }],
  }
  await writeDoc(doc)
  await writeSnapshot(doc)
  return doc
}

export async function updateWorkflow(id, patch, expectedEtag) {
  const existing = await getWorkflow(id)
  if (expectedEtag && existing.etag && expectedEtag !== existing.etag) {
    throw new StoreError('conflict', 'The workflow was modified by someone else. Reload and retry.')
  }
  const message = patch.__message || 'Edited'
  const clean = { ...patch }
  delete clean.__message
  const version = existing.version + 1
  const now = new Date().toISOString()
  const merged = {
    ...existing,
    ...clean,
    id: existing.id, // never reassign identity
    createdAt: existing.createdAt,
    createdBy: existing.createdBy,
    version,
    updatedAt: now,
  }
  // A normal edit never changes what's published; derive the lifecycle status.
  if (clean.status === undefined) merged.status = deriveStatus(merged)
  merged.history = appendHistory(existing.history, { version, updatedAt: now, message })
  await writeDoc(merged)
  await writeSnapshot(merged)
  return merged
}

/**
 * Promote the current working copy to "running": bump the version, freeze the
 * graph as `publishedSnapshot`, record the deployment, and mark it published.
 */
export async function publishWorkflow(id, deployment, expectedEtag) {
  const existing = await getWorkflow(id)
  if (expectedEtag && existing.etag && expectedEtag !== existing.etag) {
    throw new StoreError('conflict', 'The workflow was modified by someone else. Reload and retry.')
  }
  const version = existing.version + 1
  const now = new Date().toISOString()
  const merged = {
    ...existing,
    id: existing.id,
    createdAt: existing.createdAt,
    createdBy: existing.createdBy,
    version,
    updatedAt: now,
    status: 'published',
    publishedVersion: version,
    publishedSnapshot: {
      nodes: existing.nodes,
      edges: existing.edges,
      inputs: existing.inputs,
      target: existing.target,
    },
    deployment,
    history: appendHistory(existing.history, { version, updatedAt: now, message: 'Deployed' }),
  }
  await writeDoc(merged)
  await writeSnapshot(merged)
  return merged
}

/** List the version history (metadata) for a workflow, newest first. */
export async function listVersions(id) {
  const doc = await getWorkflow(id)
  return [...(doc.history ?? [])].sort((a, b) => b.version - a.version)
}

/** Read a full version snapshot for restore/diff. */
export async function getVersion(id, version) {
  try {
    const raw = await fs.readFile(versionPath(id, Number(version)), 'utf-8')
    return JSON.parse(raw)
  } catch {
    throw new StoreError('not_found', `Version ${version} of workflow '${id}' not found.`)
  }
}

/** Restore a previous version's graph into the working copy as a new version. */
export async function restoreVersion(id, version, expectedEtag) {
  const snap = await getVersion(id, version)
  return updateWorkflow(
    id,
    {
      name: snap.name,
      description: snap.description,
      prompt: snap.prompt,
      target: snap.target,
      inputs: snap.inputs,
      nodes: snap.nodes,
      edges: snap.edges,
      generation: snap.generation ?? null,
      __message: `Restored v${version}`,
    },
    expectedEtag,
  )
}

export async function deleteWorkflow(id) {
  await ensureDir()
  try {
    await fs.unlink(docPath(id))
  } catch {
    throw new StoreError('not_found', `Workflow '${id}' not found.`)
  }
  // Best-effort cleanup of version snapshots.
  await fs.rm(path.join(DATA_DIR, safeId(id)), { recursive: true, force: true }).catch(() => {})
}

/** Seed the store with starter workflows on first run so the gallery isn't empty. */
export async function seedIfEmpty(seedDocs) {
  await ensureDir()
  const files = (await fs.readdir(DATA_DIR)).filter((f) => f.endsWith('.json'))
  if (files.length > 0) return
  for (const seed of seedDocs) {
    const published = seed.status === 'published'
    const doc = {
      ...seed,
      publishedVersion: published ? seed.version : null,
      publishedSnapshot: published
        ? { nodes: seed.nodes, edges: seed.edges, inputs: seed.inputs, target: seed.target }
        : null,
      history: [{ version: seed.version, updatedAt: seed.updatedAt, message: 'Seeded' }],
    }
    doc.etag = etagFor(doc)
    await writeDoc(doc)
    await writeSnapshot(doc)
  }
}
