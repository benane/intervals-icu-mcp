"""Detect sustained climbs in an activity's altitude profile.

This is a hardened port of the algorithm that was validated against two real
rides in the cowork session (see the write-support requirement doc, section 8).

It works purely on the ``distance`` / ``altitude`` / ``time`` streams:

1. Forward-fill gaps and clamp implausible sample-to-sample altitude jumps
   (barometer spikes, tunnels, bridges).
2. Smooth the altitude with a centred moving average.
3. Break the smoothed profile into monotonic "legs" between significant
   reversals (zig-zag pivots - a reversal only counts once it exceeds
   ``reversal_m`` metres).
4. Walk the legs, gluing consecutive uphill legs together and bridging small
   dips, so a real climb is not split into fragments by a short false flat.
5. Trim standstills off the segment edges: a stop of at least ``stop_trim_s``
   seconds in the first half of a segment (traffic light, photo or food stop at
   the foot of the climb) moves the start to the moment riding resumes, so the
   pause does not inflate the interval's duration or deflate its average power.
   Uses the ``moving`` stream when available, otherwise the speed derived from
   ``distance`` / ``time``.
6. Keep the merged segments that clear the gain / length / grade thresholds.

Default thresholds: 18 m gain, 250 m length, 1.4 % grade - low enough to catch
the 2.0-2.3 % drags while still leaving true flatland out. The merge step
bridges counter-climbs of up to 13 m over up to 450 m so a real climb is not
fragmented by a short false flat.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

Number = float | int


@dataclass(frozen=True)
class _Leg:
    """A monotonic stretch of the smoothed profile between two pivots."""

    start: int
    end: int
    delta: float


@dataclass(frozen=True)
class Climb:
    """A detected climb segment, indexed into the activity data streams."""

    start_index: int
    end_index: int
    start_km: float
    end_km: float
    length_m: float
    gain_m: float
    grade_pct: float
    start_time_s: int
    end_time_s: int


def _ffill(values: Sequence[Number | None]) -> list[float]:
    """Forward-fill ``None`` gaps; back-fill a leading run of ``None``."""
    out: list[float] = []
    last: float | None = None
    for v in values:
        if v is None:
            out.append(last if last is not None else 0.0)
        else:
            last = float(v)
            out.append(last)
    if last is None:
        return out  # every sample was None
    for i, v in enumerate(values):
        if v is not None:
            first_real = float(v)
            for j in range(i):
                out[j] = first_real
            break
    return out


def _declip(alt: Sequence[float], max_step_m: float) -> list[float]:
    """Clamp implausible sample-to-sample altitude jumps."""
    if not alt:
        return []
    out: list[float] = [float(alt[0])]
    for i in range(1, len(alt)):
        step = float(alt[i]) - float(alt[i - 1])
        if step > max_step_m:
            step = max_step_m
        elif step < -max_step_m:
            step = -max_step_m
        out.append(out[-1] + step)
    return out


def _smooth(values: Sequence[float], window_pts: int) -> list[float]:
    """Centred moving average with a half-window of ``window_pts`` samples."""
    n = len(values)
    out: list[float] = []
    for i in range(n):
        lo = max(0, i - window_pts)
        hi = min(n, i + window_pts + 1)
        window = values[lo:hi]
        out.append(sum(window) / len(window))
    return out


def _moving_mask(
    dist: Sequence[float],
    secs: Sequence[float],
    moving: Sequence[bool | None] | None,
    speed_ms: float,
) -> list[bool]:
    """Per-sample "the rider is moving here" flag.

    Prefers the activity's own ``moving`` stream; when it is missing or the
    wrong length, falls back to the local speed from ``distance`` / ``time``.
    Unknown (``None``) entries count as moving - we only ever trim on a *known*
    standstill.
    """
    n = len(dist)
    if moving is not None and len(moving) == n:
        return [m is not False for m in moving]
    mask: list[bool] = [True] * n
    for i in range(1, n):
        dt = secs[i] - secs[i - 1]
        if dt <= 0:
            continue
        mask[i] = (dist[i] - dist[i - 1]) / dt >= speed_ms
    return mask


def _trim_stops(
    seg_start: int,
    seg_end: int,
    dist: Sequence[float],
    secs: Sequence[float],
    move_mask: Sequence[bool],
    stop_trim_s: float,
) -> tuple[int, int]:
    """Pull the segment edges in past long standstills.

    A stop of at least ``stop_trim_s`` seconds in the first half of the segment
    (a traffic light or food stop at the foot of the climb) moves the start to
    the moment riding resumes. Any non-moving samples left on either edge are
    then dropped. Interior stops in the upper half are left untouched.
    """
    if seg_end <= seg_start:
        return seg_start, seg_end

    half_d = dist[seg_start] + 0.5 * (dist[seg_end] - dist[seg_start])
    k = seg_start
    resume_at: int | None = None
    while k <= seg_end and dist[k] <= half_d:
        if move_mask[k]:
            k += 1
            continue
        run_start = k
        while k <= seg_end and not move_mask[k]:
            k += 1
        if secs[min(k, seg_end)] - secs[run_start] >= stop_trim_s:
            resume_at = k
    if resume_at is not None:
        seg_start = min(resume_at, seg_end)

    while seg_start < seg_end and not move_mask[seg_start]:
        seg_start += 1
    while seg_end > seg_start and not move_mask[seg_end]:
        seg_end -= 1
    return seg_start, seg_end


def _zigzag_pivots(alt_s: Sequence[float], reversal_m: float) -> list[int]:
    """Indices of significant turning points in the smoothed altitude profile."""
    n = len(alt_s)
    if n == 0:
        return []
    pivots: list[int] = [0]
    direction = 0
    extreme_idx = 0
    extreme_alt = alt_s[0]
    for i in range(1, n):
        a = alt_s[i]
        if direction >= 0 and a >= extreme_alt:
            extreme_alt, extreme_idx, direction = a, i, 1
        elif direction <= 0 and a <= extreme_alt:
            extreme_alt, extreme_idx, direction = a, i, -1
        else:
            diff = (extreme_alt - a) if direction == 1 else (a - extreme_alt)
            if diff >= reversal_m:
                pivots.append(extreme_idx)
                direction = 1 if a > extreme_alt else -1
                extreme_alt, extreme_idx = a, i
    if pivots[-1] != extreme_idx:
        pivots.append(extreme_idx)
    return pivots


def detect_climbs(
    *,
    distance: Sequence[Number | None],
    altitude: Sequence[Number | None],
    time: Sequence[Number | None],
    moving: Sequence[bool | None] | None = None,
    min_gain_m: float = 18.0,
    min_length_m: float = 250.0,
    min_grade_pct: float = 1.4,
    reversal_m: float = 5.0,
    merge_fall_m: float = 13.0,
    merge_gap_m: float = 450.0,
    smooth_pts: int = 8,
    max_step_m: float = 12.0,
    stop_trim_s: float = 120.0,
    moving_speed_ms: float = 0.5,
) -> list[Climb]:
    """Return the climbs found in the given streams, ordered by start.

    ``distance`` is metres, ``altitude`` is metres, ``time`` is seconds since the
    activity start. The three streams must be the same length; if they are not,
    or any is shorter than a smoothing window, an empty list is returned.

    ``moving`` is the optional per-sample boolean stream from intervals.icu. When
    given (and the right length) it drives the standstill trimming; otherwise the
    trimmer falls back to the speed derived from ``distance`` / ``time`` and the
    ``moving_speed_ms`` threshold. ``stop_trim_s`` is the shortest pause that gets
    trimmed off a segment's leading half.
    """
    n = len(altitude)
    if n < 2 * smooth_pts + 1 or len(distance) != n or len(time) != n:
        return []

    dist = _ffill(distance)
    alt = _ffill(altitude)
    secs = _ffill(time)
    alt_s = _smooth(_declip(alt, max_step_m), smooth_pts)
    move_mask = _moving_mask(dist, secs, moving, moving_speed_ms)

    pivots = _zigzag_pivots(alt_s, reversal_m)
    legs = [_Leg(a, b, alt_s[b] - alt_s[a]) for a, b in zip(pivots, pivots[1:], strict=False)]

    climbs: list[Climb] = []
    i = 0
    total = len(legs)
    while i < total:
        if legs[i].delta <= 0:
            i += 1
            continue

        seg_start = legs[i].start
        seg_end = legs[i].end
        j = i + 1
        while j < total:
            leg = legs[j]
            if leg.delta > 0:
                seg_end = leg.end
                j += 1
                continue
            fall = -leg.delta
            gap_dist = dist[leg.end] - dist[leg.start]
            bridges = (
                fall <= merge_fall_m
                and gap_dist <= merge_gap_m
                and j + 1 < total
                and legs[j + 1].delta > 0
            )
            if bridges:
                j += 1
                continue
            break

        seg_start, seg_end = _trim_stops(seg_start, seg_end, dist, secs, move_mask, stop_trim_s)
        if seg_end <= seg_start:
            i = j
            continue

        length = dist[seg_end] - dist[seg_start]
        gain = alt_s[seg_end] - alt_s[seg_start]
        grade = (gain / length * 100.0) if length > 0 else 0.0
        if gain >= min_gain_m and length >= min_length_m and grade >= min_grade_pct:
            climbs.append(
                Climb(
                    start_index=seg_start,
                    end_index=seg_end,
                    start_km=round(dist[seg_start] / 1000.0, 2),
                    end_km=round(dist[seg_end] / 1000.0, 2),
                    length_m=round(length, 1),
                    gain_m=round(gain, 1),
                    grade_pct=round(grade, 1),
                    start_time_s=int(round(secs[seg_start])),
                    end_time_s=int(round(secs[seg_end])),
                )
            )
        i = j

    return climbs
