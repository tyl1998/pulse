// @ts-check
import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';
import boundaries from 'eslint-plugin-boundaries';
import prettier from 'eslint-config-prettier';
import globals from 'globals';

const R = 'apps/desktop/src/renderer';

/** Disallow `from` layer importing any of `to` layers. */
const forbid = (from, to) => ({
  from: { element: { type: from } },
  disallow: { to: { element: { types: { anyOf: to } } } },
});

export default tseslint.config(
  {
    ignores: [
      '**/node_modules/**',
      '**/dist/**',
      '**/out/**',
      '**/.vite/**',
      '**/coverage/**',
      '**/storybook-static/**',
      '**/.cache/**',
      '.venv/**',
      'fixtures/target-app/**',
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.strict,
  {
    languageOptions: { globals: { ...globals.node } },
  },
  {
    files: [`${R}/**/*.{ts,tsx}`],
    languageOptions: { globals: { ...globals.browser } },
    plugins: { 'react-hooks': reactHooks },
    rules: reactHooks.configs.recommended.rules,
  },
  // Process + renderer layer boundaries (desktop-frontend.md §3/§4):
  // routes -> features -> entities -> shared; renderer never imports main/preload.
  {
    files: ['apps/desktop/src/**/*.{ts,tsx}'],
    plugins: { boundaries },
    settings: {
      'boundaries/elements': [
        { type: 'main', pattern: 'apps/desktop/src/main' },
        { type: 'preload', pattern: 'apps/desktop/src/preload' },
        { type: 'ipc', pattern: 'apps/desktop/src/shared' },
        { type: 'app', pattern: `${R}/app` },
        { type: 'routes', pattern: `${R}/routes` },
        { type: 'features', pattern: `${R}/features/*` },
        { type: 'entities', pattern: `${R}/entities` },
        { type: 'shared', pattern: `${R}/shared` },
        { type: 'workers', pattern: `${R}/workers` },
      ],
      'import/resolver': {
        typescript: { project: 'apps/desktop/tsconfig.json' },
      },
    },
    rules: {
      'boundaries/dependencies': [
        2,
        {
          default: 'allow',
          policies: [
            forbid('main', [
              'preload',
              'app',
              'routes',
              'features',
              'entities',
              'shared',
              'workers',
            ]),
            forbid('preload', [
              'main',
              'app',
              'routes',
              'features',
              'entities',
              'shared',
              'workers',
            ]),
            forbid('ipc', ['main', 'preload', 'app', 'routes', 'features', 'entities', 'shared']),
            forbid('app', ['main', 'preload']),
            forbid('routes', ['main', 'preload', 'app']),
            forbid('features', ['main', 'preload', 'app', 'routes']),
            forbid('entities', ['main', 'preload', 'app', 'routes', 'features']),
            forbid('shared', ['main', 'preload', 'app', 'routes', 'features', 'entities']),
            forbid('workers', ['main', 'preload', 'app', 'routes', 'features', 'entities']),
          ],
        },
      ],
    },
  },
  prettier,
);
