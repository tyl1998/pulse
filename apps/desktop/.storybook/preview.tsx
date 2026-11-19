import { useEffect, type ReactNode } from 'react';
import type { Preview } from '@storybook/react-vite';
import '../src/renderer/shared/ui/styles.css';
import '../src/renderer/app/shell.css';
import './preview.css';

function PreviewFrame({
  children,
  density,
  title,
  hasMain,
}: {
  children: ReactNode;
  density: string;
  title: string;
  hasMain: boolean;
}) {
  useEffect(() => {
    document.documentElement.dataset.density = density;
    document.documentElement.lang = 'zh-CN';
  }, [density]);
  const Container = hasMain ? 'div' : 'main';
  const frameProps = hasMain ? { role: 'region' as const, 'aria-label': 'Storybook 预览区域' } : {};
  return (
    <Container className="sb-frame" {...frameProps}>
      <div className="sb-heading">
        <h1>Pulse / {title}</h1>
        <p>
          前端基础组件 · {density === 'comfortable' ? '舒适' : '紧凑'}密度 · 仅模拟数据，不连接
          Runtime 或设备
        </p>
      </div>
      {children}
    </Container>
  );
}

const preview: Preview = {
  tags: ['autodocs'],
  globalTypes: {
    density: {
      description: '控件密度（仅预览，不持久化）',
      toolbar: {
        icon: 'component',
        dynamicTitle: true,
        items: [
          { value: 'compact', title: '紧凑' },
          { value: 'comfortable', title: '舒适' },
        ],
      },
    },
  },
  initialGlobals: { density: 'compact' },
  parameters: {
    layout: 'fullscreen',
    controls: { expanded: true },
    a11y: { test: 'error' },
    options: { storySort: { order: ['基础', '组件', '接入回归'] } },
  },
  decorators: [
    (Story, context) => (
      <PreviewFrame
        density={context.globals.density === 'comfortable' ? 'comfortable' : 'compact'}
        title={context.title.split('/').at(-1) ?? 'UI'}
        hasMain={context.parameters.hasMain === true}
      >
        <Story />
      </PreviewFrame>
    ),
  ],
};

export default preview;
