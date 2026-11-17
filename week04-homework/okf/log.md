# Bundle Update Log

## 2026-11-16

- **Update**: 将会话测试迁移到 `tests/auth_new/`（`tests/auth/token.test.ts` → `tests/auth_new/token.test.ts`、`tests/auth/app.test.ts` → `tests/auth_new/app.test.ts`、`src/tests/auth-service.test.ts` → `tests/auth_new/auth-service.test.ts`）；`package.json` 的 `test` 脚本更新为 `vitest run tests/auth_new src/tests`。同步更新 [测试入口与执行记录](/testing.md) 与 [项目概览](/overview.md) 中的测试路径与执行记录。
- **Update**: 实际运行 `npm test`（= `vitest run tests/auth_new src/tests`），4 文件/10 用例通过；`tests/auth_new/app.test.ts` 因沙箱 `listen EPERM: operation not permitted 0.0.0.0` 仍无法启动 HTTP 服务，5 用例标记为待验证。
- **Update**: 在 [测试入口与执行记录](/testing.md) 中新增“会话（令牌）测试文件与运行命令”小节，记录 `tests/auth/token.test.ts`、`tests/auth/app.test.ts`、`src/tests/auth-service.test.ts` 的路径与单文件/全量运行命令（来源：package.json 与各测试文件）。
- **Creation**: 基于 `login_demo/` 当前源码、`README.md` 与 `artifacts/login-flow.md` 建立知识库。
- **Creation**: 新增 [会话/令牌模块位置](/session-token.md)，记录无独立 session 模块、令牌格式与所在文件。
- **Creation**: 新增 [令牌到期规则](/token-expiry.md)，记录“当前源码未实现任何到期规则”这一源码事实。
- **Creation**: 新增 [测试入口与执行记录](/testing.md)，记录 `npm test` 入口与本次实际执行结果。
- **Creation**: 新增 [项目概览](/overview.md) 与 [登录调用链](/login-flow.md)。
- **Update**: 实际运行 `npx vitest run src/tests tests/auth/token.test.ts`，4 文件/10 用例通过；`npm test` 中 `tests/auth/app.test.ts` 因沙箱 `listen EPERM` 未通过，标记为待验证。

## Questions for maintainers

- `tests/auth_new/app.test.ts` 的 5 个 HTTP 集成用例需在允许本地网络监听的环境复验（当前沙箱报 `listen EPERM: operation not permitted 0.0.0.0`）。
