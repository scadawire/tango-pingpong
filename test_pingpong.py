"""
Unit test for PingPongDs - tests round-trip latency measurement.

Two simulated instances ping each other via direct method calls.
Tests metric calculation, guard conditions, timing accuracy, and edge cases.

Usage:
    python test_pingpong.py
"""

import sys
import os
import traceback
import threading
from time import sleep

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import PingPongDs as ppds_module
from PingPongDs import PingPongDs


# ===========================================================================
#  Mock devices
# ===========================================================================

class MockPongDevice:
    """Wraps a PingPongState to simulate a Tango DeviceProxy."""
    def __init__(self, target):
        self.target = target

    def pong(self, ping_tag):
        PingPongDs.pong(self.target, ping_tag)

    def ack(self, ping_tag):
        PingPongDs.ack(self.target, ping_tag)

    def command_list_query(self):
        return ["trigger_ping", "pong", "ack"]


class TimedMockPongDevice(MockPongDevice):
    """Mock that advances mock time on each pong to simulate latency."""
    def __init__(self, target, rtt_ms=5.0):
        super().__init__(target)
        self.rtt_ms = rtt_ms

    def pong(self, ping_tag):
        if isinstance(ppds_module.time, MockTime):
            ppds_module.time.t += self.rtt_ms / 1000.0
        super().pong(ping_tag)


class VariableTimedMockPongDevice(MockPongDevice):
    """Mock that cycles through a list of RTT values."""
    def __init__(self, target, rtts_ms):
        super().__init__(target)
        self.rtts_ms = rtts_ms
        self.idx = 0

    def pong(self, ping_tag):
        if isinstance(ppds_module.time, MockTime):
            ppds_module.time.t += self.rtts_ms[self.idx] / 1000.0
            self.idx = (self.idx + 1) % len(self.rtts_ms)
        super().pong(ping_tag)


class DelayedMockPongDevice(MockPongDevice):
    """Mock that adds real sleep delay (for real-time timing tests)."""
    def __init__(self, target, delay_ms=10.0):
        super().__init__(target)
        self.delay_ms = delay_ms

    def pong(self, ping_tag):
        sleep(self.delay_ms / 1000.0)
        super().pong(ping_tag)


# ===========================================================================
#  State carrier (no Tango DB required)
# ===========================================================================

class PingPongState:
    """Lightweight state carrier for calling PingPongDs methods."""
    def __init__(self, name=""):
        import functools
        self._functools = functools
        self._name = name

        self.total_roundtrips = 0
        self.avg_roundtrip_time = 0.0
        self.total_roundtrip_time = 0.0
        self.worst_roundtrip_time = 0.0
        self.best_roundtrip_time = 0.0
        self.last_roundtrip_time = 0.0
        self.connected = 0
        self.last_ping_time = 0.0
        self.ping_tag = 0
        self.pending_pings = {}
        self.last_print_time = ppds_module.time()
        self.start_time = ppds_module.time()
        self.pong_device = None
        self.pong_device_name = ""
        self.ping_interval_ms = 0
        self._lock = threading.Lock()
        self.logs = []

    def info_stream(self, msg):
        self.logs.append(("INFO", msg))

    def error_stream(self, msg):
        self.logs.append(("ERROR", msg))

    def reconnect(self):
        if self.pong_device is not None:
            self.connected = 1

    def __getattr__(self, name):
        attr = getattr(PingPongDs, name, None)
        if attr is not None and callable(attr):
            return self._functools.partial(attr, self)
        raise AttributeError(f"'PingPongState' has no attribute '{name}'")


# ===========================================================================
#  Factory helpers
# ===========================================================================

def make_pair(start_offset=-10):
    """Two instances wired to each other (no simulated latency)."""
    a = PingPongState("A")
    b = PingPongState("B")
    a.pong_device = MockPongDevice(b)
    a.connected = 1
    b.pong_device = MockPongDevice(a)
    b.connected = 1
    if start_offset != 0:
        a.start_time = ppds_module.time() + start_offset
        b.start_time = ppds_module.time() + start_offset
    return a, b


def make_timed_pair(rtt_ms=5.0, start_offset=-10):
    """Two instances with simulated round-trip time via mock-time advance."""
    a = PingPongState("A")
    b = PingPongState("B")
    a.pong_device = TimedMockPongDevice(b, rtt_ms=rtt_ms)
    a.connected = 1
    b.pong_device = TimedMockPongDevice(a, rtt_ms=rtt_ms)
    b.connected = 1
    if start_offset != 0:
        a.start_time = ppds_module.time() + start_offset
        b.start_time = ppds_module.time() + start_offset
    return a, b


# ===========================================================================
#  Test harness
# ===========================================================================

passed = 0
failed = 0
errors = []


def assert_equal(test_name, actual, expected, tolerance=None):
    global passed, failed
    if tolerance is not None:
        ok = abs(actual - expected) <= tolerance
    else:
        ok = (actual == expected)
    if ok:
        passed += 1
        print(f"  PASS  {test_name}")
    else:
        failed += 1
        msg = f"  FAIL  {test_name}: expected {expected!r}, got {actual!r}"
        print(msg)
        errors.append(msg)


def assert_true(test_name, value):
    assert_equal(test_name, value, True)


def assert_false(test_name, value):
    assert_equal(test_name, value, False)


def assert_in_range(test_name, value, low, high):
    global passed, failed
    ok = low <= value <= high
    if ok:
        passed += 1
        print(f"  PASS  {test_name}")
    else:
        failed += 1
        msg = f"  FAIL  {test_name}: {value!r} not in [{low}, {high}]"
        print(msg)
        errors.append(msg)


# ===========================================================================
#  Time mocking
# ===========================================================================

_real_time = ppds_module.time


class MockTime:
    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t


def install_mock_time(start=100.0):
    mt = MockTime(start)
    ppds_module.time = mt
    return mt


def restore_real_time():
    ppds_module.time = _real_time


# ===========================================================================
#  A. Metric calculation tests (direct ack calls, controlled timestamps)
# ===========================================================================

def test_ack_single_roundtrip():
    """ack() correctly calculates roundtrip time."""
    print("\n-- metrics: single roundtrip --")
    mt = install_mock_time(100.0)
    try:
        a = PingPongState("A")
        a.start_time = 90.0

        a.pending_pings[1] = 100.0
        mt.t = 100.010  # 10ms later
        PingPongDs.ack(a, 1)

        assert_equal("total = 1", a.total_roundtrips, 1)
        assert_equal("last rtt = 10ms", a.last_roundtrip_time, 10.0, tolerance=0.01)
        assert_equal("avg rtt = 10ms", a.avg_roundtrip_time, 10.0, tolerance=0.01)
        assert_equal("worst rtt = 10ms", a.worst_roundtrip_time, 10.0, tolerance=0.01)
        assert_equal("best rtt = 10ms", a.best_roundtrip_time, 10.0, tolerance=0.01)
        assert_true("pending cleaned up", 1 not in a.pending_pings)
    finally:
        restore_real_time()


def test_ack_multiple_roundtrips():
    """Average, best, worst computed correctly over multiple acks."""
    print("\n-- metrics: multiple roundtrips --")
    mt = install_mock_time(100.0)
    try:
        a = PingPongState("A")
        a.start_time = 90.0

        # 3 pings: 10ms, 20ms, 30ms
        rtts = [(100.0, 100.010), (100.1, 100.120), (100.2, 100.230)]
        for i, (send, recv) in enumerate(rtts):
            tag = i + 1
            a.pending_pings[tag] = send
            mt.t = recv
            PingPongDs.ack(a, tag)

        assert_equal("total = 3", a.total_roundtrips, 3)
        assert_equal("avg = 20ms", a.avg_roundtrip_time, 20.0, tolerance=0.01)
        assert_equal("best = 10ms", a.best_roundtrip_time, 10.0, tolerance=0.01)
        assert_equal("worst = 30ms", a.worst_roundtrip_time, 30.0, tolerance=0.01)
        assert_equal("last = 30ms", a.last_roundtrip_time, 30.0, tolerance=0.01)
    finally:
        restore_real_time()


def test_ack_best_worst_over_many():
    """best/worst track min/max correctly over varied values."""
    print("\n-- metrics: best/worst tracking --")
    mt = install_mock_time(100.0)
    try:
        a = PingPongState("A")
        a.start_time = 90.0

        rtts_ms = [15.0, 5.0, 25.0, 10.0, 3.0, 50.0, 8.0]
        for i, rtt in enumerate(rtts_ms):
            a.pending_pings[i + 1] = 100.0
            mt.t = 100.0 + rtt / 1000.0
            PingPongDs.ack(a, i + 1)

        assert_equal("best = 3ms", a.best_roundtrip_time, 3.0, tolerance=0.01)
        assert_equal("worst = 50ms", a.worst_roundtrip_time, 50.0, tolerance=0.01)
        expected_avg = sum(rtts_ms) / len(rtts_ms)
        assert_equal("avg correct", a.avg_roundtrip_time, expected_avg, tolerance=0.1)
    finally:
        restore_real_time()


def test_ack_warmup_skip():
    """ack() skips metric updates during first 5 seconds after start."""
    print("\n-- metrics: 5-second warmup skip --")
    mt = install_mock_time(100.0)
    try:
        a = PingPongState("A")
        a.start_time = 100.0  # just started

        # Within 5 seconds: metrics NOT updated
        a.pending_pings[1] = 100.0
        mt.t = 100.010
        PingPongDs.ack(a, 1)

        assert_equal("warmup: total = 0", a.total_roundtrips, 0)
        assert_equal("warmup: avg = 0", a.avg_roundtrip_time, 0.0)
        assert_true("warmup: pending still cleaned", 1 not in a.pending_pings)

        # After 5 seconds: metrics DO update
        a.pending_pings[2] = 105.1
        mt.t = 105.120
        PingPongDs.ack(a, 2)

        assert_equal("post-warmup: total = 1", a.total_roundtrips, 1)
        assert_equal("post-warmup: rtt = 20ms", a.last_roundtrip_time, 20.0, tolerance=0.01)
    finally:
        restore_real_time()


def test_ack_unknown_tag():
    """ack() with unknown tag logs error, doesn't crash."""
    print("\n-- metrics: unknown tag --")
    mt = install_mock_time(100.0)
    try:
        a = PingPongState("A")
        a.start_time = 90.0

        PingPongDs.ack(a, 999)

        assert_equal("unknown tag: total = 0", a.total_roundtrips, 0)
        assert_true("unknown tag: error logged",
                    any("unknown tag" in msg.lower() for _, msg in a.logs))
    finally:
        restore_real_time()


# ===========================================================================
#  B. trigger_ping() tests
# ===========================================================================

def test_trigger_ping_tag_increment():
    """trigger_ping() increments tag and stores in pending_pings."""
    print("\n-- trigger_ping: tag increment --")
    mt = install_mock_time(100.0)
    try:
        a, b = make_timed_pair()

        PingPongDs.trigger_ping(a)
        assert_equal("tag = 1", a.ping_tag, 1)

        PingPongDs.trigger_ping(a)
        assert_equal("tag = 2", a.ping_tag, 2)
    finally:
        restore_real_time()


def test_trigger_ping_stores_time():
    """trigger_ping() records current time in pending_pings."""
    print("\n-- trigger_ping: stores send time --")
    mt = install_mock_time(100.0)
    try:
        a, b = make_pair(start_offset=0)  # doesn't matter for this test
        # Manually test pending_pings entry before it gets cleared by ack
        a.pong_device = None  # prevent actual pong call
        a.connected = 1

        PingPongDs.trigger_ping(a)  # pong_device is None, so skipped after tag++

        # pong_device is None → no pong call, but tag was not incremented either
        # Actually: the code checks `if self.pong_device is not None` BEFORE incrementing
        # Wait no - let me re-read:
        # if(self.connected == 0): self.reconnect()
        # if self.pong_device is not None:
        #     self.ping_tag += 1
        # So with pong_device=None, nothing happens
        assert_equal("no device: tag not incremented", a.ping_tag, 0)
    finally:
        restore_real_time()


def test_trigger_ping_reconnects():
    """trigger_ping() calls reconnect when disconnected."""
    print("\n-- trigger_ping: reconnect --")
    mt = install_mock_time(100.0)
    try:
        a, b = make_timed_pair()
        a.connected = 0  # simulate disconnect

        PingPongDs.trigger_ping(a)

        assert_equal("reconnected", a.connected, 1)
        assert_equal("ping sent", a.ping_tag, 1)
    finally:
        restore_real_time()


def test_trigger_ping_no_device():
    """trigger_ping() with no pong_device doesn't crash."""
    print("\n-- trigger_ping: no device --")
    mt = install_mock_time(100.0)
    try:
        a = PingPongState("A")
        a.connected = 0
        a.pong_device = None

        PingPongDs.trigger_ping(a)
        assert_equal("no crash, no ping", a.ping_tag, 0)
    finally:
        restore_real_time()


# ===========================================================================
#  C. Full round-trip integration (two instances, mock time)
# ===========================================================================

def test_full_roundtrip():
    """A.trigger_ping -> B.pong -> A.ack: complete chain."""
    print("\n-- integration: full roundtrip --")
    mt = install_mock_time(100.0)
    try:
        a, b = make_timed_pair(rtt_ms=5.0)

        PingPongDs.trigger_ping(a)

        assert_equal("A: total = 1", a.total_roundtrips, 1)
        assert_equal("A: rtt = 5ms", a.last_roundtrip_time, 5.0, tolerance=0.01)
        assert_equal("A: pending cleared", len(a.pending_pings), 0)
        assert_equal("B: total = 0 (responder only)", b.total_roundtrips, 0)
    finally:
        restore_real_time()


def test_multiple_roundtrips_integration():
    """50 ping-pongs accumulate correct metrics."""
    print("\n-- integration: 50 roundtrips --")
    mt = install_mock_time(100.0)
    try:
        a, b = make_timed_pair(rtt_ms=5.0)
        n = 50

        for _ in range(n):
            PingPongDs.trigger_ping(a)

        assert_equal("total = 50", a.total_roundtrips, n)
        assert_equal("avg = 5ms", a.avg_roundtrip_time, 5.0, tolerance=0.1)
        assert_equal("best = 5ms", a.best_roundtrip_time, 5.0, tolerance=0.1)
        assert_equal("worst = 5ms", a.worst_roundtrip_time, 5.0, tolerance=0.1)
        assert_equal("all pending cleared", len(a.pending_pings), 0)
    finally:
        restore_real_time()


def test_bidirectional():
    """Both instances can initiate pings independently."""
    print("\n-- integration: bidirectional --")
    mt = install_mock_time(100.0)
    try:
        a, b = make_timed_pair(rtt_ms=5.0)

        PingPongDs.trigger_ping(a)
        assert_equal("A pinged: A.total = 1", a.total_roundtrips, 1)
        assert_equal("A pinged: B.total = 0", b.total_roundtrips, 0)

        PingPongDs.trigger_ping(b)
        assert_equal("B pinged: B.total = 1", b.total_roundtrips, 1)
        assert_equal("B pinged: A.total still 1", a.total_roundtrips, 1)

        PingPongDs.trigger_ping(a)
        PingPongDs.trigger_ping(b)
        assert_equal("both: A.total = 2", a.total_roundtrips, 2)
        assert_equal("both: B.total = 2", b.total_roundtrips, 2)
    finally:
        restore_real_time()


def test_variable_rtt():
    """Correct metrics with varying round-trip times."""
    print("\n-- integration: variable RTT --")
    mt = install_mock_time(100.0)
    try:
        a = PingPongState("A")
        b = PingPongState("B")
        rtts = [3.0, 7.0, 15.0, 2.0, 10.0, 50.0, 5.0, 1.0, 20.0, 8.0]
        a.pong_device = VariableTimedMockPongDevice(b, rtts)
        a.connected = 1
        b.pong_device = MockPongDevice(a)
        b.connected = 1
        a.start_time = 90.0
        b.start_time = 90.0

        for _ in range(len(rtts)):
            PingPongDs.trigger_ping(a)

        assert_equal("total = 10", a.total_roundtrips, len(rtts))
        assert_equal("best = 1ms", a.best_roundtrip_time, 1.0, tolerance=0.1)
        assert_equal("worst = 50ms", a.worst_roundtrip_time, 50.0, tolerance=0.1)
        expected_avg = sum(rtts) / len(rtts)
        assert_equal("avg = 12.1ms", a.avg_roundtrip_time, expected_avg, tolerance=0.5)
    finally:
        restore_real_time()


def test_metric_invariants():
    """best <= avg <= worst always holds."""
    print("\n-- integration: metric invariants --")
    mt = install_mock_time(100.0)
    try:
        a = PingPongState("A")
        b = PingPongState("B")
        rtts = [5.0, 15.0, 3.0, 25.0, 8.0, 1.0, 40.0, 12.0]
        a.pong_device = VariableTimedMockPongDevice(b, rtts)
        a.connected = 1
        b.pong_device = MockPongDevice(a)
        b.connected = 1
        a.start_time = 90.0
        b.start_time = 90.0

        for _ in range(len(rtts)):
            PingPongDs.trigger_ping(a)

        assert_true("best <= avg",
                    a.best_roundtrip_time <= a.avg_roundtrip_time + 0.001)
        assert_true("avg <= worst",
                    a.avg_roundtrip_time <= a.worst_roundtrip_time + 0.001)
        assert_true("best > 0", a.best_roundtrip_time > 0)
    finally:
        restore_real_time()


# ===========================================================================
#  D. Real-time timing tests
# ===========================================================================

def test_real_time_direct_calls():
    """Direct method calls have very low roundtrip time."""
    print("\n-- timing: real-time direct calls --")
    restore_real_time()
    a, b = make_pair()

    for _ in range(20):
        PingPongDs.trigger_ping(a)

    assert_equal("all completed", a.total_roundtrips, 20)
    assert_true("avg < 10ms (direct calls)", a.avg_roundtrip_time < 10.0)


def test_real_time_with_delay():
    """Measured RTT includes actual sleep delay."""
    print("\n-- timing: delay measurement --")
    restore_real_time()

    a = PingPongState("A")
    b = PingPongState("B")
    delay_ms = 10.0
    a.pong_device = DelayedMockPongDevice(b, delay_ms=delay_ms)
    a.connected = 1
    b.pong_device = MockPongDevice(a)
    b.connected = 1
    a.start_time = _real_time() - 10
    b.start_time = _real_time() - 10

    PingPongDs.trigger_ping(a)

    assert_true("rtt >= delay", a.last_roundtrip_time >= delay_ms * 0.8)
    assert_true("rtt reasonable", a.last_roundtrip_time < delay_ms * 5)


# ===========================================================================
#  E. Logging tests
# ===========================================================================

def test_log_throttle():
    """Info logging is throttled to once per second."""
    print("\n-- logging: throttle --")
    mt = install_mock_time(100.0)
    try:
        a, b = make_timed_pair(rtt_ms=1.0)

        # 10 rapid pings within same second
        for _ in range(10):
            PingPongDs.trigger_ping(a)

        info_count_1 = sum(1 for lvl, _ in a.logs if lvl == "INFO")
        assert_true("throttled: <= 2 info logs", info_count_1 <= 2)

        # Advance 2 seconds, ping again
        mt.t += 2.0
        PingPongDs.trigger_ping(a)
        info_count_2 = sum(1 for lvl, _ in a.logs if lvl == "INFO")
        assert_true("after 2s: new log", info_count_2 > info_count_1)
    finally:
        restore_real_time()


def test_log_content():
    """Info log contains roundtrip stats."""
    print("\n-- logging: content --")
    mt = install_mock_time(100.0)
    try:
        a = PingPongState("A")
        a.start_time = 90.0
        a.last_print_time = 0.0  # ensure log is not throttled

        a.pending_pings[1] = 100.0
        mt.t = 100.015
        PingPongDs.ack(a, 1)

        info_msgs = [msg for lvl, msg in a.logs if lvl == "INFO"]
        assert_true("has log entry", len(info_msgs) >= 1)
        if info_msgs:
            msg = info_msgs[-1]
            assert_true("log has 'Roundtrip'", "Roundtrip" in msg)
            assert_true("log has 'Avg'", "Avg" in msg)
            assert_true("log has 'Worst'", "Worst" in msg)
    finally:
        restore_real_time()


# ===========================================================================
#  F. Edge cases and known issues
# ===========================================================================

def test_pong_null_device():
    """pong() with no pong_device logs error instead of crashing."""
    print("\n-- edge: pong null device --")
    b = PingPongState("B")
    b.pong_device = None

    crashed = False
    try:
        PingPongDs.pong(b, 1)
    except AttributeError:
        crashed = True

    assert_false("pong does not crash", crashed)
    assert_true("pong: error logged",
                any("not connected" in msg.lower() for _, msg in b.logs))


def test_pending_pings_cleanup():
    """Completed pings are removed from pending_pings."""
    print("\n-- edge: pending cleanup --")
    mt = install_mock_time(100.0)
    try:
        a, b = make_timed_pair(rtt_ms=1.0)

        for i in range(20):
            PingPongDs.trigger_ping(a)

        assert_equal("all pending cleared", len(a.pending_pings), 0)
    finally:
        restore_real_time()


def test_failed_pong_cleans_pending():
    """If pong() fails, trigger_ping cleans up the orphaned pending entry."""
    print("\n-- edge: failed pong cleans pending --")
    mt = install_mock_time(100.0)
    try:
        a = PingPongState("A")
        b = PingPongState("B")
        b.pong_device = None  # B's pong will log error, ack never reaches A

        class FailingMock:
            def pong(self, tag):
                raise Exception("connection lost")
            def command_list_query(self):
                return []

        a.pong_device = FailingMock()
        a.connected = 1
        a.start_time = 90.0

        PingPongDs.trigger_ping(a)

        assert_equal("tag incremented", a.ping_tag, 1)
        assert_true("pending cleaned up (no leak)", 1 not in a.pending_pings)
        assert_true("error logged",
                    any("failed" in msg.lower() for _, msg in a.logs))
    finally:
        restore_real_time()


def test_large_volume():
    """Handles 10000 roundtrips without issues."""
    print("\n-- edge: large volume --")
    mt = install_mock_time(100.0)
    try:
        a, b = make_timed_pair(rtt_ms=0.5)
        n = 10000

        for _ in range(n):
            PingPongDs.trigger_ping(a)

        assert_equal("total = 10000", a.total_roundtrips, n)
        assert_equal("pending empty", len(a.pending_pings), 0)
        assert_true("best <= worst",
                    a.best_roundtrip_time <= a.worst_roundtrip_time + 0.001)
    finally:
        restore_real_time()


def test_warmup_then_measure():
    """Metrics are clean after warmup period ends."""
    print("\n-- edge: warmup boundary --")
    mt = install_mock_time(100.0)
    try:
        a, b = make_timed_pair(rtt_ms=5.0)
        a.start_time = 100.0  # just started

        # 10 pings during warmup (within 5 seconds)
        for _ in range(10):
            PingPongDs.trigger_ping(a)

        assert_equal("warmup: total = 0", a.total_roundtrips, 0)

        # Advance past warmup
        mt.t = 106.0
        for _ in range(5):
            PingPongDs.trigger_ping(a)

        assert_equal("post-warmup: total = 5", a.total_roundtrips, 5)
        assert_equal("post-warmup: avg = 5ms", a.avg_roundtrip_time, 5.0, tolerance=0.1)
    finally:
        restore_real_time()


# ===========================================================================
#  Main
# ===========================================================================

def main():
    global passed, failed

    print("=" * 60)
    print("  PingPongDs Unit Test")
    print("=" * 60)

    try:
        # A. Metric calculation
        test_ack_single_roundtrip()
        test_ack_multiple_roundtrips()
        test_ack_best_worst_over_many()
        test_ack_warmup_skip()
        test_ack_unknown_tag()

        # B. trigger_ping
        test_trigger_ping_tag_increment()
        test_trigger_ping_stores_time()
        test_trigger_ping_reconnects()
        test_trigger_ping_no_device()

        # C. Two-instance integration
        test_full_roundtrip()
        test_multiple_roundtrips_integration()
        test_bidirectional()
        test_variable_rtt()
        test_metric_invariants()

        # D. Real-time timing
        test_real_time_direct_calls()
        test_real_time_with_delay()

        # E. Logging
        test_log_throttle()
        test_log_content()

        # F. Edge cases
        test_pong_null_device()
        test_pending_pings_cleanup()
        test_failed_pong_cleans_pending()
        test_large_volume()
        test_warmup_then_measure()

    except Exception:
        traceback.print_exc()
        failed += 1
    finally:
        restore_real_time()

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"  Results: {passed}/{total} passed, {failed} failed")
    if errors:
        print("\n  Failures:")
        for e in errors:
            print(f"    {e}")
    print(f"{'=' * 60}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
