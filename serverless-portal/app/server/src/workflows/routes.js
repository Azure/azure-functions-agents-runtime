// Workflow Composer REST API.
//
// Mounted at /api by index.js. These routes are backed by the portal-owned
// store (workflows/store.js) and do NOT require the user's ARM token — the
// portal owns the workflow documents and performs deploys on the customer's
// behalf. Keeping them token-free lets the composer work in local dev with no
// Azure sign-in.

import express from 'express'

import * as store from './store.js'
import { seedWorkflows } from './seeds.js'
import { generateWorkflow } from './generator.js'
import { generateComponentCode } from './codegen.js'
import { compileWorkflow } from './compiler.js'
import { deployWorkflow } from './deployer.js'
import { simulateRun } from './runner.js'
import { getModelProvider } from '../llm/provider.js'
import { listSkills, loadSkill } from '../skills/index.js'

const wrap = (fn) => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next)

function mapStoreError(err, res, next) {
  if (err instanceof store.StoreError) {
    return res.status(err.code === 'not_found' ? 404 : 409).json({ detail: err.message })
  }
  return next(err)
}

export function createWorkflowsRouter() {
  const router = express.Router()

  // Seed starter workflows once on first use.
  let seeded = false
  router.use(
    wrap(async (_req, _res, next) => {
      if (!seeded) {
        await store.seedIfEmpty(seedWorkflows)
        seeded = true
      }
      next()
    }),
  )

  // ---- Composer meta (model + skills + component catalog) ----

  // Which model is active — surfaced so users can confirm/choose. Note the model
  // is separate from the skills below.
  router.get(
    '/composer/model',
    wrap(async (_req, res) => {
      res.json(getModelProvider().describe())
    }),
  )

  // Skills available to the composer (prompt + knowledge bundles, model-independent).
  router.get(
    '/composer/skills',
    wrap(async (_req, res) => {
      res.json(await listSkills())
    }),
  )

  // The component catalog the planner (and the palette / editors) draw from.
  router.get(
    '/composer/catalog',
    wrap(async (_req, res) => {
      const skill = await loadSkill('composer-plan')
      res.json(skill.components ?? {})
    }),
  )

  // Stage 2: generate the runtime code for a single component (tool body /
  // agent instructions) from the workflow intent. Stateless — works on unsaved
  // draft nodes; the client merges the result into the editor and saves later.
  router.post(
    '/composer/generate-code',
    wrap(async (req, res) => {
      const node = req.body?.node
      const workflow = req.body?.workflow || {}
      if (!node || !node.type) return res.status(400).json({ detail: 'A node is required.' })
      try {
        const result = await generateComponentCode({ node, workflow })
        res.json(result)
      } catch (err) {
        const status = err.code === 'bad_codegen' ? 422 : err.code === 'model_error' ? 502 : 400
        res.status(status).json({ detail: err.message })
      }
    }),
  )

  // ---- Workflow CRUD ----

  router.get(
    '/workflows',
    wrap(async (_req, res) => {
      res.json(await store.listWorkflows())
    }),
  )

  router.post(
    '/workflows',
    wrap(async (req, res) => {
      const doc = await store.createWorkflow(req.body || {})
      res.status(201).json(doc)
    }),
  )

  // Generate a draft from plain English, then persist it as a new workflow.
  router.post(
    '/workflows/generate',
    wrap(async (req, res) => {
      const prompt = String(req.body?.prompt || '').trim()
      if (!prompt) return res.status(400).json({ detail: 'A description (prompt) is required.' })
      let draft
      try {
        draft = await generateWorkflow({ prompt, name: req.body?.name, target: req.body?.target })
      } catch (err) {
        const status = err.code === 'bad_plan' ? 422 : err.code === 'model_error' ? 502 : 500
        return res.status(status).json({ detail: err.message })
      }
      const doc = await store.createWorkflow(draft)
      res.status(201).json(doc)
    }),
  )

  // Re-generate the graph for an existing workflow from a new prompt (keeps id).
  router.post(
    '/workflows/:id/regenerate',
    wrap(async (req, res, next) => {
      try {
        const existing = await store.getWorkflow(req.params.id)
        const prompt = String(req.body?.prompt ?? existing.prompt ?? '').trim()
        if (!prompt) return res.status(400).json({ detail: 'A description (prompt) is required.' })
        const draft = await generateWorkflow({ prompt, name: existing.name, target: existing.target })
        const doc = await store.updateWorkflow(req.params.id, {
          prompt,
          nodes: draft.nodes,
          edges: draft.edges,
          inputs: draft.inputs,
          generation: draft.generation,
          status: 'draft',
        })
        res.json(doc)
      } catch (err) {
        if (err.code === 'bad_plan') return res.status(422).json({ detail: err.message })
        if (err.code === 'model_error') return res.status(502).json({ detail: err.message })
        mapStoreError(err, res, next)
      }
    }),
  )

  router.get(
    '/workflows/:id',
    wrap(async (req, res, next) => {
      try {
        res.json(await store.getWorkflow(req.params.id))
      } catch (err) {
        mapStoreError(err, res, next)
      }
    }),
  )

  // Full-document save (the builder patches locally, then PUTs). Only editable
  // fields are applied; the backend owns version/status/publish bookkeeping.
  // Concurrency: If-Match (or body.etag) must match the current ETag.
  router.put(
    '/workflows/:id',
    wrap(async (req, res, next) => {
      try {
        const body = req.body || {}
        const editable = {
          name: body.name,
          description: body.description,
          prompt: body.prompt,
          target: body.target,
          inputs: body.inputs,
          nodes: body.nodes,
          edges: body.edges,
          generation: body.generation,
        }
        for (const k of Object.keys(editable)) if (editable[k] === undefined) delete editable[k]
        if (body.message) editable.__message = body.message
        const doc = await store.updateWorkflow(req.params.id, editable, req.get('if-match') || body.etag)
        res.json(doc)
      } catch (err) {
        mapStoreError(err, res, next)
      }
    }),
  )

  router.delete(
    '/workflows/:id',
    wrap(async (req, res, next) => {
      try {
        await store.deleteWorkflow(req.params.id)
        res.status(204).end()
      } catch (err) {
        mapStoreError(err, res, next)
      }
    }),
  )

  // ---- Compile / deploy / run ----

  // Preview the runtime project files the graph compiles to.
  router.get(
    '/workflows/:id/compile',
    wrap(async (req, res, next) => {
      try {
        const doc = await store.getWorkflow(req.params.id)
        res.json({ files: compileWorkflow(doc) })
      } catch (err) {
        mapStoreError(err, res, next)
      }
    }),
  )

  // Deploy on the customer's behalf (simulated) and promote the working copy to
  // published (freezes publishedSnapshot + records the deployment).
  router.post(
    '/workflows/:id/deploy',
    wrap(async (req, res, next) => {
      try {
        const doc = await store.getWorkflow(req.params.id)
        const deployment = deployWorkflow(doc)
        const updated = await store.publishWorkflow(
          req.params.id,
          deployment,
          req.get('if-match') || req.body?.etag,
        )
        res.json(updated)
      } catch (err) {
        mapStoreError(err, res, next)
      }
    }),
  )

  // ---- Version history + restore (working-copy model) ----

  router.get(
    '/workflows/:id/versions',
    wrap(async (req, res, next) => {
      try {
        res.json(await store.listVersions(req.params.id))
      } catch (err) {
        mapStoreError(err, res, next)
      }
    }),
  )

  router.get(
    '/workflows/:id/versions/:version',
    wrap(async (req, res, next) => {
      try {
        res.json(await store.getVersion(req.params.id, req.params.version))
      } catch (err) {
        mapStoreError(err, res, next)
      }
    }),
  )

  router.post(
    '/workflows/:id/restore/:version',
    wrap(async (req, res, next) => {
      try {
        const doc = await store.restoreVersion(
          req.params.id,
          Number(req.params.version),
          req.get('if-match') || req.body?.etag,
        )
        res.json(doc)
      } catch (err) {
        mapStoreError(err, res, next)
      }
    }),
  )

  // Simulate a run for the run surface.
  router.post(
    '/workflows/:id/run',
    wrap(async (req, res, next) => {
      try {
        const doc = await store.getWorkflow(req.params.id)
        res.json(simulateRun(doc, req.body?.inputs || {}))
      } catch (err) {
        mapStoreError(err, res, next)
      }
    }),
  )

  return router
}
