import { createHash, randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

const locks = new Map()

function sha256(content) {
  return createHash('sha256').update(content).digest('hex')
}

async function exists(filePath) {
  try {
    await fs.promises.access(filePath)
    return true
  } catch {
    return false
  }
}

async function readBytes(filePath) {
  try {
    return await fs.promises.readFile(filePath)
  } catch (error) {
    if (error?.code === 'ENOENT') return null
    throw error
  }
}

async function writeFlushed(filePath, content) {
  await fs.promises.mkdir(path.dirname(filePath), { recursive: true })
  const handle = await fs.promises.open(filePath, 'w')
  try {
    await handle.writeFile(content)
    await handle.sync()
  } finally {
    await handle.close()
  }
}

function targetPath(appDir, relPath) {
  const normalized = String(relPath ?? '').replace(/\\/g, '/')
  const segments = normalized.split('/').filter(Boolean)
  if (!segments.length || normalized.startsWith('/') || segments.includes('..') || segments[0] === '.transactions') {
    throw new Error(`Invalid source draft path: ${relPath}`)
  }
  const target = path.resolve(appDir, ...segments)
  const root = `${path.resolve(appDir)}${path.sep}`
  if (!target.startsWith(root)) throw new Error(`Invalid source draft path: ${relPath}`)
  return target
}

async function replaceFile(tempPath, destination) {
  await fs.promises.mkdir(path.dirname(destination), { recursive: true })
  await fs.promises.unlink(destination).catch((error) => {
    if (error?.code !== 'ENOENT') throw error
  })
  await fs.promises.rename(tempPath, destination)
}

async function writeJournal(transactionDir, journal) {
  const temp = path.join(transactionDir, 'journal.tmp')
  await writeFlushed(temp, JSON.stringify(journal, null, 2))
  await replaceFile(temp, path.join(transactionDir, 'journal.json'))
}

async function restoreOld(entry) {
  if (!entry.oldExists) {
    await fs.promises.unlink(entry.target).catch((error) => {
      if (error?.code !== 'ENOENT') throw error
    })
    return
  }
  const oldBytes = await fs.promises.readFile(entry.backup)
  const temp = `${entry.target}.${randomUUID()}.restore`
  await writeFlushed(temp, oldBytes)
  await replaceFile(temp, entry.target)
}

async function recoverTransaction(transactionDir) {
  let journal
  try {
    journal = JSON.parse(await fs.promises.readFile(path.join(transactionDir, 'journal.json'), 'utf-8'))
  } catch {
    await fs.promises.rm(transactionDir, { recursive: true, force: true })
    return
  }

  const canRollForward = await Promise.all(
    journal.entries.map(async (entry) => {
      const current = await readBytes(entry.target)
      if (current && sha256(current) === entry.newHash) return true
      const staged = await readBytes(entry.staged)
      return Boolean(staged && sha256(staged) === entry.newHash)
    }),
  ).then((values) => values.every(Boolean))

  if (canRollForward) {
    for (const entry of journal.entries) {
      const current = await readBytes(entry.target)
      if (current && sha256(current) === entry.newHash) continue
      await replaceFile(entry.staged, entry.target)
    }
  } else {
    for (const entry of journal.entries) await restoreOld(entry)
  }
  await fs.promises.rm(transactionDir, { recursive: true, force: true })
}

async function recoverUnlocked(appDir) {
  const transactionsDir = path.join(appDir, '.transactions')
  let entries
  try {
    entries = await fs.promises.readdir(transactionsDir, { withFileTypes: true })
  } catch {
    return
  }
  for (const entry of entries) {
    if (entry.isDirectory()) await recoverTransaction(path.join(transactionsDir, entry.name))
  }
}

async function withLock(appDir, operation) {
  const key = path.resolve(appDir).toLowerCase()
  const previous = locks.get(key) ?? Promise.resolve()
  const current = previous.catch(() => undefined).then(operation)
  locks.set(key, current)
  try {
    return await current
  } finally {
    if (locks.get(key) === current) locks.delete(key)
  }
}

export async function recoverSourceDrafts(appDir) {
  return withLock(appDir, () => recoverUnlocked(appDir))
}

export async function writeSourceDrafts(appDir, files, options = {}) {
  return withLock(appDir, async () => {
    await recoverUnlocked(appDir)
    const transactionDir = path.join(appDir, '.transactions', randomUUID())
    await fs.promises.mkdir(transactionDir, { recursive: true })
    const entries = []

    for (const [index, file] of files.entries()) {
      const target = targetPath(appDir, file.path)
      const oldBytes = await readBytes(target)
      const newBytes = Buffer.from(String(file.content), 'utf-8')
      const backup = path.join(transactionDir, `${index}.old`)
      const staged = `${target}.${randomUUID()}.draft`
      if (oldBytes) await writeFlushed(backup, oldBytes)
      await writeFlushed(staged, newBytes)
      entries.push({
        path: file.path,
        target,
        backup,
        staged,
        oldExists: oldBytes !== null,
        oldHash: oldBytes ? sha256(oldBytes) : null,
        newHash: sha256(newBytes),
      })
    }

    const journal = { version: 1, phase: 'prepared', replaced: 0, entries }
    await writeJournal(transactionDir, journal)
    try {
      for (const [index, entry] of entries.entries()) {
        await replaceFile(entry.staged, entry.target)
        journal.replaced = index + 1
        journal.phase = index === 0 ? 'tool_replaced' : 'requirements_replaced'
        await writeJournal(transactionDir, journal)
        await options.afterReplace?.(index, journal)
      }
      await fs.promises.rm(transactionDir, { recursive: true, force: true })
    } catch (error) {
      if (error?.code === 'SIMULATED_CRASH') throw error
      for (const entry of entries) await restoreOld(entry)
      await fs.promises.rm(transactionDir, { recursive: true, force: true })
      throw error
    }
  })
}