import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import type { PulseBridge } from '../../../shared/ipc';
import { CaseEditorPlaceholder, CaseTree } from './CaseTree';
import { getProjectStore, PROJECT_STORAGE_KEY, resetProjectStores } from './projectStore';

const detail = {
  allowed_apps: ['com.pulse.target'],
  case_ids: ['starter', 'other'],
  contract: 'pulse-runtime-api-draft',
  data_ids: ['cart'],
  default_strategy: 'rule_only',
  entry_cases: ['starter'],
  flow_ids: ['login'],
  manifest: {},
  name: 'Demo project',
  platforms: ['android'],
  profile_ids: ['android-local'],
  project_id: 'demo',
  required_capabilities: [],
  revision: '7',
  root: '/tmp/demo',
};

function fakeBridge(): PulseBridge {
  return {
    app: { getInfo: async () => ({ version: '1', platform: 'darwin' }) },
    runtime: {
      getConnection: async () => ({ port: 4311, token: 'token', pid: 9, version: '1.0.0' }),
      getStatus: async () => ({ kind: 'not-running', launchable: false, detail: 'test' }),
      ensureRunning: async () => ({ kind: 'not-running', launchable: false, detail: 'test' }),
    },
    projects: {
      pickDirectory: async () => null,
      createProject: async () => ({
        root: '/tmp/created',
        projectId: 'created',
        fileCount: 3,
        totalBytes: 1,
      }),
      importProject: async () => null,
      exportProject: async () => null,
    },
  };
}

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  resetProjectStores();
  delete window.pulse;
  vi.unstubAllGlobals();
});

test('renders the local case tree and keeps the selected case in the editor placeholder', async () => {
  window.localStorage.setItem(
    PROJECT_STORAGE_KEY,
    JSON.stringify({ version: 1, root: '/tmp/demo', projectId: 'demo' }),
  );
  window.pulse = fakeBridge();
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    if (url.pathname === '/projects' && init?.method === 'POST') {
      return Response.json(detail, { status: 201 });
    }
    if (url.pathname.endsWith('/cases')) {
      return Response.json({
        starter: { name: '启动靶场应用', goal: '确认目标应用可以启动' },
        other: { name: '购物车数量', goal: '数量必须为 1' },
      });
    }
    return Response.json({});
  });
  vi.stubGlobal('fetch', fetchMock);

  await getProjectStore().hydrate();
  render(
    <>
      <CaseTree />
      <CaseEditorPlaceholder />
    </>,
  );

  expect(await screen.findByTestId('project-case-tree')).toBeVisible();
  expect(screen.getByText('Demo project')).toBeVisible();
  expect(screen.getByText('入口')).toBeVisible();
  expect(screen.getByRole('button', { name: /启动靶场应用/ })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  expect(screen.getByRole('heading', { name: '启动靶场应用' })).toBeVisible();

  await userEvent.click(screen.getByRole('button', { name: /购物车数量/ }));
  expect(screen.getByRole('button', { name: /购物车数量/ })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  expect(screen.getByRole('heading', { name: '购物车数量' })).toBeVisible();
  expect(screen.getByText('数量必须为 1')).toBeVisible();
});

test('shows the empty state when no project is open', () => {
  resetProjectStores();
  render(<CaseTree />);
  expect(screen.getByRole('heading', { name: '尚未打开工程' })).toBeVisible();
  expect(screen.getByRole('button', { name: '打开或新建工程' })).toBeVisible();
});
