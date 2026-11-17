---
type: Reference
title: 测试入口与执行记录
description: login_demo 的测试命令、5 个测试文件清单（会话测试位于 tests/auth_new/），以及 2026-11-16 迁移后的实际执行结果（含沙箱环境限制）。
tags: [testing, vitest]
status: stable
generated: { by: pi-agent/deepseek-v4-pro, at: 2026-11-16T08:49:20Z }
sources:
  - id: package
    resource: ../login_demo/package.json
    title: package.json（test 脚本）
  - id: app-test
    resource: ../login_demo/tests/auth_new/app.test.ts
    title: HTTP 集成测试
  - id: token-test
    resource: ../login_demo/tests/auth_new/token.test.ts
    title: token 单元测试
  - id: auth-service-test
    resource: ../login_demo/tests/auth_new/auth-service.test.ts
    title: AuthService 单元测试
  - id: password-test
    resource: ../login_demo/src/tests/password.test.ts
    title: verifyPassword 单元测试
  - id: user-repo-test
    resource: ../login_demo/src/tests/user-repository.test.ts
    title: UserRepository 单元测试
---

# 测试入口与执行记录

## 测试命令（来源：package.json）

- 主入口：`npm test`，等价于 `vitest run tests/auth_new src/tests`。
- 运行单个文件：`npx vitest run <file>`。

## 会话（令牌）测试文件与运行命令

本项目的“会话”即访问令牌（见 [session-token.md](/session-token.md)），会话相关测试集中在以下文件：

| 会话测试文件 | 测试内容 | 来源 |
|--------------|----------|------|
| `tests/auth_new/token.test.ts` | `issueAccessToken` / `parseAccessToken` 往返、非法前缀、非法载荷 | 文件内 `describe("access token helpers")` |
| `tests/auth_new/app.test.ts` | `GET /api/profile` 无 token 返回 401、带 Bearer token 返回 200 并回显 `currentUser` | 文件内用例 `protects the profile route and accepts a valid bearer token` |
| `tests/auth_new/auth-service.test.ts` | `AuthService.login` 成功时返回 accessToken | 文件内用例 `returns a token and public user data for valid credentials` |

运行命令（来源：package.json `scripts.test`）：

- 全部测试：`npm test`（等价于 `vitest run tests/auth_new src/tests`）
- 仅令牌单元测试：`npx vitest run tests/auth_new/token.test.ts`
- 仅会话鉴权 HTTP 测试：`npx vitest run tests/auth_new/app.test.ts`
- 仅登录服务（签发令牌）测试：`npx vitest run tests/auth_new/auth-service.test.ts`

## 测试文件清单（共 5 个）

| 文件 | 类型 | 覆盖内容 |
|------|------|----------|
| `tests/auth_new/app.test.ts` | HTTP 集成 | 健康检查、登录成功/失败、受保护 `/api/profile` |
| `tests/auth_new/token.test.ts` | 单元 | `issueAccessToken` / `parseAccessToken` 往返、非法前缀、非法载荷 |
| `tests/auth_new/auth-service.test.ts` | 单元 | `AuthService.login` 成功/未知邮箱/错误密码 |
| `src/tests/password.test.ts` | 单元 | `verifyPassword` 匹配/不匹配 |
| `src/tests/user-repository.test.ts` | 单元 | `findByEmail` 命中/未命中 |

## 执行记录（2026-11-16，会话测试迁移到 tests/auth_new 后由本 Agent 实际运行）

### 本次实际结果

命令 `npm test`（等价于 `vitest run tests/auth_new src/tests`）结果：

- **4 个测试文件通过，10 个用例通过**：`tests/auth_new/token.test.ts`（3）、`tests/auth_new/auth-service.test.ts`（3）、`src/tests/password.test.ts`（2）、`src/tests/user-repository.test.ts`（2）。
- **1 个测试文件失败，5 个用例失败**：`tests/auth_new/app.test.ts`（HTTP 集成测试）。

### 失败原因（环境限制，非断言失败）

`tests/auth_new/app.test.ts` 的 5 个用例**未能通过**，失败原因不是断言失败，而是环境限制：

- 错误：`listen EPERM: operation not permitted 0.0.0.0`
- 位置：`tests/auth_new/app.test.ts` 的 `startApp()` 中 `createApp().listen(0)`。
- 原因：当前沙箱禁止绑定 `0.0.0.0`（`listen(0)` 默认绑定所有接口），导致测试无法启动临时 HTTP 服务。
- 因此该文件 5 个用例的 HTTP 行为（`/health`、登录 200/400/401、`/api/profile` 鉴权）**标记为待验证**，需在允许本地网络监听的环境中运行 `npm test` 复验。

## 结论

- 10 个纯单元用例：已在本环境验证通过。
- 5 个 HTTP 集成用例：因沙箱 `EPERM` 无法启动服务器，结果待验证。

## 相关概念

- 项目概览：[overview.md](/overview.md)
- 登录调用链：[login-flow.md](/login-flow.md)
