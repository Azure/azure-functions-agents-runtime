// Minimal .env loader (dependency-free, Node >=18). Loads KEY=VALUE lines from a
// `.env` file in the server root into process.env, without overriding variables
// already set in the real environment. Imported first by index.js so config
// (e.g. GITHUB_OAUTH_*) is present before any module reads process.env.

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const envPath = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '.env')

try {
  const text = fs.readFileSync(envPath, 'utf-8')
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (!line || line.startsWith('#')) continue
    const eq = line.indexOf('=')
    if (eq === -1) continue
    const key = line.slice(0, eq).trim()
    let val = line.slice(eq + 1).trim()
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1)
    }
    if (key && !(key in process.env)) process.env[key] = val
  }
} catch {
  // No .env file — rely on real environment variables.
}
