const labels = ['Model', 'Instructions', 'Deployment target', 'Review and deploy']

interface CreationStepsProps {
  current: number
  completed: boolean[]
  available: boolean[]
  onNavigate: (step: number) => void
  disabled?: boolean
}

export function CreationSteps({ current, completed, available, onNavigate, disabled = false }: CreationStepsProps) {
  return (
    <nav className="steps" aria-label="Skill creation progress">
      {labels.map((label, index) => {
        const step = index + 1
        const isComplete = completed[index]
        const canNavigate = available[index] || isComplete || step === current
        return (
          <span className="step-group" key={label}>
            <button
              type="button"
              className={'step' + (step === current ? ' active' : '') + (isComplete ? ' done' : '')}
              disabled={disabled || !canNavigate}
              onClick={() => canNavigate && onNavigate(step)}
              aria-current={step === current ? 'step' : undefined}
              aria-label={`Step ${step} of ${labels.length}: ${label}${isComplete ? ', completed' : ''}`}
            >
              <span className="step-mark" aria-hidden="true">{isComplete ? '✓' : step}</span>
              {label}
            </button>
            {step < labels.length && <span className="step-sep" aria-hidden="true">→</span>}
          </span>
        )
      })}
    </nav>
  )
}