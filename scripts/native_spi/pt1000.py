"""Qualify direct PT1000 web values against guest-timed MAX31865 samples."""
from bisect import bisect_right


TOLERANCE_C = 0.15
# Q Duo polls the PT1000 every five guest seconds. A GET can see the
# preceding value briefly while the firmware publishes a newly read sample.
MAX_SAMPLE_AGE_MS = 6000
PUBLISH_SLACK_MS = 250
MAX_REQUEST_MS = 1000
MAX_LAST_READ_AGE_MS = 10000


def matches_fixture(value, expected):
    return type(value) in (int, float) and type(expected) in (int, float) and abs(value - expected) < TOLERANCE_C


def matching_sample(read, sample_times, fixture_values):
    before, after, value = read
    if before <= 0 or not 0 <= after - before <= MAX_REQUEST_MS:
        return None
    last = bisect_right(sample_times, after) - 1
    if last < 0 or fixture_values[last] is None:
        return None
    candidates = (last, last - 1) if last > 0 and sample_times[last] >= before - PUBLISH_SLACK_MS else (last,)
    for index in candidates:
        expected = fixture_values[index]
        sampled_at = sample_times[index]
        if after - sampled_at <= MAX_SAMPLE_AGE_MS and matches_fixture(value, expected):
            return {"guest_ms": sampled_at, "fixture_c": expected}
    return None


def dynamic_qualification(start_c, reads, events, end_ms):
    result = {"verified": False, "reads": len(reads), "matched_reads": 0, "matched_away_from_start": 0}
    sample_times, fixture_values = [], []
    for event in events:
        at = event.get("guest_us")
        expected = event.get("fixture_c")
        if type(at) is not int or at < 0 or (sample_times and at // 1000 < sample_times[-1]):
            result["reason"] = "invalid_sample_clock"
            return result
        if at == 0:
            continue  # Native guest clock was not yet sampled.
        sample_times.append(at // 1000)
        fixture_values.append(expected if type(expected) in (int, float) and 0 <= expected <= 100 else None)

    previous_read_end = 0
    for read in reads:
        before, after, value = read
        if not previous_read_end <= before <= after:
            result["reason"] = "invalid_read_clock"
            return result
        previous_read_end = after
        match = matching_sample(read, sample_times, fixture_values)
        result["last_read"] = {"from_ms": before, "to_ms": after, "value_c": value, "match": match}
        if match is not None:
            result["matched_reads"] += 1
            if not matches_fixture(value, start_c) and not matches_fixture(match["fixture_c"], start_c):
                result["matched_away_from_start"] += 1

    if not sample_times or not reads:
        result["reason"] = "missing_evidence"
    elif not 0 <= end_ms - reads[-1][1] <= MAX_LAST_READ_AGE_MS:
        result["reason"] = "last_read_stale"
    elif result["last_read"]["match"] is None:
        result["reason"] = "last_read_unmatched"
    elif result["matched_away_from_start"] == 0:
        result["reason"] = "no_change_proven"
    else:
        result["verified"] = True
    return result
