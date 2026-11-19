import { useEffect, useMemo, useState } from 'react';
import { Outlet, useNavigate, useRouterState } from '@tanstack/react-router';
import { Download, FolderOpen, LayoutPanelLeft, PackagePlus } from 'lucide-react';
import { useWorkbenchLayout, PRESET_IDS, WORKBENCH_PRESETS } from '../features/cases/layoutStore';
import { ProjectManagerDialog } from '../features/projects/ProjectManagerDialog';
import { useProjectStore } from '../features/projects/projectStore';
import { CommandPalette, type CommandItem } from '../shared/ui';
import { NAV_ITEMS } from './navItems';
import { SideNav } from './SideNav';
import { TopBar } from './TopBar';

export function AppShell() {
  const [paletteOpen, setPaletteOpen] = useState(false);
  const navigate = useNavigate();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const layout = useWorkbenchLayout();
  const { state: projectState, store: projectStore } = useProjectStore();

  useEffect(() => {
    void projectStore.hydrate();
  }, [projectStore]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.altKey) return;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setPaletteOpen(true);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  useEffect(() => {
    const normalized =
      pathname.length > 1 && pathname.endsWith('/') ? pathname.slice(0, -1) : pathname;
    const match = NAV_ITEMS.find((item) => item.to === normalized);
    document.title = `Pulse · ${match?.label ?? '页面不存在'}`;
  }, [pathname]);

  const commands = useMemo<CommandItem[]>(() => {
    const navigation = NAV_ITEMS.map((item) => {
      const Icon = item.icon;
      return {
        id: `nav-${item.to}`,
        group: '导航',
        label: `转到${item.label}`,
        hint: item.description,
        keywords: [item.label, ...item.keywords],
        icon: <Icon />,
        run: () => {
          void navigate({ to: item.to });
        },
      } satisfies CommandItem;
    });

    const layouts = PRESET_IDS.map(
      (id) =>
        ({
          id: `layout-${id}`,
          group: '布局',
          label: `以${WORKBENCH_PRESETS[id].label}布局打开用例工作室`,
          hint: WORKBENCH_PRESETS[id].description,
          keywords: ['布局', '预设', id, WORKBENCH_PRESETS[id].label],
          icon: <LayoutPanelLeft />,
          run: () => {
            layout.store.setActivePreset(id);
            void navigate({ to: '/cases' });
          },
        }) satisfies CommandItem,
    );

    const projects = [
      {
        id: 'project-manage',
        group: '工程',
        label: '工程管理',
        hint: '新建、打开、导入或导出本地测试工程',
        keywords: ['project', '工程', '打开', '新建', '导入', '导出'],
        icon: <FolderOpen />,
        run: () => projectStore.openDialog(),
      },
      {
        id: 'project-open',
        group: '工程',
        label: '打开本地工程',
        hint: '选择已有工程目录并由本机 Runtime 校验',
        keywords: ['open', 'project', '打开', '目录'],
        icon: <FolderOpen />,
        run: () => {
          void projectStore.openFromDialog();
        },
      },
      {
        id: 'project-import',
        group: '工程',
        label: '导入 Pulse 工程包',
        hint: '校验并导入 .pulse-project.json 工程包',
        keywords: ['import', 'package', '导入', '工程包'],
        icon: <PackagePlus />,
        run: () => {
          void projectStore.importProject();
        },
      },
      {
        id: 'project-export',
        group: '工程',
        label: '导出当前工程包',
        hint: projectState.project ? `导出 ${projectState.project.name}` : '尚未打开工程',
        keywords: ['export', 'package', '导出', '工程包'],
        icon: <Download />,
        disabled: !projectState.project,
        run: () => {
          void projectStore.exportProject();
        },
      },
    ] satisfies CommandItem[];

    return [...navigation, ...layouts, ...projects];
  }, [layout.store, navigate, projectState.project, projectStore]);

  return (
    <div className="app-shell">
      <a
        className="app-skip-link"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById('main-content')?.focus();
        }}
      >
        跳到主要内容
      </a>
      <TopBar onOpenPalette={() => setPaletteOpen(true)} />
      <div className="app-body">
        <SideNav />
        <main id="main-content" className="app-main" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
      <CommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} items={commands} />
      <ProjectManagerDialog />
    </div>
  );
}
