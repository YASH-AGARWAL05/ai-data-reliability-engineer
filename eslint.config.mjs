import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: [
      ".tools/**",
      ".venv/**",
      ".next/**",
      "**/.next/**",
      "node_modules/**",
      "playwright-report/**",
      "test-results/**",
    ],
  },
  ...tseslint.configs.strictTypeChecked,
  { languageOptions: { parserOptions: { projectService: true } } },
);
