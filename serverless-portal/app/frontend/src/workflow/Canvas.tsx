// Interactive workflow canvas.
//
// Free-form, movable graph: nodes are absolutely positioned and draggable, edges
// are drawn as SVG curves between node anchors, and new relationships are created
// by dragging from a node's output handle onto another node. A floating action
// bar appears over the canvas whenever the graph has unsaved changes.
//
// Positions in `node.position` are pixel coordinates (the builder normalizes the
// generator's grid coordinates to pixels on load).

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { type Workflow, type WorkflowNode, NODE_META } from './types'

const NODE_W = 210
const DEFAULT_H = 96

type Size = { w: number; h: number }

function midToMid(
  sp: { x: number; y: number },
  ss: Size | undefined,
  tp: { x: number; y: number },
  ts: Size | undefined,
) {
  const sw = ss?.w ?? NODE_W
  const sh = ss?.h ?? DEFAULT_H
  const th = ts?.h ?? DEFAULT_H
  return { x1: sp.x + sw, y1: sp.y + sh / 2, x2: tp.x, y2: tp.y + th / 2 }
}

function curve(x1: number, y1: number, x2: number, y2: number) {
  const dx = Math.max(40, Math.abs(x2 - x1) / 2)
  return `M ${x1},${y1} C ${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`
}

export default function Canvas({
  workflow,
  selectedId,
  dirty,
  onSelect,
  onMoveNode,
  onConnect,
  onDeleteEdge,
  onRegenerate,
  onSave,
  regenerating,
}: {
  workflow: Workflow
  selectedId: string | null
  dirty: boolean
  onSelect: (id: string | null) => void
  onMoveNode: (id: string, x: number, y: number) => void
  onConnect: (from: string, to: string) => void
  onDeleteEdge: (id: string) => void
  onRegenerate: () => void
  onSave: () => void
  regenerating?: boolean
}) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const [sizes, setSizes] = useState<Record<string, Size>>({})
  const [drag, setDrag] = useState<{ id: string; dx: number; dy: number; moved: boolean } | null>(null)
  const [link, setLink] = useState<{ from: string; x: number; y: number } | null>(null)

  const posOf = useCallback((n: WorkflowNode) => ({ x: n.position?.x ?? 0, y: n.position?.y ?? 0 }), [])

  // Convert a client point into canvas-surface coordinates (accounting for scroll).
  const toCanvas = useCallback((clientX: number, clientY: number) => {
    const el = scrollRef.current
    if (!el) return { x: clientX, y: clientY }
    const r = el.getBoundingClientRect()
    return { x: clientX - r.left + el.scrollLeft, y: clientY - r.top + el.scrollTop }
  }, [])

  // Measure node sizes so edge anchors line up with the real card height.
  const measure = useCallback(
    (id: string) => (el: HTMLDivElement | null) => {
      if (!el) return
      const w = el.offsetWidth
      const h = el.offsetHeight
      setSizes((s) => (s[id]?.w === w && s[id]?.h === h ? s : { ...s, [id]: { w, h } }))
    },
    [],
  )

  // Global drag / linking handlers, active only while interacting.
  useEffect(() => {
    if (!drag && !link) return
    const move = (e: MouseEvent) => {
      const p = toCanvas(e.clientX, e.clientY)
      if (drag) {
        onMoveNode(drag.id, Math.max(0, p.x - drag.dx), Math.max(0, p.y - drag.dy))
        if (!drag.moved) setDrag((d) => (d ? { ...d, moved: true } : d))
      } else if (link) {
        setLink((l) => (l ? { ...l, x: p.x, y: p.y } : l))
      }
    }
    const up = (e: MouseEvent) => {
      if (drag) {
        if (!drag.moved) onSelect(drag.id) // a click, not a drag
        setDrag(null)
      } else if (link) {
        const target = (document.elementFromPoint(e.clientX, e.clientY) as HTMLElement | null)?.closest(
          '[data-node-id]',
        )
        const toId = target?.getAttribute('data-node-id')
        if (toId && toId !== link.from) onConnect(link.from, toId)
        setLink(null)
      }
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
    return () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
    }
  }, [drag, link, toCanvas, onMoveNode, onSelect, onConnect])

  const byId = useMemo(() => new Map(workflow.nodes.map((n) => [n.id, n])), [workflow.nodes])

  // Surface bounds so the scroll area encompasses every node.
  const bounds = useMemo(() => {
    let w = 900
    let h = 500
    for (const n of workflow.nodes) {
      const p = posOf(n)
      w = Math.max(w, p.x + NODE_W + 120)
      h = Math.max(h, p.y + (sizes[n.id]?.h ?? DEFAULT_H) + 120)
    }
    return { w, h }
  }, [workflow.nodes, sizes, posOf])

  const startDrag = (e: React.MouseEvent, node: WorkflowNode) => {
    if ((e.target as HTMLElement).dataset.handle) return // handle starts a link
    e.preventDefault()
    const p = toCanvas(e.clientX, e.clientY)
    const np = posOf(node)
    setDrag({ id: node.id, dx: p.x - np.x, dy: p.y - np.y, moved: false })
  }
  const startLink = (e: React.MouseEvent, node: WorkflowNode) => {
    e.preventDefault()
    e.stopPropagation()
    const p = toCanvas(e.clientX, e.clientY)
    setLink({ from: node.id, x: p.x, y: p.y })
  }

  return (
    <div className="canvas-viewport">
      <div className="canvas-scroll" ref={scrollRef}>
        <div
          className="canvas-surface"
          style={{ width: bounds.w, height: bounds.h, cursor: drag ? 'grabbing' : 'default' }}
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) onSelect(null) // click empty space clears selection
          }}
        >
          <svg className="edges" width={bounds.w} height={bounds.h}>
            <defs>
              <marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">
                <path d="M0,0 L8,3 L0,6 Z" fill="#9aa4b0" />
              </marker>
            </defs>
            {workflow.edges.map((edge) => {
              const s = byId.get(edge.from)
              const t = byId.get(edge.to)
              if (!s || !t) return null
              const { x1, y1, x2, y2 } = midToMid(posOf(s), sizes[s.id], posOf(t), sizes[t.id])
              return (
                <path
                  key={edge.id}
                  d={curve(x1, y1, x2, y2)}
                  fill="none"
                  stroke="#9aa4b0"
                  strokeWidth={2}
                  markerEnd="url(#arrow)"
                />
              )
            })}
            {link &&
              (() => {
                const s = byId.get(link.from)
                if (!s) return null
                const sp = posOf(s)
                const ss = sizes[s.id]
                return (
                  <path
                    d={curve(sp.x + (ss?.w ?? NODE_W), sp.y + (ss?.h ?? DEFAULT_H) / 2, link.x, link.y)}
                    fill="none"
                    stroke="var(--brand)"
                    strokeWidth={2}
                    strokeDasharray="5 4"
                  />
                )
              })()}
          </svg>

          {/* Edge labels + delete, as clickable overlays at each edge midpoint. */}
          {workflow.edges.map((edge) => {
            const s = byId.get(edge.from)
            const t = byId.get(edge.to)
            if (!s || !t) return null
            const { x1, y1, x2, y2 } = midToMid(posOf(s), sizes[s.id], posOf(t), sizes[t.id])
            const mx = (x1 + x2) / 2
            const my = (y1 + y2) / 2
            return (
              <div key={edge.id} className="edge-mid" style={{ left: mx, top: my }}>
                {edge.label && <span className="edge-mid-label">{edge.label}</span>}
                <button className="edge-mid-del" title="Delete connection" onClick={() => onDeleteEdge(edge.id)}>
                  ×
                </button>
              </div>
            )
          })}

          {/* Nodes */}
          {workflow.nodes.map((node) => {
            const meta = NODE_META[node.type]
            const p = posOf(node)
            return (
              <div
                key={node.id}
                ref={measure(node.id)}
                data-node-id={node.id}
                className={`node canvas-node ${meta.cls}${selectedId === node.id ? ' selected' : ''}`}
                style={{ left: p.x, top: p.y, cursor: drag?.id === node.id ? 'grabbing' : 'grab' }}
                onMouseDown={(e) => startDrag(e, node)}
              >
                <div className="node-head">
                  <span className={`tag ${meta.cls}`}>
                    {meta.icon} {node.type}
                  </span>
                  {node.source && (
                    <span className={`src ${node.source}`}>
                      {node.source === 'reused' ? 'reuse' : node.source === 'generated' ? 'gen' : 'edit'}
                    </span>
                  )}
                </div>
                <div className="node-title">{node.name}</div>
                <div className="node-meta">{node.kind}</div>
                {/* output handle → drag to another node to create a relationship */}
                <div
                  className="node-handle"
                  data-handle="1"
                  title="Drag to connect"
                  onMouseDown={(e) => startLink(e, node)}
                />
              </div>
            )
          })}
        </div>
      </div>

      {/* Floating action bar — appears when the workflow has changed. */}
      {dirty && (
        <div className="canvas-actions">
          <span className="canvas-changed">● Workflow changed</span>
          <button className="btn sm" onClick={onSave}>
            💾 Save
          </button>
          <button
            className="btn sm primary"
            disabled={regenerating}
            title="Re-plan the workflow from its description (replaces the current graph)"
            onClick={onRegenerate}
          >
            {regenerating ? '✨ Regenerating…' : '✨ Regenerate'}
          </button>
        </div>
      )}

      {workflow.nodes.length === 0 && (
        <div className="canvas-empty">No components yet. Describe your app or add one from the palette.</div>
      )}
    </div>
  )
}
