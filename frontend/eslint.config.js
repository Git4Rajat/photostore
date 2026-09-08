// @ts-check
import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import globals from 'globals';

export default tseslint.config(
  {
    ignores: ['build/**', 'node_modules/**', 'public/**', 'scripts/**'],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      // Only the long-established, stable react-hooks rules. eslint-plugin-
      // react-hooks v7's "recommended" config also bundles newer, stricter
      // React-Compiler-oriented checks (refs/set-state-in-effect/purity/etc.)
      // that flag common, deliberate patterns already used throughout this
      // codebase (e.g. the "latest ref" pattern); adopting those wholesale
      // would require unrelated hook rewrites, not a lint-adoption task.
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      'react-refresh/only-export-components': 'off',
      // The codebase has a large existing surface of `any` usage; keep this
      // a warning (visible, trackable) rather than a hard error so `lint`
      // is adoptable without a big-bang type-safety rewrite.
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/no-unused-vars': [
        'warn',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      '@typescript-eslint/no-empty-object-type': 'warn',
      'no-empty': ['warn', { allowEmptyCatch: true }],
    },
  },
  {
    files: ['**/*.test.{ts,tsx}', 'src/setupTests.ts'],
    languageOptions: {
      globals: {
        ...globals.node,
      },
    },
  },
);
