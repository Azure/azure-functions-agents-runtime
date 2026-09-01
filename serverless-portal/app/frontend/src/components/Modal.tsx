// Modal dialog built on the CoreAI/Fluent Dialog: focus-trapped, escape- and
// backdrop-dismissable, and portalled. Keeps the lightweight
// { title, onClose, children, width } API used across the portal so call sites
// don't change.
import { type ReactNode } from 'react'
import { Dialog, DialogSurface, DialogBody, DialogTitle, DialogContent, Button } from '@coreai/fluentui-react'
import { DismissRegular } from '@fluentui/react-icons'

export function Modal({
  title,
  onClose,
  children,
  width,
  closeDisabled = false,
}: {
  title: ReactNode
  onClose: () => void
  children: ReactNode
  width?: number
  closeDisabled?: boolean
}) {
  return (
    <Dialog
      open
      onOpenChange={(_, data) => {
        if (!data.open && !closeDisabled) onClose()
      }}
    >
      <DialogSurface style={width ? { maxWidth: `${width}px` } : undefined}>
        <DialogBody>
          <DialogTitle
            action={
              <Button
                appearance="subtle"
                size="small"
                icon={<DismissRegular />}
                onClick={onClose}
                disabled={closeDisabled}
                title="Close"
                aria-label="Close"
              />
            }
          >
            {title}
          </DialogTitle>
          <DialogContent>{children}</DialogContent>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  )
}
