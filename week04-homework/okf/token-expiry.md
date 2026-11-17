---
type: Reference
title: 令牌到期规则
description: login_demo 当前源码中没有实现任何令牌过期/到期规则，令牌永不过期。
tags: [token, expiry, security]
status: stable
generated: { by: pi-agent/deepseek-v4-pro, at: 2026-11-16T06:49:50Z }
sources:
  - id: token
    resource: ../login_demo/src/utils/token.ts
    title: token 签发与解析
  - id: user-types
    resource: ../login_demo/src/types/user.ts
    title: token 载荷类型
  - id: middleware
    resource: ../login_demo/src/middleware/auth-middleware.ts
    title: 认证中间件
---

# 令牌到期规则

## 核心结论（已从源码验证）

**当前源码没有实现任何到期/过期规则，访问令牌永不过期。**

这是从以下源码事实得出的结论，不是推断：

1. `AuthTokenPayload` 只有 `{ userId, role }` 两个字段（来源：src/types/user.ts），**没有** `exp`、`iat`、`nbf` 或任何时间字段。
2. `issueAccessToken` 只做 `base64url(JSON.stringify(payload))`，**不写入**任何有效期（来源：src/utils/token.ts）。
3. `parseAccessToken` 只校验 `demo-token.` 前缀并尝试 `JSON.parse`，**不检查**任何时间/过期条件（来源：src/utils/token.ts）。
4. `authMiddleware` 只校验前缀与解析成功，**不校验**令牌时效（来源：src/middleware/auth-middleware.ts）。

## 结论的影响

- 任何客户端拿到令牌后可以无限期使用，直到进程重启（令牌本身不含状态，重启后也不会“失效”，因为解析不依赖服务端状态）。
- 没有 TTL、滑动过期、固定过期时间等任何机制。
- 若未来需要到期规则，需要在 `AuthTokenPayload` 增加时间字段，并在 `parseAccessToken` 或 `authMiddleware` 中增加时间比对——这些目前都不存在。

## 待验证

- 无。到期规则“不存在”这一点已直接来自源码阅读；如需更多安全分析（如令牌可被离线伪造，因为无签名），属于超出源码事实的评估，本知识库不展开。

## 相关概念

- 会话/令牌模块位置：[session-token.md](/session-token.md)
- 登录鉴权链：[login-flow.md](/login-flow.md)
