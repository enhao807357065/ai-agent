import { defineConfig } from "vitest/config";

/** login_demo 与 demo-app fixture 都使用 Node 内置 test runner，各自由自己的 `npm test` 执行；不能被 planner 的 Vitest 二次收集。 */
export default defineConfig({
  test: {
    exclude: ["login_demo/**", "fixtures/**", "node_modules/**", "dist/**"],
  },
});
