from bot.ratelimit import RateLimiter


def test_consume_within_limit():
    rl = RateLimiter(max_tokens=2, period_seconds=3600)
    assert rl.consume() is True
    assert rl.consume() is True
    assert rl.consume() is False


def test_remaining_and_retry_after():
    rl = RateLimiter(max_tokens=1, period_seconds=3600)
    assert rl.remaining == 1
    assert rl.retry_after == 0.0
    rl.consume()
    assert rl.remaining == 0
    assert rl.retry_after > 3590
