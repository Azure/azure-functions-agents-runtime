// Shared helpers for the Serverless Agent Portal UI.

const api = {
  async list() { return req('GET', '/api/agents'); },
  async get(name) { return req('GET', `/api/agents/${encodeURIComponent(name)}`); },
  async create(payload) { return req('POST', '/api/agents', payload); },
  async update(name, content) { return req('PUT', `/api/agents/${encodeURIComponent(name)}`, { content }); },
  async health() { return req('GET', '/api/health'); },
};

async function req(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const detail = (data && data.detail) ? data.detail : `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return data;
}

function toast(message, kind = 'ok') {
  let host = document.getElementById('toast');
  if (!host) { host = document.createElement('div'); host.id = 'toast'; document.body.appendChild(host); }
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => el.remove(), 3800);
}

function qs(name) {
  return new URLSearchParams(location.search).get(name);
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// Show which storage backend is active in the header env badge.
async function showBackend() {
  const badge = document.getElementById('env-badge');
  if (!badge) return;
  try {
    const h = await api.health();
    badge.textContent = `${h.project}/${h.environment} · ${h.storage}`;
  } catch {
    badge.textContent = 'storage: unreachable';
  }
}
