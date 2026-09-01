from dataclasses import dataclass, field

"""
toolcall runtime职责：
1.工具解析：根据tool_name从registry找到真正可执行的tool，模型输出只是文本协议，不能直接信任
2.参数校验：json schema/pydantic校验、类型转换、默认值处理
3.身份与权限：把当前用户的user_id、tenant_id、permissions注入执行上下文
4.审批控制：高风险操作中断，等待人审批准
5.调用执行：http、rpc、db、shell、mcp、python function等适配，屏蔽异构协议
6.可靠性：timeout、retry
7.结果规范化：将结果转成模型可消费的toolMessage，不能把任意内部对象/错误原样塞给模型
8.安全处理：脱敏、输出大小限制、敏感字段过滤、ssrf防护，工具输出同样可能是攻击输入
9.可观测：traceid、耗时、参数摘要、结果摘要、审计记录，调试agent不能只看最终回答
10.资源治理：并发上限、限流、预算、队列、租户隔离等，多agent下很容易把下游打爆

注意：
1. 不信任llm的toolcall：模型产生的工具参数，本质上属于不可信输入，必须做pydantic/json schema做强校验、拒绝未知字段，避免参数走私、权限判断必须以ExecutionContext为准，而不是tool arguments
2. 身份上下文必须由服务端注入：如果需要读取指定用户的场景，需要校验是否有读取target_user_id的权限
3. tool_output也不可信：网页内容、rag文档、邮件、数据库文本、第三方api返回值同样会反过来污染上下文。
    runtime不应该把原始工具内容毫无边界的塞给模型，至少应做：标记来源、限制单次输出长度和总token_budget、清洗html/二进制/不必要字段、对敏感字段脱敏
4. 读操作和写操作的治理等级必须不同：read-only：可自动执行，限流/脱敏。reversible-write：自动或低风险确认。irreversible-write：明确审批/二次确认。high-risk：强审批、审计、最小权限
5. timeout、retry、camel要按工具类型设计：不能为所有工具套同一套重试策略
6. 并行不是全部gather()：需要考虑最大并发、每个工具自己的并发上限、同一租户的配额、工具的依赖关系、返回顺序
7. tool_call_id
8. runtime必须可观测、可审计

auth_scope的本质是一个能力边界（capability boundary）
调用方被授予了“对某类资源执行某类动作”的权限，而不是拥有一个可以无限使用的身份。
常见格式常写成：
calendar:read
calendar:write
email:send
order:read
order:refund
customer:pii:read
database:query
"""

@dataclass(frozen=True)
class ExecutionContext:
    # 由服务端可信身份系统构造，不从llm参数中获取
    user_id: str        # 当前用户id
    tenant_id: str      # 租户id
    permission: frozenset[str]  # 上下文操作权限
    approved_call_ids: frozenset[str] = field(default_factory=frozenset)    # 已授权的tool_call_id？
    trace_id: str = ""
    order_service: object | None = None # 绑定的具体服务，测试用
