// Lightweight modal dialog rendered in a portal at the document body, so it
// overlays the whole page. Closes on backdrop click or Escape.
import { type ReactNode, useEffect } from 'react'
import { createPortal } from 'react-dom'

export function Modal({
  title,
  onClose,
  children,
  width,
}: {
  title: ReactNode
  onClose: () => void
  children: ReactNode
  width?: number
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = prev
    }
  }, [onClose])

  return createPortal(
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        style={width ? { maxWidth: width } : undefined}
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h3>{title}</h3>
          <button className="btn ghost sm" onClick={onClose} title="Close" aria-label="Close">
            ✕
          </button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>,
    document.body,
  )
}
