import {
  BookOpen,
  FileText,
  FolderTree,
  LayoutDashboard,
  PlayCircle,
  Settings,
  Smartphone,
  type LucideIcon,
} from 'lucide-react';

export type AppPath =
  '/' | '/cases' | '/devices' | '/runs' | '/reports' | '/experience' | '/settings';

export interface NavItem {
  to: AppPath;
  label: string;
  description: string;
  icon: LucideIcon;
  keywords: readonly string[];
}

export const NAV_ITEMS: readonly NavItem[] = [
  {
    to: '/',
    label: '工作台',
    description: '最近用例、运行中任务与待处理项',
    icon: LayoutDashboard,
    keywords: ['workbench', 'home', 'dashboard', '最近', '任务'],
  },
  {
    to: '/cases',
    label: '用例',
    description: '本地工程、用例树与可停靠工作室',
    icon: FolderTree,
    keywords: ['cases', 'studio', 'editor', '工程', '编辑'],
  },
  {
    to: '/devices',
    label: '设备',
    description: '本机设备、公共设备池与我的占用',
    icon: Smartphone,
    keywords: ['devices', 'adb', '设备池', '占用'],
  },
  {
    to: '/runs',
    label: '运行',
    description: '运行中任务、历史与计划',
    icon: PlayCircle,
    keywords: ['runs', 'run', '运行中', '任务'],
  },
  {
    to: '/reports',
    label: '报告',
    description: '本地报告、团队报告与环境覆盖',
    icon: FileText,
    keywords: ['reports', 'report', '报告', '证据'],
  },
  {
    to: '/experience',
    label: '经验',
    description: '经验浏览、来源与验证记录',
    icon: BookOpen,
    keywords: ['experience', '经验', '知识'],
  },
  {
    to: '/settings',
    label: '设置',
    description: 'Runtime、服务端、快捷键与外观',
    icon: Settings,
    keywords: ['settings', 'preferences', '设置', '快捷键'],
  },
];
