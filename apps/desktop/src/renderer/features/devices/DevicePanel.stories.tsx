import { useEffect, useState } from 'react';
import type { Meta, StoryObj } from '@storybook/react-vite';
import { DevicePanel } from './DevicePanel';
import { DEVICE_PREVIEW_TIME, simulatedDevice, simulatedTargetApp } from './deviceFixtures';
import { createDeviceStore } from './deviceStore';

function DevicePreview({
  snapshot = simulatedDevice(),
  offline = false,
  now = DEVICE_PREVIEW_TIME + 1000,
}: {
  snapshot?: ReturnType<typeof simulatedDevice>;
  offline?: boolean;
  now?: number;
}) {
  const [store] = useState(() =>
    createDeviceStore({
      getConnection: async () =>
        offline ? null : { port: 1, pid: 1, token: 'synthetic-preview-only', version: '1.0.0' },
      clientFor: () => ({
        listDevices: async () => ({ devices: [snapshot] }),
        getDevice: async () => snapshot,
        getAgentHealth: async () => snapshot,
        connectAgent: async () => snapshot,
        getTargetApp: async (serial) => simulatedTargetApp(serial),
      }),
      setTimer: () => null,
      clearTimer: () => undefined,
    }),
  );
  useEffect(() => {
    void store.refresh().then(() => store.selectDevice(snapshot.serial));
  }, [snapshot.serial, store]);
  return <DevicePanel store={store} source="模拟 Runtime 设备快照（无设备连接）" now={now} />;
}

const meta = {
  title: '接入回归/本机设备面板',
  component: DevicePanel,
  render: () => <DevicePreview />,
  parameters: {
    docs: {
      description: {
        component:
          '本机设备、健康、能力声明与只读靶场安装检查。全部使用合成快照，不请求 Runtime、ADB、Agent 或团队服务。',
      },
    },
  },
} satisfies Meta<typeof DevicePanel>;
export default meta;
type Story = StoryObj<typeof meta>;

export const ReadyWithoutOcr: Story = {};
export const UnknownCapabilities: Story = {
  render: () => (
    <DevicePreview
      snapshot={simulatedDevice('SIM', {
        status: 'agent_available',
        ready: false,
        connection: {},
        capabilities: null,
        observed_at_ms: null,
      })}
    />
  ),
};
export const Disconnected: Story = {
  render: () => (
    <DevicePreview
      snapshot={simulatedDevice('SIM', {
        state: 'absent',
        status: 'disconnected',
        ready: false,
        connection: {},
        capabilities: null,
      })}
    />
  ),
};
export const Unauthorized: Story = {
  render: () => (
    <DevicePreview
      snapshot={simulatedDevice('SIM', {
        state: 'unauthorized',
        status: 'adb_unauthorized',
        ready: false,
        connection: {},
        capabilities: null,
        diagnostic: {
          code: 'adb_unauthorized',
          manual_required: true,
          guidance: 'Authorize this computer in the device USB debugging prompt.',
        },
      })}
    />
  ),
};
export const NativeBusy: Story = {
  render: () => (
    <DevicePreview
      snapshot={simulatedDevice('SIM', {
        status: 'agent_busy',
        ready: false,
        capabilities: { ...simulatedDevice().capabilities, platform_operation_pending: true },
      })}
    />
  ),
};
export const RecoveryRequired: Story = {
  render: () => (
    <DevicePreview
      snapshot={simulatedDevice('SIM', {
        status: 'recovery_required',
        ready: false,
        control: { recovery_required: true },
        diagnostic: {
          code: 'action_uncertain',
          manual_required: true,
          cleanup_pending: true,
          guidance: 'Inspect the target state manually. No automatic recovery.',
        },
      })}
    />
  ),
};
export const StaleSnapshot: Story = {
  render: () => <DevicePreview now={DEVICE_PREVIEW_TIME + 45_000} />,
};
export const RuntimeUnavailable: Story = { render: () => <DevicePreview offline /> };
