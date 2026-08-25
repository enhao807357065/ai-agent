"""逻辑模型限流器的开关与滑动窗口行为测试。"""

import asyncio

from app.services.rate_limiter import ModelRateLimit, ModelRateLimiter


def test_disabled_rate_limiter_is_a_side_effect_free_noop():
    limiter = ModelRateLimiter(enabled=False)
    limiter.configure("chat-default", ModelRateLimit(rpm=1, tpm=1, max_wait=0.0))

    assert asyncio.run(limiter.acquire_request("chat-default")) == 0.0
    limiter.report_tokens("chat-default", 999_999)

    assert limiter.enabled is False
    assert limiter._limiters == {}
    assert limiter.get_status("chat-default") == {
        "enabled": False,
        "configured": True,
    }
    assert limiter.get_all_status() == {
        "enabled": False,
        "models": {
            "chat-default": {"enabled": False, "configured": True},
        },
    }


def test_enabled_rate_limiter_creates_windows_and_records_tokens():
    limiter = ModelRateLimiter(enabled=True)
    limiter.configure("chat-default", ModelRateLimit(rpm=2, tpm=100, max_wait=0.0))

    assert asyncio.run(limiter.acquire_request("chat-default")) == 0.0
    limiter.report_tokens("chat-default", 7)

    assert limiter.get_status("chat-default") == {
        "enabled": True,
        "configured": True,
        "rpm": {"usage": 1, "limit": 2},
        "tpm": {"usage": 7, "limit": 100},
    }
