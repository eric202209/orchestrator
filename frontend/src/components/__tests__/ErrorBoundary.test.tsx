import { act } from 'react'
import type React from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { ErrorBoundary } from '../ErrorBoundary'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => { root.unmount() })
  container.remove()
})

describe('ErrorBoundary', () => {
  it('renders children normally when no error is thrown', () => {
    act(() => {
      root.render(
        <ErrorBoundary>
          <div data-testid="child">Hello World</div>
        </ErrorBoundary>,
      )
    })
    expect(container.querySelector('[data-testid="child"]')).toBeTruthy()
    expect(container.textContent).toContain('Hello World')
  })

  it('shows the fallback UI with the error message when a child throws', () => {
    function ThrowChild(): React.JSX.Element {
      throw new Error('something bad happened')
    }

    act(() => {
      root.render(
        <ErrorBoundary>
          <ThrowChild />
        </ErrorBoundary>,
      )
    })

    expect(container.textContent).toContain('Something went wrong')
    expect(container.textContent).toContain('something bad happened')
  })

  it('clicking "Try again" calls resetErrorBoundary and recovers the children', () => {
    let shouldThrow = false

    function ConditionalThrowChild(): React.JSX.Element {
      if (shouldThrow) {
        throw new Error('boom')
      }
      return <div data-testid="recovered">Recovered</div>
    }

    // Mount with the child in throwing mode
    shouldThrow = true
    act(() => {
      root.render(
        <ErrorBoundary>
          <ConditionalThrowChild />
        </ErrorBoundary>,
      )
    })
    expect(container.textContent).toContain('Something went wrong')

    // Switch the child so it no longer throws
    shouldThrow = false

    // Click the "Try again" button — this invokes resetErrorBoundary internally
    // which re-renders the children and picks up the non-throwing child
    const tryAgainBtn = Array.from(container.querySelectorAll('button')).find(
      (btn) => btn.textContent === 'Try again',
    )
    act(() => { tryAgainBtn?.click() })

    expect(container.textContent).toContain('Recovered')
    expect(container.querySelector('[data-testid="recovered"]')).toBeTruthy()
  })
})
