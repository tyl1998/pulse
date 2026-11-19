import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createHashHistory, createMemoryHistory, type RouterHistory } from '@tanstack/react-router';
import { afterEach, expect, test, vi } from 'vitest';
import { App } from './App';
import { createAppRouter } from './router';
import { resetWorkbenchLayoutStores } from '../features/cases/layoutStore';
import { resetDeviceStore } from '../features/devices/deviceStore';
import { resetProjectStores } from '../features/projects/projectStore';

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  resetWorkbenchLayoutStores();
  resetDeviceStore();
  resetProjectStores();
  vi.restoreAllMocks();
});

function renderApp(path = '/') {
  const router = createAppRouter(createMemoryHistory({ initialEntries: [path] }));
  const view = render(<App router={router} />);
  return { router, unmount: view.unmount };
}

test('renders the workbench shell and routes through the side navigation', async () => {
  const { router } = renderApp();
  expect(await screen.findByRole('heading', { name: '工作台' })).toBeVisible();
  const nav = screen.getByRole('navigation', { name: '主导航' });
  expect(nav).toHaveTextContent('用例');
  expect(screen.getByTestId('runtime-indicator')).toHaveAttribute('data-state', 'searching');
  expect(screen.getByTestId('server-indicator')).toHaveTextContent('服务端未配置');
  expect(screen.getByTestId('model-indicator')).toHaveTextContent('模型未接入');

  await userEvent.click(screen.getByRole('link', { name: '用例' }));
  expect(await screen.findByRole('heading', { name: '用例' })).toBeVisible();
  expect(router.state.location.pathname).toBe('/cases');
  expect(screen.getByTestId('workbench-dock')).toHaveAttribute('data-preset', 'edit');
});

test('device navigation reaches the local device panel without a preload, Runtime or team server', async () => {
  const fetchSpy = vi.spyOn(globalThis, 'fetch');
  const { router } = renderApp();
  await screen.findByRole('heading', { name: '工作台' });
  await userEvent.click(screen.getByRole('link', { name: '设备' }));
  expect(await screen.findByTestId('device-panel')).toBeVisible();
  expect(await screen.findByRole('heading', { name: '设备状态不可确认' })).toBeVisible();
  expect(router.state.location.pathname).toBe('/devices');
  expect(screen.queryByText('设备面板未接入')).toBeNull();
  expect(screen.queryByRole('heading', { name: '未发现本机设备' })).toBeNull();
  expect(fetchSpy).not.toHaveBeenCalled();
});

test.each(['click', 'Enter'])(
  'the skip link focuses main without changing the hash route on %s or reload',
  async (activation) => {
    const previousUrl = window.location.href;
    const previousState = window.history.state;
    window.history.replaceState(previousState, '', '#/runs');
    const history = createHashHistory();
    let reloadedHistory: RouterHistory | undefined;

    try {
      const router = createAppRouter(history);
      const { unmount } = render(<App router={router} />);
      expect(await screen.findByRole('heading', { name: '运行' })).toBeVisible();
      const skipLink = screen.getByRole('link', { name: '跳到主要内容' });
      const url = window.location.href;
      const historyLength = window.history.length;

      if (activation === 'click') {
        await userEvent.click(skipLink);
      } else {
        await userEvent.tab();
        expect(skipLink).toHaveFocus();
        await userEvent.keyboard('{Enter}');
      }

      expect(window.location.href).toBe(url);
      expect(window.history.length).toBe(historyLength);
      expect(router.state.location.pathname).toBe('/runs');
      expect(screen.getByRole('main')).toHaveFocus();

      unmount();
      reloadedHistory = createHashHistory();
      const reloadedRouter = createAppRouter(reloadedHistory);
      render(<App router={reloadedRouter} />);
      expect(await screen.findByRole('heading', { name: '运行' })).toBeVisible();
      expect(reloadedRouter.state.location.pathname).toBe('/runs');
      expect(screen.queryByRole('heading', { name: '页面不存在' })).not.toBeInTheDocument();
    } finally {
      cleanup();
      reloadedHistory?.destroy();
      history.destroy();
      window.history.replaceState(previousState, '', previousUrl);
    }
  },
);

test('Meta+K opens the command palette and a layout command navigates with that preset', async () => {
  const { router } = renderApp();
  await screen.findByRole('navigation', { name: '主导航' });
  fireEvent.keyDown(window, { key: 'k', metaKey: true });
  expect(await screen.findByRole('dialog')).toBeVisible();

  const input = screen.getByRole('combobox', { name: '搜索命令' });
  await userEvent.type(input, '录制布局');
  expect(screen.getAllByRole('option')).toHaveLength(1);
  await userEvent.keyboard('{Enter}');

  expect(await screen.findByTestId('workbench-dock')).toHaveAttribute('data-preset', 'record');
  expect(router.state.location.pathname).toBe('/cases');
  expect(screen.queryByRole('dialog')).toBeNull();
});

test('the top bar command trigger opens the same palette and regains focus on Escape', async () => {
  renderApp();
  await screen.findByRole('navigation', { name: '主导航' });
  const trigger = screen.getByRole('button', { name: '搜索命令' });
  await userEvent.click(trigger);
  expect(await screen.findByRole('dialog')).toBeVisible();
  const input = screen.getByRole('combobox', { name: '搜索命令' });
  await waitFor(() => expect(input).toHaveFocus());
  await userEvent.type(input, '设备');
  await userEvent.keyboard('{Escape}');
  await waitFor(() => expect(trigger).toHaveFocus());
  expect(screen.queryByRole('dialog')).toBeNull();
});

test.each(['metaKey', 'ctrlKey'] as const)(
  '%s+K from an input returns focus and selection to that input on Escape',
  async (modifier) => {
    renderApp();
    await screen.findByRole('navigation', { name: '主导航' });
    await userEvent.click(screen.getByRole('button', { name: '未打开工程' }));
    await userEvent.click(screen.getByRole('button', { name: '新建工程' }));
    const editor = screen.getByRole('textbox', { name: '工程名称' });
    await userEvent.type(editor, '未保存的工程名称');
    (editor as HTMLInputElement).setSelectionRange(1, 4);

    fireEvent.keyDown(editor, { key: 'k', [modifier]: true });
    expect(await screen.findByRole('dialog', { name: '命令面板' })).toBeVisible();
    await waitFor(() => expect(screen.getByRole('combobox', { name: '搜索命令' })).toHaveFocus());
    await userEvent.keyboard('{Escape}');

    await waitFor(() => expect(editor).toHaveFocus());
    expect(editor).toHaveValue('未保存的工程名称');
    expect((editor as HTMLInputElement).selectionStart).toBe(1);
    expect((editor as HTMLInputElement).selectionEnd).toBe(4);
    expect(screen.queryByRole('dialog', { name: '命令面板' })).toBeNull();
    expect(screen.getByRole('dialog', { name: '工程管理' })).toBeVisible();
  },
);

test('the project management command hands focus to its dialog without refocusing the opener', async () => {
  renderApp();
  await screen.findByRole('navigation', { name: '主导航' });
  const trigger = screen.getByRole('button', { name: '搜索命令' });
  await userEvent.click(trigger);
  const palette = await screen.findByRole('dialog', { name: '命令面板' });
  await userEvent.type(screen.getByRole('combobox', { name: '搜索命令' }), '工程管理');
  const focusOpener = vi.spyOn(trigger, 'focus');
  const closed = vi.fn();
  palette.addEventListener('focusScope.autoFocusOnUnmount', closed);

  await userEvent.keyboard('{Enter}');

  expect(await screen.findByRole('dialog', { name: '工程管理' })).toBeVisible();
  await waitFor(() => expect(closed).toHaveBeenCalledOnce());
  expect(screen.queryByRole('dialog', { name: '命令面板' })).toBeNull();
  expect(screen.getByRole('heading', { name: '工程管理' })).toHaveFocus();
  expect(focusOpener).not.toHaveBeenCalled();
  await userEvent.tab();
  expect(screen.getByRole('button', { name: '打开工程' })).toHaveFocus();
});

test('top bar project switcher opens the project manager and create form', async () => {
  renderApp();
  await screen.findByRole('navigation', { name: '主导航' });
  await userEvent.click(screen.getByRole('button', { name: '未打开工程' }));
  expect(await screen.findByRole('dialog')).toBeVisible();
  expect(screen.getByTestId('project-manager')).toHaveTextContent('尚未打开工程');

  await userEvent.click(screen.getByRole('button', { name: '新建工程' }));
  expect(screen.getByRole('heading', { name: '新建空白工程' })).toBeVisible();
  expect(screen.getByRole('button', { name: '选择目录并创建' })).toBeVisible();
});

test('unknown hashes show the shell not-found page', async () => {
  renderApp('/missing');
  expect(await screen.findByRole('heading', { name: '页面不存在' })).toBeVisible();
  expect(screen.getByRole('link', { name: '返回工作台' })).toHaveAttribute('href');
});

test('layout preset chosen in one session is restored after the stores are recreated', async () => {
  const first = renderApp('/cases');
  await userEvent.click(await screen.findByRole('button', { name: '录制' }));
  expect(screen.getByTestId('workbench-dock')).toHaveAttribute('data-preset', 'record');

  first.unmount();
  resetWorkbenchLayoutStores();
  const router = createAppRouter(createMemoryHistory({ initialEntries: ['/cases'] }));
  render(<App router={router} />);
  expect(await screen.findByTestId('workbench-dock')).toHaveAttribute('data-preset', 'record');
  expect(screen.getByRole('button', { name: '录制' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByRole('tab', { name: '事件日志' })).toHaveAttribute('data-state', 'active');
});
