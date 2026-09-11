# Standstill trimming in climb detection

## Problem (field report)

On a real ride, "Anstieg 4" (km 57–60) came back as **65 min for 3 km**. That was
not a climb: it was a food stop that fell into the same window — ~57 min at 0 W,
then ~8 min of actual climbing at 188–250 W. The interval written to intervals.icu
spanned the whole standstill, so its duration and average power were meaningless.

## Cause

`detect_climbs()` in `src/intervals_icu_mcp/climb_detection.py` segments on
**stream indices** derived purely from `distance` / `altitude`, and never looked at
whether the rider was moving:

- During the stop, samples keep coming in with `distance` and `altitude` frozen.
- `_zigzag_pivots()` puts the lower turning point on the *first* sample that hits
  that altitude minimum — i.e. the moment the rider rolls to a halt, not the
  moment they set off again.
- `seg_start` therefore points at the start of the pause. `length`, `gain` and
  `grade` are unaffected (distance/altitude don't move during the stop), so the
  segment still clears every threshold.
- The interval is then written as `start_index … end_index` **including the whole
  standstill** → inflated duration, deflated average power / heart rate.

Any stop on a segment edge is affected (traffic light, photo, food); a stop right
at the foot of a climb is the worst case.

## Solution (implemented)

`detect_climbs()` now takes an optional `moving` stream and trims standstills off
the segment edges before the thresholds are checked (`_moving_mask` +
`_trim_stops`):

1. Build a per-sample "moving" mask. Prefer the activity's `moving` stream; when
   it is absent or the wrong length, fall back to the local speed from
   `distance` / `time` against `moving_speed_ms` (default 0.5 m/s ≈ 1.8 km/h).
2. If a run of non-moving samples lasting at least `stop_trim_s` (default 120 s)
   sits in the **first half** of the segment's distance, move `seg_start` to the
   sample where riding resumes.
3. Drop any non-moving samples still sitting on either edge.
4. Recompute `length` / `gain` / `grade` from the trimmed indices and re-check the
   thresholds, so a segment that only passed because of the pause now drops out.

`mark_climbs_as_intervals` requests the `moving` stream and passes it through.

### Deliberate limitations

- **Interior stops in the upper half of a climb are left in.** Cutting them would
  mean either splitting the climb or discarding real climbing before the stop.
  The field report was only about stops at the *foot*, so the trimmer stays
  conservative. Revisit if upper-half stops turn out to matter.
- Trailing standstills are only edge-trimmed, not scanned for a `stop_trim_s`
  run — a long stop just before the summit is rare and less distorting in
  proportion.

### Tests

`tests/test_climb_detection.py` → `TestStandstillTrimming`: trimming via the
`moving` stream, the distance-speed fallback, an all-moving stream switching
trimming off, and a sub-threshold stop being left alone.
