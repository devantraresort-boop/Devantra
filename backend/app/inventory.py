"""Pure inventory calculations shared by availability and admin capacity checks."""
from collections import defaultdict
from datetime import date


def occupancy_peak(stays, check_in: date, check_out: date) -> int:
    """Sweep half-open stays so departing and arriving guests can share a date."""
    changes = defaultdict(int)
    for start, end in stays:
        start, end = max(start, check_in), min(end, check_out)
        if start < end:
            changes[start] += 1
            changes[end] -= 1
    peak = count = 0
    for day in sorted(changes):
        count += changes[day]
        peak = max(peak, count)
    return peak

