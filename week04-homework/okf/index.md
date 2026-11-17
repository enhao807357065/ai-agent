---
okf_version: "0.2"
---

# login_demo 知识库

本 bundle 记录 `login_demo` 项目的代码库知识，重点覆盖会话模块位置、到期规则和测试入口。

## 概览

- [项目概览](overview.md) - TypeScript/Express 演示项目，分层结构与 npm 脚本
- [登录调用链](login-flow.md) - `POST /api/login` 及 `/api/profile` 鉴权链

## 重点概念

- [会话/令牌模块位置](session-token.md) - 会话由无状态令牌实现，核心在 `src/utils/token.ts`
- [令牌到期规则](token-expiry.md) - 当前源码没有实现任何到期规则，令牌永不过期
- [测试入口与执行记录](testing.md) - `npm test` 入口、5 个测试文件、2026-11-16 执行结果

## 更新历史

- [Bundle 更新日志](log.md)
