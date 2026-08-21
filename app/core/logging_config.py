import structlog
import logging
import sys
import os

"""
执行流程：
logger.info("request started", method="GET", path="/api")
         │
         ▼
┌─ processor 管线（按顺序执行）──────────────────┐
│                                                │
│  1. add_log_level    → 加 level="info"         │
│  2. add_logger_name  → 加 logger="__main__"    │
│  3. TimeStamper      → 加 timestamp="2026-..." │
│  4. format_exc_info  → 异常 → 可读 traceback   │
│  5. ConsoleRenderer / JSONRenderer → 最终输出   │
│                                                │
└────────────────────────────────────────────────┘
         │
         ▼
  开发环境（ConsoleRenderer）:
  2026-08-17 10:30:00 [info] request started  method=GET path=/api

  生产环境（JSONRenderer）:
  {"event":"request started","method":"GET","path":"/api","level":"info","timestamp":"2026-08-17T10:30:00Z"}

"""

def setup_logging(level: str = "INFO", is_test: bool = True, log_path: str = "./log/api.log"):
    """配置结构化日志"""

    handlers = [logging.StreamHandler(sys.stdout)]
    if log_path:
        log_dir = os.path.dirname(log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8", mode="w"))  # 文件输出

    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(message)s",
        handlers=handlers,
        # datefmt="%Y-%m-%d %H:%M:%S",
        # stream=sys.stdout,
    )

    # structlog 的核心思想：日志不是字符串，是结构化数据。通过 processor 管线逐步加工日志事件（dict），最终输出为人类可读的彩色文本（开发）或机器可解析的 JSON（生产）。
    processors = [
        structlog.contextvars.merge_contextvars,        # structlog.contextvars.bind_contextvars 要生效，processor 管线里必须有structlog.contextvars.merge_contextvars 这个processor，否则绑定的上下文变量根本不会被合并进日志事件。
        structlog.stdlib.add_log_level,                 # 添加日志级别
        structlog.stdlib.add_logger_name,               # 添加logger名
        structlog.processors.TimeStamper(fmt="iso"),    # 时间戳
        structlog.processors.format_exc_info,           # 异常信息格式化
        # structlog.dev.ConsoleRenderer() if level == "DEBUG"
        # else structlog.processors.JSONRenderer(),
    ]

    if is_test:
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,                              # 管线
        wrapper_class=structlog.stdlib.BoundLogger,         # 让 structlog 的 logger 支持 .info/.error 等标准方法
        context_class=dict,                                 # 上下文用普通 dict 存储
        logger_factory=structlog.stdlib.LoggerFactory(),    # 底层用标准 logging 做实际输出
        cache_logger_on_first_use=True,                     # 首次调用后缓存 logger 实例（性能优化）
    )