import { defineConfig } from "vitest/config";

/** login_demo 使用 Node 内置 test runner，由它自己的 `npm test` 执行；不能被 planner 的 Vitest 二次收集。 */
export default defineConfig({
  test: {
    exclude: ["login_demo/**", "node_modules/**", "dist/**"],
  },
});
