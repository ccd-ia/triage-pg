/*
 * ErrorBoundary — a render-error firewall around the routed views.
 *
 * Without this, an uncaught render error (e.g. a backend payload shaped
 * differently than the SPA expects — the /status `runs` regression that rendered
 * an object as a React child) unmounts the WHOLE app and leaves a blank page.
 * This catches it and shows a recoverable banner with the error message instead,
 * scoped to the routed content so the top bar + nav stay usable.
 */
import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surface to the console for debugging; the banner shows the message.
    console.error('[dashboard] render error', error, info.componentStack)
  }

  reset = (): void => this.setState({ error: null })

  render(): ReactNode {
    const { error } = this.state
    if (error) {
      return (
        <main className="page">
          <div className="banner err">
            <b>Something went wrong rendering this view.</b>
            <div style={{ marginTop: 6, fontFamily: 'ui-monospace, Menlo, monospace' }}>
              {error.message}
            </div>
            <button
              type="button"
              className="seg"
              style={{ marginTop: 10 }}
              onClick={this.reset}
            >
              Try again
            </button>
          </div>
        </main>
      )
    }
    return this.props.children
  }
}
