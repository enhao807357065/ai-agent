---
type: Reference
title: 会话/令牌模块位置
description: login_demo 中“会话”由无状态访问令牌实现，模块位于 src/utils/token.ts，无独立 session 模块。
tags: [session, token, auth]
status: stable
generated: { by: pi-agent/deepseek-v4-pro, at: 2026-11-16T06:49:50Z }
sources:
  - id: token
    resource: ../login_demo/src/utils/token.ts
    title: token 签发与解析
  - id: user-types
    resource: ../login_demo/src/types/user.ts
    title: 用户与 token 载荷类型
  - id: middleware
    resource: ../login_demo/src/middleware/auth-middleware.ts
    title: 认证中间件
---

# 会话/令牌模块位置

## 核心结论（已从源码验证）

login_demo **没有独立的 session 模块**，也没有服务端会话存储。“会话”即一个无状态访问令牌（access token）。相关代码分散在三个文件：

| 文件 | 职责 | 关键内容 |
|------|------|----------|
| `src/utils/token.ts` | 签发与解析 | `issueAccessToken(payload)`、`parseAccessToken(token)` |
| `src/types/user.ts` | 类型定义 | `AuthTokenPayload = { userId: string; role: "admin" \| "member" }` |
| `src/middleware/auth-middleware.ts` | 消费令牌 | 校验 `Authorization: Bearer <token>`，成功后写 `response.locals.currentUser` |

## 令牌格式（来源：src/utils/token.ts）

- `issueAccessToken` 将载荷 `JSON.stringify` 后做 `base64url` 编码，返回 `demo-token.<encoded>`。
- 载荷内容仅含 `{ userId, role }`（来源：src/utils/token.ts、src/types/user.ts）。
- `parseAccessToken` 先校验前缀 `demo-token.`，再解码并 `JSON.parse`；任何异常返回 `null`。

## 不存在的能力（已从源码确认）

- 无服务端 session 表/存储。
- 无 refresh token。
- 无令牌吊销/黑名单机制。
- 令牌载荷中无时间相关字段（无 `exp`/`iat`/`nbf`）——由此引出到期规则，见 [token-expiry.md](/token-expiry.md)。

## 相关概念

- 登录调用链与鉴权链：[login-flow.md](/login-flow.md)
- 到期规则：[token-expiry.md](/token-expiry.md)
