import '@testing-library/jest-dom/vitest'

// jsdom implements neither of these. The annotation layout uses ResizeObserver
// to re-measure and rAF to coalesce; in tests the synchronous first pass in
// useLayoutEffect is what we assert against, so no-op stubs are sufficient.
if (!('ResizeObserver' in globalThis)) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}

// jsdom has no layout engine, so scrollIntoView is missing on elements.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {}
}
