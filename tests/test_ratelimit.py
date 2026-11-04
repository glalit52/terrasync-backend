from terrasync.ratelimit import SlidingWindowLimiter


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_allows_up_to_limit_then_refuses_until_window_passes():
    clock = Clock()
    rl = SlidingWindowLimiter(3, 60, clock=clock)
    assert [rl.hit("a") for _ in range(3)] == [0, 0, 0]
    wait = rl.hit("a")
    assert 0 < wait <= 60
    assert rl.hit("b") == 0          # other clients unaffected
    clock.t = 61
    assert rl.hit("a") == 0


def test_key_table_is_bounded():
    rl = SlidingWindowLimiter(1, 60, max_keys=100)
    for i in range(1000):
        rl.hit(f"10.0.{i // 256}.{i % 256}")
    assert len(rl._hits) <= 100


def test_check_does_not_count_and_record_does():
    clock = Clock()
    rl = SlidingWindowLimiter(2, 60, clock=clock)
    for _ in range(10):
        assert rl.check("a") == 0       # successes never call record
    rl.record("a")
    rl.record("a")
    assert rl.check("a") > 0
    clock.t = 60
    assert rl.check("a") == 0
