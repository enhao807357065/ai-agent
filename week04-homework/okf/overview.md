---
type: Reference
title: login_demo 项目概览
description: login_demo 是一个用于演示“代码库 Agent 如何定位登录流程”的 TypeScript/Express 教学项目。
tags: [overview, express, typescript, login]
status: stable
generated: { by: pi-agent/deepseek-v4-pro, at: 2026-11-16T08:49:20Z }
sources:
  - id: readme
    resource: ../login_demo/README.md
    title: login_demo README
  - id: package
    resource: ../login_demo/package.json
    title: package.json
  - id: tsconfig
    resource: ../login_demo/tsconfig.json
    title: tsconfig.json
---

# login_demo 项目概览

`login_demo` 是一个 TypeScript + Express 的演示项目，目标是展示“代码库 Agent 如何定位登录流程”。它不是生产级实现。

## 技术栈（来源：package.json）

- 运行时依赖：`express` ^4.21.2
- 开发依赖：`typescript` ^5.9.2、`vitest` ^4.1.12、`ts-node-dev` ^2.0.0
- 编译目标：ES2020，模块解析 Node16（来源：tsconfig.json）

## npm 脚本（来源：package.json）

| 脚本 | 命令 | 说明 |
|------|------|------|
| `dev` | `ts-node-dev --respawn --transpile-only src/server.ts` | 开发启动 |
| `build` | `tsc -p tsconfig.json` | 编译到 dist/ |
| `start` | `node dist/server.js` | 运行编译产物 |
| `test` | `vitest run tests/auth_new src/tests` | 运行测试（详见 [testing.md](/testing.md)） |

## 目录分层

| 层 | 位置 | 职责 |
|----|------|------|
| 启动/挂载 | `src/server.ts`、`src/app.ts` | 创建 Express 应用、监听 3001、挂载 `/api` 路由 |
| 路由 | `src/routes/login.ts`、`src/routes/profile.ts` | HTTP 入参/响应 |
| 业务 | `src/services/auth-service.ts` | 登录判断 |
| 仓储 | `src/repositories/user-repository.ts` | 按邮箱查用户 |
| 数据访问 | `src/db/database.ts` | 内存 `users` 表模拟 |
| 工具 | `src/utils/password.ts`、`src/utils/token.ts` | 密码校验、token 编解码 |
| 中间件 | `src/middleware/auth-middleware.ts` | 登录后身份校验 |
| 类型 | `src/types/user.ts` | `UserRecord`、`AuthTokenPayload`、`LoginResult` |

## 教学性质的简化（来源：src/utils/password.ts、src/db/database.ts）

- 密码校验是 `passwordHash === "hash:" + 明文密码`，不是真实哈希。
- “数据库”是内存数组，内置两条用户记录，密码以 `hash:明文` 形式保存。
- 具体用户邮箱/密码明文不在本知识库中记录（避免保存凭证）。

## 相关概念

- 登录调用链：[login-flow.md](/login-flow.md)
- 会话/令牌模块位置：[session-token.md](/session-token.md)
- 令牌到期规则：[token-expiry.md](/token-expiry.md)
- 测试入口与执行记录：[testing.md](/testing.md)
