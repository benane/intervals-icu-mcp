"""Interval write tools for the Intervals.icu MCP server.

Three tools:

* ``create_intervals``          - add intervals to an activity (additive / merge).
* ``replace_intervals``         - replace the whole interval set (destructive).
* ``mark_climbs_as_intervals``  - detect climbs from the elevation profile and
  write them as intervals in one call.

Notes on how intervals.icu behaves (verified against the live API):

* An interval is defined by ``start_index`` / ``end_index`` - indices into the
  activity's data streams. ``start_time`` / ``end_time`` (seconds) are derived by
  the server and cannot be relied on for writing when the ride has pauses.
* Every interval written through the API is stored as ``type="WORK"``. The
  ``RECOVERY`` type is only ever assigned by intervals.icu's own auto-analyzer,
  so these tools do not expose a ``type`` parameter.
* The merge endpoint (``?all=false``) does not reject a new interval that sits
  inside an existing one - it *carves* it out, splitting the surrounding
  interval so the timeline stays contiguous (this is exactly what the "A" key
  does in the web UI). ``create_intervals`` and ``mark_climbs_as_intervals``
  therefore allow this, but every existing interval that gets carved is listed
  in the response so nothing happens silently. Two *new* intervals in the same
  request may still not overlap each other - that is rejected up front.
* Any interval write flips the activity's ``icu_intervals_edited`` flag to true,
  which stops intervals.icu from auto-updating that activity's intervals. Use
  "Reset" on the activity's Intervals panel in the web UI to hand control back
  to the auto-analyzer (and to undo carving).
"""

import json
from typing import Annotated, Any

from fastmcp import Context

from ..auth import ICUConfig
from ..client import ICUAPIError, ICUClient
from ..climb_detection import Climb, detect_climbs
from ..models import Interval
from ..response_builder import ResponseBuilder

_EDITED_FLAG_NOTE = (
    "This activity's intervals are now marked as manually edited "
    "(icu_intervals_edited=true); intervals.icu will not auto-update them. "
    "Use 'Reset' on the activity's Intervals panel in the web UI to revert."
)


# ==================== shared helpers ====================


def _parse_interval_list(raw: str) -> tuple[list[dict[str, Any]], str | None]:
    """Parse the JSON ``intervals`` argument into a list of dicts.

    Returns ``(items, error)``. ``error`` is a message string when parsing fails.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return [], f"Invalid JSON: {e}"

    if not isinstance(parsed, list):
        return [], "Expected a JSON array of interval objects."
    if not parsed:
        return [], "The intervals array is empty - nothing to write."

    items: list[dict[str, Any]] = []
    for idx, entry in enumerate(parsed):  # type: ignore[misc]
        if not isinstance(entry, dict):
            return [], f"Interval {idx} is not an object."
        items.append({str(k): v for k, v in entry.items()})  # type: ignore[misc]
    return items, None


def _seconds_to_index(time_stream: list[int], seconds: float) -> int:
    """First stream index whose timestamp is >= ``seconds`` (clamped to the end)."""
    for i, t in enumerate(time_stream):
        if t >= seconds:
            return i
    return len(time_stream) - 1


def _resolve_bounds(
    item: dict[str, Any],
    time_stream: list[int] | None,
    label_for_error: str,
) -> tuple[int, int, str | None]:
    """Turn one input item into ``(start_index, end_index, error)``.

    Accepts either ``start_index`` / ``end_index`` directly, or
    ``start_time_s`` / ``end_time_s`` which are mapped through the ``time``
    stream.
    """
    has_index = "start_index" in item and "end_index" in item
    has_time = "start_time_s" in item and "end_time_s" in item

    if has_index:
        try:
            start = int(item["start_index"])
            end = int(item["end_index"])
        except (TypeError, ValueError):
            return 0, 0, f"{label_for_error}: start_index/end_index must be integers."
    elif has_time:
        if not time_stream:
            return (
                0,
                0,
                (
                    f"{label_for_error}: start_time_s/end_time_s given but the activity "
                    "has no time stream to map them against."
                ),
            )
        try:
            start_s = float(item["start_time_s"])
            end_s = float(item["end_time_s"])
        except (TypeError, ValueError):
            return 0, 0, f"{label_for_error}: start_time_s/end_time_s must be numbers."
        start = _seconds_to_index(time_stream, start_s)
        end = _seconds_to_index(time_stream, end_s)
    else:
        return (
            0,
            0,
            (f"{label_for_error}: need either start_index+end_index or start_time_s+end_time_s."),
        )

    if start < 0 or end < 0:
        return 0, 0, f"{label_for_error}: indices must be non-negative."
    if end <= start:
        return 0, 0, f"{label_for_error}: end must be after start (got {start}..{end})."
    return start, end, None


def _internal_overlaps(new_windows: list[tuple[int, int, str]]) -> list[str]:
    """Messages for any pair of *new* windows that overlap each other."""
    errors: list[str] = []
    for i in range(len(new_windows)):
        for j in range(i + 1, len(new_windows)):
            s1, e1, l1 = new_windows[i]
            s2, e2, l2 = new_windows[j]
            if s1 < e2 and s2 < e1:
                errors.append(f"'{l1}' ({s1}-{e1}) overlaps '{l2}' ({s2}-{e2}) in this request")
    return errors


def _carved_existing(
    new_windows: list[tuple[int, int, str]],
    existing: list[Interval],
) -> list[dict[str, Any]]:
    """Existing intervals that a new window overlaps and that the merge will split."""
    carved: list[dict[str, Any]] = []
    for ex in existing:
        es, ee = ex.start_index, ex.end_index
        if es is None or ee is None:
            continue
        hitters = [lbl for ns, ne, lbl in new_windows if ns < ee and es < ne]
        if hitters:
            carved.append(
                {
                    "start_index": es,
                    "end_index": ee,
                    "type": ex.type,
                    "label": ex.label,
                    "carved_by": hitters,
                }
            )
    return carved


def _summarize(intervals: list[Interval]) -> list[dict[str, Any]]:
    """Compact view of an interval list for tool responses."""
    rows: list[dict[str, Any]] = []
    for iv in intervals:
        row: dict[str, Any] = {
            "start_index": iv.start_index,
            "end_index": iv.end_index,
            "type": iv.type,
        }
        if iv.label:
            row["label"] = iv.label
        if iv.start_time is not None:
            row["start_seconds"] = iv.start_time
        if iv.end_time is not None:
            row["end_seconds"] = iv.end_time
        if iv.distance:
            row["distance_meters"] = round(iv.distance, 1)
        if iv.average_watts:
            row["average_watts"] = iv.average_watts
        rows.append(row)
    return rows


async def _get_time_stream(client: ICUClient, activity_id: str) -> list[int] | None:
    """Fetch just the ``time`` stream (used to map seconds -> index)."""
    streams = await client.get_activity_streams(activity_id, ["time"])
    if not streams.time:
        return None
    return [int(t) for t in streams.time if t is not None]


def _build_payload(
    items: list[dict[str, Any]],
    time_stream: list[int] | None,
) -> tuple[list[tuple[int, int, str]], list[dict[str, Any]], str | None]:
    """Validate input items into ``(windows, api_payload, error)``."""
    windows: list[tuple[int, int, str]] = []
    payload: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        label = str(item.get("label") or f"interval {i + 1}")
        start, end, err = _resolve_bounds(item, time_stream, label)
        if err:
            return [], [], err
        windows.append((start, end, label))
        entry: dict[str, Any] = {"start_index": start, "end_index": end, "type": "WORK"}
        if item.get("label"):
            entry["label"] = str(item["label"])
        payload.append(entry)
    return windows, payload, None


def _match_written(after: list[Interval], windows: list[tuple[int, int, str]]) -> list[Interval]:
    return [
        iv for iv in after if any(iv.start_index == s and iv.end_index == e for s, e, _ in windows)
    ]


# ==================== tools ====================


async def create_intervals(
    activity_id: Annotated[str, "Activity ID to add intervals to"],
    intervals: Annotated[
        str,
        "JSON array of interval objects. Each object needs either "
        '"start_index"+"end_index" (indices into the activity data streams) or '
        '"start_time_s"+"end_time_s" (seconds since activity start), plus an '
        'optional "label". Example: '
        '[{"start_index": 1630, "end_index": 1804, "label": "Anstieg km 27.2-30.0"}]',
    ],
    ctx: Context | None = None,
) -> str:
    """Add one or more intervals to an activity, keeping the existing ones.

    The new intervals are merged in. Nothing is deleted: where a new interval
    lands inside an existing one, the existing interval is split around it (same
    as pressing "A" in the web UI). Every existing interval affected this way is
    listed under ``carved_existing`` in the response.

    Two new intervals in the same request may not overlap each other - that is
    rejected before anything is written. All intervals are created with type
    "WORK" (intervals.icu only assigns "RECOVERY" via its own auto-analyzer).

    Args:
        activity_id: The activity to add intervals to.
        intervals: JSON array of interval objects (see the parameter description).

    Returns:
        JSON string with the added intervals, the intervals carved to make room,
        and the full interval list after the write.
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    items, parse_error = _parse_interval_list(intervals)
    if parse_error:
        return ResponseBuilder.build_error_response(parse_error, error_type="validation_error")

    try:
        async with ICUClient(config) as client:
            needs_time = any("start_index" not in it or "end_index" not in it for it in items)
            time_stream = await _get_time_stream(client, activity_id) if needs_time else None

            windows, payload, err = _build_payload(items, time_stream)
            if err:
                return ResponseBuilder.build_error_response(err, error_type="validation_error")

            clashes = _internal_overlaps(windows)
            if clashes:
                return ResponseBuilder.build_error_response(
                    "Intervals in this request overlap each other: " + "; ".join(clashes),
                    error_type="overlap_error",
                )

            existing = await client.get_activity_intervals(activity_id)
            carved = _carved_existing(windows, existing)

            after = await client.update_intervals(activity_id, payload, replace=False)
            written = _match_written(after, windows)

            msg = f"Added {len(payload)} interval(s); activity now has {len(after)}."
            if carved:
                msg += f" {len(carved)} existing interval(s) were split to make room."

            return ResponseBuilder.build_response(
                data={
                    "activity_id": activity_id,
                    "added": _summarize(written),
                    "carved_existing": carved,
                    "all_intervals": _summarize(after),
                },
                metadata={
                    "message": msg,
                    "added_count": len(payload),
                    "carved_count": len(carved),
                    "total_count": len(after),
                    "note": _EDITED_FLAG_NOTE,
                },
                query_type="create_intervals",
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {e}", error_type="internal_error"
        )


async def replace_intervals(
    activity_id: Annotated[str, "Activity ID whose intervals will be replaced"],
    intervals: Annotated[
        str,
        "JSON array of interval objects, same shape as create_intervals. This set "
        "REPLACES every existing interval on the activity.",
    ],
    ctx: Context | None = None,
) -> str:
    """Replace the entire interval set of an activity (destructive).

    Every existing interval is discarded and the given set is written in its
    place. Intervals within the request may not overlap. All intervals are
    created with type "WORK".

    Use create_intervals instead if you only want to add intervals.

    Args:
        activity_id: The activity whose intervals will be replaced.
        intervals: JSON array of interval objects (see create_intervals).

    Returns:
        JSON string summarising the new interval set.
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    items, parse_error = _parse_interval_list(intervals)
    if parse_error:
        return ResponseBuilder.build_error_response(parse_error, error_type="validation_error")

    try:
        async with ICUClient(config) as client:
            needs_time = any("start_index" not in it or "end_index" not in it for it in items)
            time_stream = await _get_time_stream(client, activity_id) if needs_time else None

            windows, payload, err = _build_payload(items, time_stream)
            if err:
                return ResponseBuilder.build_error_response(err, error_type="validation_error")

            clashes = _internal_overlaps(windows)
            if clashes:
                return ResponseBuilder.build_error_response(
                    "Intervals in the request overlap: " + "; ".join(clashes),
                    error_type="overlap_error",
                )

            before = await client.get_activity_intervals(activity_id)
            after = await client.update_intervals(activity_id, payload, replace=True)

            return ResponseBuilder.build_response(
                data={"activity_id": activity_id, "intervals": _summarize(after)},
                metadata={
                    "message": f"Replaced {len(before)} interval(s) with {len(after)}.",
                    "removed_count": len(before),
                    "new_count": len(after),
                    "note": _EDITED_FLAG_NOTE,
                },
                query_type="replace_intervals",
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {e}", error_type="internal_error"
        )


def _climb_label(index: int, climb: Climb) -> str:
    return f"Anstieg {index}: +{climb.gain_m:.0f}hm, {climb.grade_pct:.1f}%"


async def mark_climbs_as_intervals(
    activity_id: Annotated[str, "Activity ID (outdoor ride/run with elevation data)"],
    min_gain_m: Annotated[float, "Minimum net elevation gain for a climb, metres"] = 18.0,
    min_length_m: Annotated[float, "Minimum climb length, metres"] = 250.0,
    min_grade_pct: Annotated[float, "Minimum average grade, percent"] = 1.4,
    merge_fall_m: Annotated[
        float, "Bridge a counter-climb into the climb if it drops at most this many metres"
    ] = 13.0,
    merge_gap_m: Annotated[float, "...and is at most this many metres long"] = 450.0,
    ctx: Context | None = None,
) -> str:
    """Detect climbs from the elevation profile and add them as intervals.

    Fetches the distance/altitude/time/moving streams, runs climb detection
    (zig-zag segmentation of the smoothed altitude, small dips bridged so a climb
    is not fragmented, standstills of 2+ minutes at the foot of a climb trimmed
    off so a traffic light or food stop does not inflate the interval), then adds
    each climb as a "WORK" interval labelled e.g. "Anstieg 1: +78hm, 2.8%".

    The write is additive (see create_intervals): existing intervals - including
    the ones intervals.icu auto-detected - are kept, and any that a climb lands
    inside are split around it. The affected ones are listed under
    ``carved_existing``.

    Args:
        activity_id: The outdoor activity to tag.
        min_gain_m: Minimum net elevation gain for a segment to count (default 18).
        min_length_m: Minimum horizontal length (default 250).
        min_grade_pct: Minimum average grade (default 1.4 - low enough to catch
            2.0-2.3% drags, high enough to leave true flatland out).
        merge_fall_m: A counter-climb (short descent) between two uphill stretches
            is bridged into one climb if it drops at most this much (default 13).
        merge_gap_m: ...and spans at most this distance (default 450).

    Returns:
        JSON string with the climbs found, the intervals written, and the
        existing intervals carved to fit them.
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    thresholds = {
        "min_gain_m": min_gain_m,
        "min_length_m": min_length_m,
        "min_grade_pct": min_grade_pct,
        "merge_fall_m": merge_fall_m,
        "merge_gap_m": merge_gap_m,
    }

    try:
        async with ICUClient(config) as client:
            streams = await client.get_activity_streams(
                activity_id, ["distance", "altitude", "time", "moving"]
            )
            if not streams.altitude or not streams.distance or not streams.time:
                return ResponseBuilder.build_error_response(
                    "Activity has no distance/altitude/time streams - cannot detect climbs "
                    "(is this an indoor activity or one without a recorded elevation track?).",
                    error_type="validation_error",
                )

            climbs = detect_climbs(
                distance=streams.distance,
                altitude=streams.altitude,
                time=streams.time,
                moving=streams.moving,
                min_gain_m=min_gain_m,
                min_length_m=min_length_m,
                min_grade_pct=min_grade_pct,
                merge_fall_m=merge_fall_m,
                merge_gap_m=merge_gap_m,
            )

            detected = [
                {
                    "start_km": c.start_km,
                    "end_km": c.end_km,
                    "length_m": c.length_m,
                    "gain_m": c.gain_m,
                    "grade_pct": c.grade_pct,
                    "start_time_s": c.start_time_s,
                    "end_time_s": c.end_time_s,
                }
                for c in climbs
            ]

            if not climbs:
                return ResponseBuilder.build_response(
                    data={"activity_id": activity_id, "climbs_detected": [], "written": []},
                    metadata={
                        "message": "No climbs matched the thresholds.",
                        "thresholds": thresholds,
                    },
                    query_type="mark_climbs_as_intervals",
                )

            windows = [
                (c.start_index, c.end_index, _climb_label(n, c))
                for n, c in enumerate(climbs, start=1)
            ]
            payload = [
                {"start_index": s, "end_index": e, "type": "WORK", "label": lbl}
                for s, e, lbl in windows
            ]

            existing = await client.get_activity_intervals(activity_id)
            carved = _carved_existing(windows, existing)

            after = await client.update_intervals(activity_id, payload, replace=False)
            written = _match_written(after, windows)

            total_gain = round(sum(c.gain_m for c in climbs), 1)
            msg = (
                f"Wrote {len(payload)} climb interval(s) "
                f"(total +{total_gain:.0f} hm detected); activity now has {len(after)} interval(s)."
            )
            if carved:
                msg += f" {len(carved)} existing interval(s) were split to make room."

            return ResponseBuilder.build_response(
                data={
                    "activity_id": activity_id,
                    "climbs_detected": detected,
                    "written": _summarize(written),
                    "carved_existing": carved,
                },
                analysis={
                    "climb_count": len(climbs),
                    "written_count": len(payload),
                    "carved_count": len(carved),
                    "total_gain_m_detected": total_gain,
                },
                metadata={
                    "message": msg,
                    "thresholds": thresholds,
                    "note": _EDITED_FLAG_NOTE,
                },
                query_type="mark_climbs_as_intervals",
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {e}", error_type="internal_error"
        )
