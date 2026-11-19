import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { StatusPill } from './StatusPill';

const now = Date.parse('2026-11-02T09:00:00.000Z');

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

test('displays the supplied value, explicit source and ISO observation time', () => {
  render(
    <StatusPill value="已连接" source="本地心跳（模拟）" observedAt={now} now={now} tone="info" />,
  );
  expect(screen.getByText('已连接')).toBeVisible();
  expect(screen.getByText('来源：本地心跳（模拟）')).toBeVisible();
  expect(screen.getByText('2026-11-02T09:00:00.000Z')).toHaveAttribute(
    'datetime',
    '2026-11-02T09:00:00.000Z',
  );
  expect(screen.queryByText('可能过期')).not.toBeInTheDocument();
  expect(screen.queryByText(/通过|运行中/)).not.toBeInTheDocument();
});

test.each([null, NaN, Infinity, 9e15])(
  'invalid or missing observation %s is unknown, never a fabricated time',
  (observedAt) => {
    const { container } = render(
      <StatusPill value="未知" source="未知来源" observedAt={observedAt} now={now} />,
    );
    expect(screen.getByText('时间未知')).toBeVisible();
    expect(container.querySelector('time')).toBeNull();
    expect(screen.queryByText('可能过期')).not.toBeInTheDocument();
  },
);

test('expired observations keep the original value and time with an additional text warning', () => {
  render(<StatusPill value="已连接" source="最后一次心跳" observedAt={now - 30_001} now={now} />);
  expect(screen.getByText('已连接')).toBeVisible();
  expect(screen.getByText('可能过期')).toBeVisible();
  expect(screen.getByText('2026-11-02T08:59:29.999Z')).toBeVisible();
});

test('live freshness advances without incoming events and the timer is cleaned on unmount', () => {
  vi.useFakeTimers();
  vi.setSystemTime(now);
  const { unmount } = render(
    <StatusPill value="已连接" source="心跳" observedAt={now} staleAfterMs={2000} />,
  );
  act(() => vi.advanceTimersByTime(2000));
  expect(screen.queryByText('可能过期')).not.toBeInTheDocument();
  act(() => vi.advanceTimersByTime(1000));
  expect(screen.getByText('可能过期')).toBeVisible();
  unmount();
  expect(vi.getTimerCount()).toBe(0);
});

test('a new observation clears stale state and a subsequent missing time stays unknown', () => {
  const { rerender } = render(
    <StatusPill value="已连接" source="心跳" observedAt={now - 60_000} now={now} />,
  );
  expect(screen.getByText('可能过期')).toBeVisible();
  rerender(<StatusPill value="已连接" source="心跳" observedAt={now} now={now} />);
  expect(screen.queryByText('可能过期')).not.toBeInTheDocument();
  rerender(<StatusPill value="离线" source="连接观察器" observedAt={null} now={now} />);
  expect(screen.getByText('时间未知')).toBeVisible();
});

test('future timestamps are identified as clock anomalies rather than fresh evidence', () => {
  render(<StatusPill value="未知" source="模拟时钟" observedAt={now + 10_000} now={now} />);
  expect(screen.getByText('时间异常（晚于本机时钟）')).toBeVisible();
});

test('empty labels and sources have honest fallbacks', () => {
  render(<StatusPill value=" " source=" " observedAt={null} now={now} />);
  expect(screen.getByText('未知状态')).toBeVisible();
  expect(screen.getByText('来源：未知来源')).toBeVisible();
});
