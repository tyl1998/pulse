import { RouterProvider } from '@tanstack/react-router';
import { router as defaultRouter, type AppRouter } from './router';

export interface AppProps {
  router?: AppRouter;
}

export function App({ router }: AppProps) {
  return <RouterProvider router={router ?? defaultRouter} />;
}
