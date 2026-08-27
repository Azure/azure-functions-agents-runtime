import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import { recoverSourceDrafts, writeSourceDrafts } from '../src/source-draft-store.js'

async function tempDir(t) {
  const directory = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'source-drafts-'))
  t.after(() => fs.promises.rm(directory, { recursive: true, force: true }))
  return directory
}

test('writes both source drafts in one transaction', async (t) => {
  const directory = await tempDir(t)
  await writeSourceDrafts(directory, [
    { path: 'tools/azure_rest.py', content: 'tool' },
    { path: 'requirements.txt', content: 'deps' },
  ])

  assert.equal(await fs.promises.readFile(path.join(directory, 'tools', 'azure_rest.py'), 'utf-8'), 'tool')
  assert.equal(await fs.promises.readFile(path.join(directory, 'requirements.txt'), 'utf-8'), 'deps')
})

test('rolls forward an interrupted transaction when staged bytes are intact', async (t) => {
  const directory = await tempDir(t)
  await assert.rejects(
    writeSourceDrafts(
      directory,
      [
        { path: 'tools/azure_rest.py', content: 'new-tool' },
        { path: 'requirements.txt', content: 'new-deps' },
      ],
      { afterReplace: async (index) => index === 0 && Promise.reject(Object.assign(new Error('crash'), { code: 'SIMULATED_CRASH' })) },
    ),
  )

  await recoverSourceDrafts(directory)
  assert.equal(await fs.promises.readFile(path.join(directory, 'tools', 'azure_rest.py'), 'utf-8'), 'new-tool')
  assert.equal(await fs.promises.readFile(path.join(directory, 'requirements.txt'), 'utf-8'), 'new-deps')
})

test('rolls back an interrupted transaction when staged bytes are missing', async (t) => {
  const directory = await tempDir(t)
  await fs.promises.mkdir(path.join(directory, 'tools'), { recursive: true })
  await fs.promises.writeFile(path.join(directory, 'tools', 'azure_rest.py'), 'old-tool')
  await fs.promises.writeFile(path.join(directory, 'requirements.txt'), 'old-deps')
  await assert.rejects(
    writeSourceDrafts(
      directory,
      [
        { path: 'tools/azure_rest.py', content: 'new-tool' },
        { path: 'requirements.txt', content: 'new-deps' },
      ],
      {
        afterReplace: async (index, journal) => {
          if (index !== 0) return
          await fs.promises.unlink(journal.entries[1].staged)
          throw Object.assign(new Error('crash'), { code: 'SIMULATED_CRASH' })
        },
      },
    ),
  )

  await recoverSourceDrafts(directory)
  await recoverSourceDrafts(directory)
  assert.equal(await fs.promises.readFile(path.join(directory, 'tools', 'azure_rest.py'), 'utf-8'), 'old-tool')
  assert.equal(await fs.promises.readFile(path.join(directory, 'requirements.txt'), 'utf-8'), 'old-deps')
})