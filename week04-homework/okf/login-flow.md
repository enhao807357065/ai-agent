---
type: Playbook
title: login_demo 登录调用链
description: POST /api/login 的完整调用链，以及登录后访问受保护接口的鉴权链。
tags: [login, flow, express]
status: stable
generated: { by: pi-agent/deepseek-v4-pro, at: 2026-11-16T06:49:50Z }
sources:
  - id: server
    resource: ../login_demo/src/server.ts
    title: 服务启动入口
  - id: app
    resource: ../login_demo/src/app.ts
    title: 路由挂载
  - id: login-route
    resource: ../login_demo/src/routes/login.ts
    title: 登录路由
  - id: profile-route
    resource: ../login_demo/src/routes/profile.ts
    title: 受保护路由
  - id: auth-service
    resource: ../login_demo/src/services/auth-service.ts
    title: 登录业务逻辑
  - id: user-repo
    resource: ../login_demo/src/repositories/user-repository.ts
    title: 用户仓储
  - id: db
    resource: ../login_demo/src/db/database.ts
    title: 内存 users 表
  - id: password
    resource: ../login_demo/src/utils/password.ts
    title: 密码校验
  - id: token
    resource: ../login_demo/src/utils/token.ts
    title: token 签发与校验
  - id: middleware
    resource: ../login_demo/src/middleware/auth-middleware.ts
    title: 认证中间件
---

# 登录调用链

## HTTP 接口

| 方法 | 路径 | 说明 | 源码 |
|------|------|------|------|
| `POST` | `/api/login` | 登录入口 | `src/routes/login.ts` 的 `loginRouter.post("/login", ...)` |
| `GET` | `/api/profile` | 受保护接口，需 Bearer token | `src/routes/profile.ts` |
| `GET` | `/health` | 健康检查，返回 `{ ok: true }` | `src/app.ts` |

路由挂载：`src/app.ts` 中 `app.use("/api", loginRouter)` 与 `app.use("/api", profileRouter)`，因此完整路径分别是 `/api/login` 与 `/api/profile`。

## 登录成功链路

`POST /api/login` → `loginRouter` → `authService.login(email, password)` → `userRepository.findByEmail(email)` → `queryOne("users", { email })`（内存表查找）→ `verifyPassword(password, user.passwordHash)` → `issueAccessToken({ userId, role })` → 返回 `{ accessToken, user }`，路由返回 `200`。

## 登录失败分支（来源：src/routes/login.ts、src/services/auth-service.ts）

| 情况 | 触发点 | HTTP 状态 | 响应体 |
|------|--------|-----------|--------|
| 缺少 email 或 password | 路由层 | `400` | `{ error: "email and password are required" }` |
| 用户不存在 | Service 抛 `User not found` | `401` | `{ error: "User not found" }` |
| 密码错误 | Service 抛 `Invalid password` | `401` | `{ error: "Invalid password" }` |

路由层对 `authService.login` 的所有异常统一返回 `401` 并透出 `error.message`。

## 登录后鉴权链

`GET /api/profile` → `authMiddleware` → 从 `Authorization: Bearer <token>` 读取 → `parseAccessToken(token)` → 成功则写入 `response.locals.currentUser` → `next()` → 路由返回 `{ message: "Authenticated request", currentUser }`。

中间件失败分支：

- 请求头缺失或不是 `Bearer ` 开头 → `401 { error: "Missing bearer token" }`
- `parseAccessToken` 返回 `null` → `401 { error: "Invalid token" }`

token 的格式与“会话”位置见 [session-token.md](/session-token.md)；token 是否过期见 [token-expiry.md](/token-expiry.md)。
