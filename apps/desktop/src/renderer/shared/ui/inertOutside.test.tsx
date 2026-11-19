import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';
import { inertOutside } from './inertOutside';

afterEach(cleanup);

test('overlapping focus-isolation leases restore the original background only after the last closes', () => {
  render(
    <>
      <div data-testid="outside" />
      <div data-testid="popup" />
    </>,
  );
  const outside = screen.getByTestId('outside');
  const popup = screen.getByTestId('popup');
  const closeFirst = inertOutside(popup);
  const closeSecond = inertOutside(popup);
  expect(outside).toHaveAttribute('inert');
  expect(popup).not.toHaveAttribute('inert');
  closeFirst?.();
  expect(outside).toHaveAttribute('inert');
  closeSecond?.();
  expect(outside).not.toHaveAttribute('inert');
});

test('existing inert state is preserved and a detached/null ref is harmless', () => {
  render(
    <>
      <div data-testid="outside" inert />
      <div data-testid="popup" />
    </>,
  );
  const close = inertOutside(screen.getByTestId('popup'));
  close?.();
  expect(screen.getByTestId('outside')).toHaveAttribute('inert');
  expect(inertOutside(null)).toBeUndefined();
  inertOutside(document.createElement('div'))?.();
});
