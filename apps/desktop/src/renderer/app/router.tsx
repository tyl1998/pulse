import {
  createHashHistory,
  createRootRoute,
  createRoute,
  createRouter,
  type RouterHistory,
} from '@tanstack/react-router';
import { CasesPage } from '../routes/cases';
import { DevicesPage } from '../routes/devices';
import { ExperiencePage } from '../routes/experience';
import { ReportsPage } from '../routes/reports';
import { RunsPage } from '../routes/runs';
import { SettingsPage } from '../routes/settings';
import { WorkbenchPage } from '../routes/workbench';
import { AppShell } from './AppShell';
import { NotFoundPage, RouteErrorPage } from './RouterFallback';

export const rootRoute = createRootRoute({
  component: AppShell,
  notFoundComponent: NotFoundPage,
  errorComponent: RouteErrorPage,
});

const workbenchRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  component: WorkbenchPage,
});

const casesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/cases',
  component: CasesPage,
});

const devicesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/devices',
  component: DevicesPage,
});

const runsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/runs',
  component: RunsPage,
});

const reportsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/reports',
  component: ReportsPage,
});

const experienceRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/experience',
  component: ExperiencePage,
});

const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/settings',
  component: SettingsPage,
});

const routeTree = rootRoute.addChildren([
  workbenchRoute,
  casesRoute,
  devicesRoute,
  runsRoute,
  reportsRoute,
  experienceRoute,
  settingsRoute,
]);

export function createAppRouter(history: RouterHistory = createHashHistory()) {
  return createRouter({
    routeTree,
    history,
    defaultPreload: false,
    scrollRestoration: false,
  });
}

export type AppRouter = ReturnType<typeof createAppRouter>;

export const router = createAppRouter();

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router;
  }
}
