import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { DevicePanel } from './DevicePanel';
import { DEVICE_PREVIEW_TIME, simulatedDevice, simulatedTargetApp } from './deviceFixtures';
import { createDeviceStore } from './deviceStore';

afterEach(cleanup);

function renderPanel(devices = [simulatedDevice()], now = DEVICE_PREVIEW_TIME + 1000) {
  const api = {
    listDevices: vi.fn(async (): Promise<unknown> => ({ devices })),
    getDevice: vi.fn(async (serial: string): Promise<unknown> => simulatedDevice(serial)),
    getAgentHealth: vi.fn(async (serial: string): Promise<unknown> => simulatedDevice(serial)),
    connectAgent: vi.fn(async (serial: string): Promise<unknown> => simulatedDevice(serial)),
    getTargetApp: vi.fn(async (serial: string): Promise<unknown> => simulatedTargetApp(serial)),
  };
  const store = createDeviceStore({
    getConnection: async () => ({ port: 4311, token: 'simulated-token', pid: 9, version: '1.0.0' }),
    clientFor: () => api,
    setTimer: () => null,
    clearTimer: () => undefined,
  });
  const view = render(<DevicePanel store={store} source="模拟 Runtime 设备快照" now={now} />);
  return { api, store, ...view };
}

async function select(serial = 'SIM-ANDROID-A') {
  await userEvent.click(
    await screen.findByRole('button', { name: `查看设备 模拟 Android ${serial}` }),
  );
}

test('renders Runtime facts with source/time and leaves the device unselected until an explicit action', async () => {
  const { api } = renderPanel();
  await screen.findByRole('button', { name: '查看设备 模拟 Android SIM-ANDROID-A' });
  expect(screen.getByRole('heading', { name: '选择要查看的设备' })).toBeVisible();
  expect(screen.getByText('Agent 状态 ready · ready=true')).toBeVisible();
  expect(screen.getByText('来源：模拟 Runtime 设备快照')).toBeVisible();
  expect(screen.getByText(new Date(DEVICE_PREVIEW_TIME).toISOString())).toBeVisible();
  expect(api.connectAgent).not.toHaveBeenCalled();
  await select();
  const health = screen.getByRole('region', { name: '设备与 Agent 健康' });
  expect(within(health).getByText('Health 已鉴权').nextElementSibling).toHaveTextContent('是');
  expect(within(health).getByText('Runtime 会话').nextElementSibling).toHaveTextContent(
    '00000000-0000-4000-8000-000000000001',
  );
  expect(screen.getByRole('button', { name: '接入既有 Agent' })).toBeDisabled();
  const capabilities = screen.getByRole('region', { name: 'Agent 能力声明' });
  expect(within(capabilities).getByText('不可用')).toBeVisible();
  expect(within(capabilities).getByText('tap、swipe、set_text、node_click')).toBeVisible();
});

test('no snapshot timestamp is replaced with a render timestamp and unknown capabilities stay unknown', async () => {
  const view = renderPanel([
    simulatedDevice('SIM', {
      observed_at_ms: null,
      ready: false,
      status: 'agent_available',
      connection: {},
      capabilities: null,
    }),
  ]);
  await select('SIM');
  expect(view.container.querySelector('time')).toBeNull();
  expect(screen.getAllByText('时间未知').length).toBeGreaterThan(0);
  const capabilities = screen.getByRole('region', { name: 'Agent 能力声明' });
  expect(within(capabilities).getAllByText('未知').length).toBeGreaterThanOrEqual(3);
  expect(within(capabilities).queryByText('不可用')).toBeNull();
  expect(screen.getByText('Health 已鉴权').nextElementSibling).toHaveTextContent('未知');
});

test.each([
  [DEVICE_PREVIEW_TIME + 30_001, '可能过期'],
  [DEVICE_PREVIEW_TIME - 1000, '时间异常（晚于本机时钟）'],
])('status freshness is visibly honest at clock %s', async (now, expected) => {
  renderPanel(undefined, now);
  await screen.findByRole('button', { name: '查看设备 模拟 Android SIM-ANDROID-A' });
  expect(screen.getByText(expected)).toBeVisible();
});

test('successful empty discovery is not confused with HTTP-200 discovery failure', async () => {
  const { api, store } = renderPanel([]);
  expect(await screen.findByRole('heading', { name: '未发现本机设备' })).toBeVisible();
  api.listDevices.mockResolvedValue({ devices: [], error: 'adb_unavailable' });
  await userEvent.click(screen.getByRole('button', { name: '刷新设备列表' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('adb_unavailable');
  expect(screen.queryByRole('heading', { name: '未发现本机设备' })).toBeNull();
  expect(store.getSnapshot().status).toBe('error');
});

test('no preload/Runtime is unavailable, not an empty success, and requires no team service', async () => {
  const store = createDeviceStore({
    getConnection: async () => null,
    clientFor: () => {
      throw new Error('must not call');
    },
    setTimer: () => null,
    clearTimer: () => undefined,
  });
  render(<DevicePanel store={store} now={DEVICE_PREVIEW_TIME} />);
  expect(await screen.findByRole('heading', { name: '设备状态不可确认' })).toBeVisible();
  expect(screen.getByRole('alert')).toHaveTextContent('Runtime 未运行或未连接');
  expect(screen.queryByRole('heading', { name: '未发现本机设备' })).toBeNull();
});

test('offline installed=false is not shown as proof that the Agent was uninstalled', async () => {
  const raw = simulatedDevice('SIM');
  renderPanel([
    simulatedDevice('SIM', {
      state: 'absent',
      status: 'disconnected',
      ready: false,
      agent: { ...raw.agent, installed: false },
      connection: {},
      capabilities: null,
    }),
  ]);
  await select('SIM');
  expect(screen.getByText(/设备不可检查，不代表已卸载/)).toBeVisible();
  expect(screen.getByRole('button', { name: '接入既有 Agent' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '检查靶场安装' })).toBeDisabled();
  expect(screen.getAllByText('设备已断开 · ready=false')).toHaveLength(2);
});

test('ready=false and control recovery remain visible even when Agent status is ready', async () => {
  renderPanel([simulatedDevice('SIM', { ready: false, control: { recovery_required: true } })]);
  await select('SIM');
  expect(screen.getAllByText('Agent 状态 ready · ready=false')).toHaveLength(2);
  expect(screen.getByText('控制门要求恢复').nextElementSibling).toHaveTextContent('是');
});

test('manual authorization and Runtime guidance are displayed without permission or preparation controls', async () => {
  renderPanel([
    simulatedDevice('SIM', {
      status: 'manual_authorization_required',
      ready: false,
      connection: {},
      diagnostic: {
        code: 'accessibility_policy_blocked',
        manual_required: true,
        guidance: 'Inspect device settings manually.',
        awaiting_authorization: true,
        authorization_reason: 'accessibility_policy_blocked',
        operation_id: '00000000-0000-4000-8000-000000000004',
        cleanup_pending: false,
      },
    }),
  ]);
  await select('SIM');
  expect(screen.getAllByText('等待人工授权 · ready=false')).toHaveLength(2);
  expect(screen.getByText('Runtime 指引：Inspect device settings manually.')).toBeVisible();
  expect(
    screen.queryByRole('button', { name: /安装 Agent|开启无障碍|准备 Agent|重启 Agent/ }),
  ).toBeNull();
});

test('target installation checks are explicit and do not turn an unavailable Agent into ready', async () => {
  const { api } = renderPanel([
    simulatedDevice('SIM', {
      status: 'agent_missing',
      ready: false,
      connection: {},
      capabilities: null,
    }),
  ]);
  await select('SIM');
  expect(api.getTargetApp).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: '检查靶场安装' }));
  expect(await screen.findByText('靶场已安装')).toBeVisible();
  expect(api.getTargetApp).toHaveBeenCalledWith('SIM');
  expect(api.connectAgent).not.toHaveBeenCalled();
  expect(screen.getAllByText('Agent 未安装 · ready=false')).toHaveLength(2);
  expect(screen.getByText('来源：本机 Runtime 靶场安装检查')).toBeVisible();
});

test('health errors retain history with an explicit warning rather than current availability', async () => {
  const { api } = renderPanel();
  await select();
  api.getAgentHealth.mockRejectedValue(new Error('secret-transport-output'));
  await userEvent.click(screen.getByRole('button', { name: '复查健康' }));
  expect(await screen.findByText('以下为上次快照，当前健康未确认。')).toBeVisible();
  expect(screen.getByRole('button', { name: '复查健康' })).toBeDisabled();
  expect(screen.queryByText(/secret-transport-output/)).toBeNull();
});

test('device choice is keyboard-operable and exposes its selected state', async () => {
  renderPanel();
  const device = await screen.findByRole('button', { name: '查看设备 模拟 Android SIM-ANDROID-A' });
  expect(device).toHaveAccessibleDescription(/ADB.*ready=true.*模拟 Runtime.*2026-11-16T03:00:00/);
  await userEvent.tab();
  expect(screen.getByRole('button', { name: '刷新设备列表' })).toHaveFocus();
  await userEvent.tab();
  expect(device).toHaveFocus();
  await userEvent.keyboard('{Enter}');
  expect(device).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByRole('region', { name: '选中设备详情' })).toBeVisible();
});
