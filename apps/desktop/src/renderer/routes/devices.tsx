import { DevicePanel } from '../features/devices/DevicePanel';
import { PageHeader } from './PageScaffold';

export function DevicesPage() {
  return (
    <div className="route-page">
      <PageHeader
        title="设备"
        description="查看本机设备的连接、能力与健康事实。公共设备池和我的占用在 P4 接入。"
        phase="本机 · FE-P2.5"
      />
      <DevicePanel />
    </div>
  );
}
