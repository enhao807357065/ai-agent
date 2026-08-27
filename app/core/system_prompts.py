"""
System Prompt 版本管理 — 基于 Jinja2 模板引擎

设计思路：
    - 每个 System Prompt 版本是一个 Jinja2 模板字符串（内联在代码中）
    - 模板通过变量支持动态内容（时间、用户名、语言偏好等）
    - 渲染使用 Jinja2 的 Environment + BaseLoader（从字符串加载）
    - API 暴露：前端通过 GET /v1/system-prompts 获取列表，渲染在后端完成

核心 Jinja2 知识点回顾：
    {{ expr }}       → 输出变量/表达式
    {% stmt %}       → 控制语句（if/for/block）
    {# comment #}   → 注释（不输出）
    trim_blocks      → 块标签后换行自动去掉
    lstrip_blocks    → 块标签前空白自动去掉
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from jinja2 import BaseLoader, StrictUndefined, Template
from jinja2.sandbox import ImmutableSandboxedEnvironment


# ============================================================
# Jinja2 Environment 配置
# ============================================================

class PromptSandbox(ImmutableSandboxedEnvironment):
    """Prompt 模板的最小权限 Jinja2 沙箱。

    模板源码只来自本模块维护的 PromptVersion；用户输入、RAG 文档等外部内容
    只能作为 render() 变量传入，绝不能传给 from_string() 作为模板源码。

    此层防御的是 SSTI 和意外暴露上下文对象，不替代 LLM Prompt Injection 的
    工具鉴权、数据隔离及输出校验。
    """

    def is_safe_attribute(self, obj: object, attr: str, value: object) -> bool:
        """Prompt 不需要访问对象属性，默认全部拒绝。"""
        return False

    def is_safe_callable(self, obj: object) -> bool:
        """Prompt 不需要调用上下文函数，默认全部拒绝。"""
        return False


jinja_env = PromptSandbox(
    loader=BaseLoader(),        # 从字符串加载，不需要文件系统
    undefined=StrictUndefined,  # 变量未定义 → 立即报错（不静默忽略）
    trim_blocks=True,           # 块标签后的第一个换行自动去掉
    lstrip_blocks=True,         # 块标签前的空白自动去掉
    autoescape=False,           # system prompt 不需要 HTML 转义
)


# ============================================================
# Prompt 模板内容（Jinja2 字符串）
# ============================================================

TEMPLATE_GENERAL_V1 = """\
你是一个有帮助的AI助手。

当前时间: {{ current_time }}
{% if user_name %}
用户称呼: {{ user_name }}
{% endif %}
请用中文回答用户的问题，回答要准确、简洁。
{% if extra_instructions %}

补充指令:
{{ extra_instructions }}
{% endif %}
"""

TEMPLATE_CODER_V1 = """\
你是一个专业的编程助手。

当前时间: {{ current_time }}
{% if user_name %}
用户称呼: {{ user_name }}
{% endif %}
请遵循以下原则：
1. 优先给出可运行的代码示例
2. 代码要有必要的注释
3. 解释核心思路，不要冗长废话
4. 如果问题有多种方案，列出各方案的优缺点
5. 指出潜在的坑和最佳实践

{% if preferred_language %}
默认使用 {{ preferred_language }}，除非用户指定其他语言。
{% else %}
默认使用 Python，除非用户指定其他语言。
{% endif %}
{% if extra_instructions %}

补充指令:
{{ extra_instructions }}
{% endif %}
"""

TEMPLATE_AGENT_ARCHITECT_V1 = """\
你是一个 AI Agent 系统架构师。

当前时间: {{ current_time }}
{% if user_name %}
用户称呼: {{ user_name }}
{% endif %}
你的专长包括：
- Agent Loop 设计（ReAct、Plan-and-Execute、LLMCompiler）
- 工具调用协议和沙箱设计
- 多 Agent 编排（Supervisor、Swarm、Hierarchical）
- 记忆系统（短期/长期/工作记忆）
- Streaming 和持久化策略

回答时请：
1. 先给出架构概览（可用 ASCII 图或列表）
2. 分析 trade-off（性能 vs 复杂度、一致性 vs 灵活性）
3. 给出你的推荐方案和理由
4. 如果涉及代码，给出关键接口/数据结构定义
{% if focus_area %}

当前重点关注领域: {{ focus_area }}
{% endif %}
{% if extra_instructions %}

补充指令:
{{ extra_instructions }}
{% endif %}
"""


# ============================================================
# Prompt 版本注册
# ============================================================

@dataclass(frozen=True)
class PromptVersion:
    """一个版本化的 System Prompt 元信息 + 模板"""
    id: str                     # 唯一标识（前端 select value）
    name: str                   # 显示名称
    description: str            # 简短描述
    template_source: str        # Jinja2 模板字符串
    variables: list[str] = field(default_factory=list)  # 接受的变量名
    tags: list[str] = field(default_factory=list)       # 分类标签

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "variables": self.variables,
            "tags": self.tags,
        }


# ============================================================
# 内置 3 个版本
# ============================================================

PROMPT_VERSIONS: list[PromptVersion] = [
    PromptVersion(
        id="general-v1",
        name="通用助手 v1",
        description="简洁通用的 AI 助手角色，适合日常对话和问答。",
        template_source=TEMPLATE_GENERAL_V1,
        variables=["current_time", "user_name", "extra_instructions"],
        tags=["通用", "中文"],
    ),
    PromptVersion(
        id="coder-v1",
        name="编程助手 v1",
        description="专注于代码编写和技术问题的助手，输出结构化且附带解释。",
        template_source=TEMPLATE_CODER_V1,
        variables=["current_time", "user_name", "preferred_language", "extra_instructions"],
        tags=["编程", "Python"],
    ),
    PromptVersion(
        id="agent-architect-v1",
        name="Agent 架构师 v1",
        description="AI Agent 系统设计专家，适合讨论架构、流程编排、工具设计等话题。",
        template_source=TEMPLATE_AGENT_ARCHITECT_V1,
        variables=["current_time", "user_name", "focus_area", "extra_instructions"],
        tags=["Agent", "架构", "系统设计"],
    ),
]

# 快速查找索引
_VERSION_MAP: dict[str, PromptVersion] = {v.id: v for v in PROMPT_VERSIONS}

# 预编译模板（启动时一次性编译，运行时直接渲染）
_TEMPLATE_CACHE: dict[str, Template] = {
    v.id: jinja_env.from_string(v.template_source)
    for v in PROMPT_VERSIONS
}

# 默认版本
DEFAULT_PROMPT_ID: str = "general-v1"


# ============================================================
# 核心 API
# ============================================================

def get_version(prompt_id: str) -> PromptVersion | None:
    """按 ID 获取 prompt 版本元信息"""
    return _VERSION_MAP.get(prompt_id)


def get_default_version() -> PromptVersion:
    """获取默认版本"""
    return _VERSION_MAP[DEFAULT_PROMPT_ID]


def list_versions(tag: str | None = None) -> list[PromptVersion]:
    """列出所有版本（可按 tag 过滤）"""
    if tag:
        return [v for v in PROMPT_VERSIONS if tag in v.tags]
    return PROMPT_VERSIONS


def render_prompt(
    prompt_id: str,
    *,
    user_name: str = "",
    extra_instructions: str = "",
    **kwargs,
) -> str:
    """
    渲染指定版本的 System Prompt（Jinja2 模板渲染）

    自动注入：
        - current_time: 当前时间（格式：2025-01-01 10:30）
        - user_name: 用户名（可选）
        - extra_instructions: 额外指令（可选）

    额外变量通过 **kwargs 传入（如 preferred_language, focus_area）

    Returns:
        渲染后的 prompt 字符串（已 strip）

    Raises:
        ValueError: prompt_id 不存在
        jinja2.UndefinedError: 必需变量未传入
    """
    if prompt_id not in _TEMPLATE_CACHE:
        raise ValueError(f"Unknown prompt version: {prompt_id}")

    template = _TEMPLATE_CACHE[prompt_id]

    # 构建渲染上下文
    context = {
        "current_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "user_name": user_name,
        "extra_instructions": extra_instructions,
        **kwargs,
    }

    return template.render(**context).strip()
