"""Tests for the climb detection algorithm."""

from intervals_icu_mcp.climb_detection import detect_climbs


def _ramp(start: float, end: float, n: int) -> list[float]:
    """Linear ramp of ``n`` points from ``start`` to ``end`` inclusive."""
    if n == 1:
        return [start]
    step = (end - start) / (n - 1)
    return [start + step * i for i in range(n)]


def _profile(segments: list[tuple[float, int]], *, dx: float = 1.0):
    """Build (distance, altitude, time) streams from (altitude_delta, n_points) legs."""
    altitude: list[float] = [0.0]
    for delta, n in segments:
        altitude.extend(_ramp(altitude[-1], altitude[-1] + delta, n + 1)[1:])
    distance = [i * dx for i in range(len(altitude))]
    time = list(range(len(altitude)))
    return distance, altitude, time


class TestDetectClimbs:
    def test_finds_a_clear_climb(self):
        # Rolling approach (so zig-zag pivots form at the climb base), then
        # 800 m climbing 60 m (7.5%), then a rolling run-out.
        rolling_in = [(10.0, 40), (-10.0, 40), (10.0, 40), (-10.0, 40)]
        rolling_out = [(-10.0, 40), (10.0, 40), (-10.0, 40), (10.0, 40)]
        distance, altitude, time = _profile(rolling_in + [(60.0, 800)] + rolling_out)

        climbs = detect_climbs(distance=distance, altitude=altitude, time=time)

        assert len(climbs) == 1
        climb = climbs[0]
        assert 50 < climb.gain_m < 70
        assert climb.grade_pct >= 5.0  # the steep 7.5% ramp dominates the segment
        assert climb.start_index < climb.end_index

    def test_ignores_flat_terrain(self):
        distance, altitude, time = _profile([(0.0, 2000)])
        assert detect_climbs(distance=distance, altitude=altitude, time=time) == []

    def test_ignores_a_climb_below_thresholds(self):
        # 1200 m gaining only 15 m -> 1.25% grade and 15 m gain, under both the
        # grade (1.4%) and gain (18 m) thresholds.
        distance, altitude, time = _profile([(0.0, 300), (15.0, 1200), (0.0, 300)])
        assert detect_climbs(distance=distance, altitude=altitude, time=time) == []

    def test_bridges_a_small_dip_into_one_climb(self):
        # up 40 m / 500 m, down 5 m / 100 m, up 40 m / 500 m  -> one merged climb
        distance, altitude, time = _profile(
            [(0.0, 200), (40.0, 500), (-5.0, 100), (40.0, 500), (0.0, 200)]
        )

        climbs = detect_climbs(distance=distance, altitude=altitude, time=time)

        assert len(climbs) == 1
        assert climbs[0].gain_m > 65  # ~75 m net across the bridged dip

    def test_tolerates_none_gaps_in_streams(self):
        distance, altitude, time = _profile([(0.0, 300), (60.0, 800), (0.0, 300)])
        altitude_with_gaps: list[float | None] = list(altitude)
        for i in range(50, len(altitude_with_gaps), 37):
            altitude_with_gaps[i] = None

        climbs = detect_climbs(distance=distance, altitude=altitude_with_gaps, time=time)
        assert len(climbs) == 1

    def test_clamps_barometer_spike(self):
        # A single implausible +80 m spike must not become a phantom climb.
        distance, altitude, time = _profile([(0.0, 1000)])
        altitude[500] = 80.0

        climbs = detect_climbs(distance=distance, altitude=altitude, time=time)
        assert climbs == []

    def test_returns_empty_on_mismatched_stream_lengths(self):
        assert detect_climbs(distance=[0, 1, 2], altitude=[0, 1], time=[0, 1, 2]) == []

    def test_returns_empty_on_tiny_streams(self):
        assert detect_climbs(distance=[0, 1], altitude=[0, 5], time=[0, 1]) == []


def _climb_with_stop_at_the_foot(stop_pts: int = 400):
    """(distance, altitude, time, moving) for: 200 m flat approach, a standstill
    (distance frozen, time still ticking), then 800 m climbing 60 m, then flat.
    """
    distance: list[float] = []
    altitude: list[float] = []
    moving: list[bool] = []

    for i in range(200):  # flat riding approach, 1 m/s
        distance.append(float(i))
        altitude.append(0.0)
        moving.append(True)

    for _ in range(stop_pts):  # standstill at the foot of the climb
        distance.append(199.0)
        altitude.append(0.0)
        moving.append(False)

    for k in range(1, 801):  # the climb: +60 m over 800 m
        distance.append(199.0 + k)
        altitude.append(60.0 * k / 800.0)
        moving.append(True)

    for k in range(1, 201):  # descending run-out so the top pivot closes at 1399
        distance.append(distance[-1] + 1.0)
        altitude.append(60.0 - 20.0 * k / 200.0)
        moving.append(True)

    time = list(range(len(distance)))
    return distance, altitude, time, moving


class TestStandstillTrimming:
    def test_trims_a_stop_at_the_foot_using_the_moving_stream(self):
        distance, altitude, time, moving = _climb_with_stop_at_the_foot(stop_pts=400)

        climbs = detect_climbs(distance=distance, altitude=altitude, time=time, moving=moving)

        assert len(climbs) == 1
        climb = climbs[0]
        # start pulled past the 400 s pause to where riding resumes (index 600)
        assert 600 <= climb.start_index <= 640
        # the interval's duration is the ~800 s of real climbing, not ~1200 s
        assert (climb.end_time_s - climb.start_time_s) < 900
        assert climb.gain_m > 50

    def test_falls_back_to_distance_speed_when_no_moving_stream(self):
        distance, altitude, time, _ = _climb_with_stop_at_the_foot(stop_pts=400)

        climbs = detect_climbs(distance=distance, altitude=altitude, time=time)

        assert len(climbs) == 1
        assert climbs[0].start_index >= 600
        assert (climbs[0].end_time_s - climbs[0].start_time_s) < 900

    def test_keeps_the_stop_when_moving_stream_says_moving(self):
        # An all-True moving stream must switch the trimming off entirely.
        distance, altitude, time, _ = _climb_with_stop_at_the_foot(stop_pts=400)
        moving = [True] * len(distance)

        climbs = detect_climbs(distance=distance, altitude=altitude, time=time, moving=moving)

        assert len(climbs) == 1
        assert climbs[0].start_index < 200  # untrimmed: starts back at the approach
        assert (climbs[0].end_time_s - climbs[0].start_time_s) > 1100

    def test_short_stop_below_threshold_is_left_alone(self):
        # A 60 s pause is under stop_trim_s (120 s) -> not trimmed.
        distance, altitude, time, moving = _climb_with_stop_at_the_foot(stop_pts=60)

        climbs = detect_climbs(distance=distance, altitude=altitude, time=time, moving=moving)

        assert len(climbs) == 1
        assert climbs[0].start_index < 200
