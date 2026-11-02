import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

// Layering (docs/FRONTEND.md §5): app → features → ui, lib. ui/lib never import a
// feature; features talk to each other only through a feature's index.ts.
const layering = {
  "no-restricted-imports": ["error", {
    patterns: [{ group: ["@/features/*/*", "!@/features/*/index"], message: "Import another feature through its index.ts public surface." }],
  }],
};

export default tseslint.config(
  { ignores: ["node_modules", "dist", "src/lib/api/schema.d.ts", "../src"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: { globals: { ...globals.browser } },
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      "@typescript-eslint/consistent-type-imports": "error",
      ...layering,
    },
  },
  {
    files: ["src/ui/**", "src/lib/**"],
    rules: { "no-restricted-imports": ["error", { patterns: [{ group: ["@/features/*", "@/app/*"], message: "ui/ and lib/ must not depend on features or the app shell." }] }] },
  },
  { files: ["scripts/**", "*.config.*"], languageOptions: { globals: { ...globals.node } } },
);
