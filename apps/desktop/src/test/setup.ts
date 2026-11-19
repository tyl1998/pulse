import '@testing-library/jest-dom/vitest';

// jsdom has no layout, scrolling or pointer capture. Browser checks cover these for real.
if (typeof HTMLElement !== 'undefined') {
  for (const [name, value] of Object.entries({
    hasPointerCapture: (): boolean => false,
    setPointerCapture: (): void => undefined,
    releasePointerCapture: (): void => undefined,
    scrollIntoView: (): void => undefined,
  })) {
    if (!(name in HTMLElement.prototype)) {
      Object.defineProperty(HTMLElement.prototype, name, { configurable: true, value });
    }
  }
  if (typeof ResizeObserver === 'undefined') {
    Object.defineProperty(globalThis, 'ResizeObserver', {
      configurable: true,
      value: class {
        observe = (): void => undefined;
        unobserve = (): void => undefined;
        disconnect = (): void => undefined;
      },
    });
  }
}

// jsdom reports window.scrollTo as "not implemented"; router scroll restoration is disabled,
// but focus/link behavior may still call it.
if (typeof window !== 'undefined') {
  window.scrollTo = () => undefined;
}
