# 技术债巡检报告

任务类型：debt_audit（技术债巡检 · 纯配置新增类）
扫描时间：本轮巡检
扫描范围：工作区全部文本文件（18 个文件 / 75.6 KB），核心逻辑在 `src/**/*.ts`（11 个文件 / 5.5 KB）；
`package-lock.json`、`README.md`、`package.json` 只作为命中的噪声来源，不计入债务。

检索词表：`TODO` `FIXME` `XXX` `HACK` `DEMO` / `demo-only` / `Demo-only` / `demo only` / `临时` / `待办` / `先这样` / `暂时`
（扩展覆盖：`demo` `placeholder` `workaround` `hardcod` `deprecated`）。
两轮检索：全工作区一轮命中 34 处（含本报告自身与 `artifacts/`、锁文件噪声），
限定 `src/` 复核一轮命中 9 处，命中行号与本表逐条比对一致。

## 一、结论摘要

- 代码侧（`src/`，11 个文件）检索命中 9 处，落在 5 个文件：`db/database.ts`、
  `routes/login.ts`、`server.ts`、`utils/password.ts`、`utils/token.ts`。
  去掉纯命名噪声（`login.ts:6` 注释措辞、`server.ts:7` 日志里的 `login_demo`）后，
  **真正需要跟踪的债务 4 条**；另加 1 条手工复核发现的（表内第 3 条，不带标记词）。
- 代码里**没有任何 `TODO` / `FIXME` / `XXX` / `HACK` 标记**。
  这个仓库的债全部是"Demo-only 残留"，即功能性代码被明文写成演示实现，
  而不是留下待办注释——这类债比 TODO 更危险，因为没有任何"以后要改"的提示。
- 4 条债中 3 条是认证安全问题（明文口令、无签名令牌、错误信息泄漏），
  **任一单独存在都足以阻断上线**，且它们组合起来构成一条完整的绕过链，见第三节。

## 二、命中明细

| # | 位置 | 原文 | 风险判断 | 等级 |
| --- | --- | --- | --- | --- |
| 1 | `src/utils/password.ts:1` | `// Demo-only password check: the stored hash is "hash:" + plain text password.` | 注释已自认是演示实现。对应实现 `password.ts:6` 为 `passwordHash === \`hash:${plainTextPassword}\``，即"哈希"就是明文前缀拼接，`verifyPassword` 只做字符串相等。库里的 `hash:demo123` 可被直接还原为口令。 | **高** |
| 2 | `src/db/database.ts:8-10`、`:16` | `email: "demo@example.com"` / `passwordHash: "hash:demo123"` / `displayName: "Demo User"` / `passwordHash: "hash:admin123"` | 与第 1 条同源的持久化侧：种子数据把口令以 `hash:<明文>` 形式落库，其中 `u_1002` 是 `role: "admin"`。本轮检索的命中词是 `demo`，但真正的隐患是明文口令与硬编码的 admin 凭据。 | **高** |
| 3 | `src/utils/token.ts:5`、`:9`、`:13` | `return \`demo-token.${encoded}\`;` / `if (!token.startsWith("demo-token.")) {` | 令牌是 `demo-token.` + base64url(payload) 的**无签名**编码，`parseAccessToken` 仅校验前缀后即反序列化 JSON。任何人可自己 base64url 编码 `{"userId":"u_1002","role":"admin"}` 伪造管理员令牌。这一条**不带标记词**，靠词表抓不到，是按登录链路手工复核发现的。 | **高** |
| 4 | `src/routes/login.ts:22-24` | `response.status(401).json({ error: error instanceof Error ? error.message : "Login failed" });` | 把 `auth-service.ts:11` 的 `"User not found"` 与 `:17` 的 `"Invalid password"` 原样回给客户端，构成账号枚举（可批量探测哪些邮箱已注册）。属于演示代码直接透传内部错误。 | 中 |

说明：`src/routes/login.ts:6` 的 `// Login entry point for the demo:` 与
`src/server.ts:7` 的 `login_demo` 日志、`package.json`/`README.md` 的项目名同样含 `demo`，
但只是命名与注释措辞，不构成缺陷，列在这里以免下轮巡检重复误报。

## 三、组合风险：一条完整的权限绕过链

第 1、3 条不是两个独立缺陷，串起来是实际可利用的路径：

1. 攻击者从库中或种子数据拿到 `hash:demo123`，直接读到明文口令口令（债 1）；
2. 即使用户口令被改强，攻击者也不需要口令：`auth-service.ts:20-23` 签发的令牌
   payload 只有 `userId` 与 `role`，而 `token.ts` 不签名，直接构造令牌即可（债 3）；
3. `auth-middleware.ts:24` 把 `parseAccessToken` 的返回值**原样**写入
   `response.locals.currentUser` 并 `next()`，全程不做签名校验、不做过期校验；
4. `src/routes/profile.ts:6-11` 直接把这个 `currentUser` 回显为响应体，
   于是伪造的 `role: "admin"` 会被系统当成已认证管理员。

因此修复优先级应为债 3 ≥ 债 1 > 债 2 > 债 4，且债 3、债 1 必须在同一批次处理完，
只修其中一条并不能阻断链路。

## 四、建议的修复方向（本轮未改动任何源码）

- 债 1 / 债 2：改用 bcrypt 或 argon2 的自适应哈希，`verifyPassword` 换成库函数的定时安全比较；
  种子数据里的 `hash:demo123` / `hash:admin123` 必须删除或替换为真实哈希，禁止硬编码管理员凭据。
- 债 3：换用签名 JWT（HS256/RS256）或服务端会话，`parseAccessToken` 必须验签并校验 `exp`；
  `auth-middleware` 只信任验签通过的 payload。
- 债 4：对外统一返回 `"Invalid credentials"`，账号是否存在只记服务端日志。

## 五、没命中的部分 / 巡检本身的局限

- 代码中不存在 `TODO`、`FIXME`、`XXX`、`HACK` 标记，"待办/临时/先这样/暂时"类中文注释亦为零。
  也就是说本仓库的技术债**完全不靠注释标记暴露**，纯词表扫描必然漏掉债 3 这类问题。
  建议把"无签名令牌""明文口令前缀"这类模式词纳入常规巡检词表。
- 本次只做静态阅读、检索与一次 `git_status` 复查，**未运行 `run_test`** 验证上述行为（任务要求为一次检索、一份报告、
  一次 `git_status` 复查，不改源码、不跑改动验证）。第三节的绕过链是依据
  `token.ts`、`auth-middleware.ts`、`profile.ts`、`auth-service.ts` 的实际代码推导，
  建议后续补一条测试：构造未签名令牌访问 `/api/profile`，断言应返回 401。
- 未覆盖项：`src/db/database.ts:22-28` 的 `getTable` 用模拟数据替代真实数据库访问，
  `queryOne` 以 `Object.entries` 全等比较做过滤，作为演示脚手架本身不算缺陷，
  但接入真实存储前需整体替换，这里只作记录。
