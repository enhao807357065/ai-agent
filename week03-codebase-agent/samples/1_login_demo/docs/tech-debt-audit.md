# 技术债巡检报告

扫描范围：`**/*.ts`（跳过 node_modules、dist、.git）。
标记词：TODO、FIXME、Demo-only。

## 命中

| 位置 | 原文 | 风险判断 |
| --- | --- | --- |
| `src/utils/password.ts:1` | `// Demo-only password check: the stored hash is "hash:" + plain text password.` | 口令"哈希"是明文拼接，`verifyPassword` 只做字符串相等比较。任何人都能从库里的 `hash:<明文>` 直接还原密码，**上线前必须换成真实的自适应哈希（bcrypt/argon2）**。 |

## 没命中的部分

- 代码里没有 TODO 与 FIXME 标记。
- `src/utils/token.ts` 生成的令牌是 base64url 编码、没有签名，
  这一条不带标记词所以没被扫到，但风险等级不低于上面那条，建议一并纳入巡检词表。
