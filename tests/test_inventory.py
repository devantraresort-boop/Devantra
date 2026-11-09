from datetime import date, timedelta
import random

from backend.app.inventory import occupancy_peak


def test_checkout_day_can_be_reused():
    start = date(2030, 1, 1)
    middle, end = start + timedelta(days=1), start + timedelta(days=2)
    assert occupancy_peak([(start, middle), (middle, end)], start, end) == 1


def test_intersecting_stays_and_window_boundaries():
    start, end = date(2030, 1, 5), date(2030, 1, 10)
    stays = [(date(2030, 1, 1), start), (end, date(2030, 1, 15)),
             (start, end), (start + timedelta(days=1), end + timedelta(days=2))]
    assert occupancy_peak(stays, start, end) == 2
    assert occupancy_peak([], start, end) == 0


def test_sweep_matches_independent_nightly_counter():
    random.seed(42)
    origin = date(2030, 1, 1)
    for _ in range(300):
        spans = []
        for _ in range(random.randrange(100)):
            begin = random.randrange(40)
            spans.append((origin + timedelta(days=begin), origin + timedelta(days=begin + random.randrange(1, 15))))
        begin, end = origin + timedelta(days=10), origin + timedelta(days=30)
        expected = max(sum(a <= begin + timedelta(days=offset) < b for a, b in spans) for offset in range(20))
        assert occupancy_peak(spans, begin, end) == expected
