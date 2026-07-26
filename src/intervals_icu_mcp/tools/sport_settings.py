"""Sport-specific settings tools for FTP, threshold HR, and threshold pace."""

import difflib
import json
from typing import Annotated, Any

from fastmcp import Context

from ..auth import ICUConfig
from ..client import ICUAPIError, ICUClient
from ..models import SportSettings
from ..response_builder import ResponseBuilder

# intervals.icu stores threshold_pace in meters per second no matter what
# pace_units says. pace_units only controls the distance the pace is shown over.
PACE_UNIT_METERS: dict[str, float] = {
    "MINS_KM": 1000.0,
    "MINS_MILE": 1609.344,
    "SECS_100M": 100.0,
    "SECS_100Y": 91.44,
    "SECS_500M": 500.0,
}

PACE_UNIT_LABELS: dict[str, str] = {
    "MINS_KM": "/km",
    "MINS_MILE": "/mi",
    "SECS_100M": "/100m",
    "SECS_100Y": "/100y",
    "SECS_500M": "/500m",
}

DEFAULT_PACE_UNITS = "MINS_KM"

# The activity types intervals.icu accepts in a "types" array. Worth checking
# before sending: the API answers an unknown type with HTTP 400 "JSON parse
# error", which says nothing about the actual problem.
SPORT_TYPES: tuple[str, ...] = (
    "Ride", "Run", "Swim", "WeightTraining", "Hike", "Walk", "AlpineSki", "BackcountrySki",
    "Badminton", "Canoeing", "Crossfit", "EBikeRide", "EMountainBikeRide", "Elliptical", "Golf",
    "GravelRide", "TrackRide", "Handcycle", "HighIntensityIntervalTraining", "Hockey",
    "IceSkate", "InlineSkate", "Kayaking", "Kitesurf", "MountainBikeRide", "NordicSki",
    "OpenWaterSwim", "Padel", "Pilates", "Pickleball", "Racquetball", "Rugby", "RockClimbing",
    "RollerSki", "Rowing", "Sail", "Skateboard", "Snowboard", "Snowshoe", "Soccer", "Squash",
    "StairStepper", "StandUpPaddling", "Surfing", "TableTennis", "Tennis", "TrailRun",
    "Transition", "Velomobile", "VirtualRide", "VirtualRow", "VirtualRun", "VirtualSki",
    "WaterSport", "Wheelchair", "Windsurf", "Workout", "Yoga", "Other",
)  # fmt: skip

_SPORT_TYPES_BY_LOWER = {name.lower(): name for name in SPORT_TYPES}


class SportSettingsInputError(ValueError):
    """Raised when a tool argument cannot be interpreted."""


def _as_int(value: int | str | None, field: str) -> int | None:
    """Coerce a tool argument to int, tolerating clients that send numbers as strings."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise SportSettingsInputError(f"{field} must be a number, got a boolean")
    if isinstance(value, int):
        return value
    try:
        return int(float(value.strip()))
    except (ValueError, AttributeError):
        raise SportSettingsInputError(f"{field} must be a number, got {value!r}") from None


def _parse_sport_types(value: str | list[str]) -> list[str]:
    """Normalise the sport types argument into a list of valid activity type names.

    Accepts a real list, a single type, a comma-separated list, or a JSON array
    that arrived as a string because the client serialised it that way. No type
    name contains a comma, so splitting on one is unambiguous.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                raise SportSettingsInputError(
                    f"sport_types looks like a list but is not valid JSON: {value!r}"
                ) from None
            if not isinstance(decoded, list):
                raise SportSettingsInputError(
                    f"sport_types must be a sport type or a list of them, got {value!r}"
                )
            raw = decoded
        else:
            raw = text.split(",")
    else:
        raw = list(value)

    types: list[str] = []
    for item in raw:
        name = str(item).strip()
        if not name:
            continue
        known = _SPORT_TYPES_BY_LOWER.get(name.lower())
        if known is None:
            hint = difflib.get_close_matches(name, SPORT_TYPES, n=3, cutoff=0.5)
            suggestion = (
                f" Did you mean {' or '.join(hint)}?"
                if hint
                else " Common ones are Ride, Run, Swim, Walk, Hike and WeightTraining."
            )
            raise SportSettingsInputError(
                f"{name!r} is not an intervals.icu activity type.{suggestion}"
            )
        if known not in types:
            types.append(known)

    if not types:
        raise SportSettingsInputError("At least one sport type is required")

    return types


def _pace_to_mps(value: float | str, units: str, field: str) -> float:
    """Convert a threshold pace into meters per second.

    Accepts "M:SS" (minutes and seconds over the unit distance) or a plain
    number of seconds over the unit distance.
    """
    meters = PACE_UNIT_METERS.get(units, PACE_UNIT_METERS[DEFAULT_PACE_UNITS])

    if isinstance(value, str) and ":" in value:
        minutes_part, _, seconds_part = value.strip().partition(":")
        try:
            seconds = int(minutes_part) * 60 + float(seconds_part)
        except ValueError:
            raise SportSettingsInputError(
                f"{field} must look like '4:30' or be a number of seconds, got {value!r}"
            ) from None
    else:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            raise SportSettingsInputError(
                f"{field} must look like '4:30' or be a number of seconds, got {value!r}"
            ) from None

    if seconds <= 0:
        raise SportSettingsInputError(f"{field} must be greater than zero, got {value!r}")

    return meters / seconds


def format_pace(mps: float, units: str | None) -> str:
    """Render a meters-per-second pace as 'M:SS' over the configured unit distance."""
    units = units or DEFAULT_PACE_UNITS
    meters = PACE_UNIT_METERS.get(units, PACE_UNIT_METERS[DEFAULT_PACE_UNITS])
    total_seconds = round(meters / mps)
    label = PACE_UNIT_LABELS.get(units, PACE_UNIT_LABELS[DEFAULT_PACE_UNITS])
    return f"{total_seconds // 60}:{total_seconds % 60:02d} {label}"


def _summarize(settings: SportSettings) -> dict[str, Any]:
    """Build the response payload for a single sport settings entry."""
    info: dict[str, Any] = {
        "id": settings.id,
        "types": settings.types,
    }

    if settings.ftp is not None:
        info["ftp_watts"] = settings.ftp
    if settings.indoor_ftp is not None:
        info["indoor_ftp_watts"] = settings.indoor_ftp
    if settings.lthr is not None:
        info["threshold_hr_bpm"] = settings.lthr
    if settings.max_hr is not None:
        info["max_hr_bpm"] = settings.max_hr
    if settings.threshold_pace:
        info["threshold_pace"] = format_pace(settings.threshold_pace, settings.pace_units)
        info["threshold_pace_mps"] = round(settings.threshold_pace, 4)
    if settings.pace_units is not None:
        info["pace_units"] = settings.pace_units

    return info


def _build_update_payload(
    ftp: int | str | None,
    indoor_ftp: int | str | None,
    threshold_hr: int | str | None,
    max_hr: int | str | None,
    threshold_pace: float | str | None,
    pace_units: str | None,
    current_pace_units: str | None,
) -> dict[str, Any]:
    """Translate tool arguments into an intervals.icu request body."""
    payload: dict[str, Any] = {}

    for field, value in (
        ("ftp", _as_int(ftp, "ftp")),
        ("indoor_ftp", _as_int(indoor_ftp, "indoor_ftp")),
        ("lthr", _as_int(threshold_hr, "threshold_hr")),
        ("max_hr", _as_int(max_hr, "max_hr")),
    ):
        if value is not None:
            payload[field] = value

    if pace_units is not None:
        if pace_units not in PACE_UNIT_METERS:
            raise SportSettingsInputError(
                f"pace_units must be one of {', '.join(PACE_UNIT_METERS)}, got {pace_units!r}"
            )
        payload["pace_units"] = pace_units

    if threshold_pace is not None:
        units = pace_units or current_pace_units or DEFAULT_PACE_UNITS
        payload["threshold_pace"] = _pace_to_mps(threshold_pace, units, "threshold_pace")

    return payload


async def _find_settings(client: ICUClient, sport_id: int) -> SportSettings | None:
    """Look up one sport settings entry; the API has no single-entry GET we use."""
    for settings in await client.get_sport_settings():
        if settings.id == sport_id:
            return settings
    return None


async def get_sport_settings(
    ctx: Context | None = None,
) -> str:
    """Get all sport-specific settings (FTP, threshold HR, threshold pace, zones).

    Returns:
        Formatted list of sport settings with thresholds
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        async with ICUClient(config) as client:
            settings_list = await client.get_sport_settings()

            if not settings_list:
                return ResponseBuilder.build_response(
                    {"message": "No sport settings found"}, metadata={"count": 0}
                )

            return ResponseBuilder.build_response(
                {"sport_settings": [_summarize(s) for s in settings_list]},
                metadata={"count": len(settings_list), "type": "sport_settings_list"},
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), error_type="unexpected_error")


async def update_sport_settings(
    sport_id: Annotated[int | str, "ID of the sport settings to update"],
    ftp: Annotated[int | str | None, "Functional Threshold Power in watts (cycling)"] = None,
    indoor_ftp: Annotated[int | str | None, "Indoor Functional Threshold Power in watts"] = None,
    threshold_hr: Annotated[int | str | None, "Threshold heart rate in bpm (LTHR/FTHR)"] = None,
    max_hr: Annotated[int | str | None, "Maximum heart rate in bpm"] = None,
    threshold_pace: Annotated[
        float | str | None,
        "Threshold pace as 'M:SS' over the sport's pace unit "
        "(e.g. '4:30' for 4:30/km running, '1:45' for 1:45/100m swimming), "
        "or a number of seconds",
    ] = None,
    pace_units: Annotated[
        str | None,
        "Pace unit: MINS_KM, MINS_MILE, SECS_100M, SECS_100Y or SECS_500M",
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Update sport-specific settings (FTP, threshold HR, threshold pace).

    Args:
        sport_id: ID of the sport settings to update (see get_sport_settings)
        ftp: Functional Threshold Power in watts (optional)
        indoor_ftp: Indoor Functional Threshold Power in watts (optional)
        threshold_hr: Threshold heart rate in bpm (optional)
        max_hr: Maximum heart rate in bpm (optional)
        threshold_pace: Threshold pace as 'M:SS' or seconds (optional)
        pace_units: Unit the pace is measured over (optional)

    Returns:
        Updated sport settings
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        sport_id_value = _as_int(sport_id, "sport_id")
        assert sport_id_value is not None

        async with ICUClient(config) as client:
            current = await _find_settings(client, sport_id_value)
            if current is None:
                return ResponseBuilder.build_error_response(
                    f"No sport settings found with id {sport_id_value}",
                    error_type="not_found",
                )

            payload = _build_update_payload(
                ftp,
                indoor_ftp,
                threshold_hr,
                max_hr,
                threshold_pace,
                pace_units,
                current.pace_units,
            )

            if not payload:
                return ResponseBuilder.build_error_response(
                    "No fields provided to update", error_type="validation_error"
                )

            settings = await client.update_sport_settings(sport_id_value, payload)

            return ResponseBuilder.build_response(
                _summarize(settings),
                metadata={
                    "type": "sport_settings_updated",
                    "updated_fields": sorted(payload),
                    "message": "Sport settings updated successfully",
                },
            )

    except SportSettingsInputError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), error_type="unexpected_error")


async def apply_sport_settings(
    sport_id: Annotated[int | str, "ID of the sport settings to apply"],
    ctx: Context | None = None,
) -> str:
    """Apply sport settings (zones, thresholds) to matching activities.

    This recalculates training load, zones, and other derived metrics for the
    activities covered by these settings. The API runs it in the background, so
    the result is not visible immediately.

    Args:
        sport_id: ID of the sport settings to apply

    Returns:
        Result of applying settings
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        sport_id_value = _as_int(sport_id, "sport_id")
        assert sport_id_value is not None

        async with ICUClient(config) as client:
            result = await client.apply_sport_settings(sport_id_value)

            return ResponseBuilder.build_response(
                {"sport_id": sport_id_value, "result": result},
                metadata={
                    "type": "sport_settings_applied",
                    "message": (
                        "Sport settings queued for reprocessing; "
                        "intervals.icu applies them to matching activities in the background"
                    ),
                },
            )

    except SportSettingsInputError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), error_type="unexpected_error")


async def create_sport_settings(
    sport_types: Annotated[
        str | list[str],
        "One or more intervals.icu activity types these settings cover "
        "(e.g. 'Run', or ['Run', 'TrailRun', 'VirtualRun'])",
    ],
    ftp: Annotated[int | str | None, "Functional Threshold Power in watts (cycling)"] = None,
    indoor_ftp: Annotated[int | str | None, "Indoor Functional Threshold Power in watts"] = None,
    threshold_hr: Annotated[int | str | None, "Threshold heart rate in bpm (LTHR/FTHR)"] = None,
    max_hr: Annotated[int | str | None, "Maximum heart rate in bpm"] = None,
    threshold_pace: Annotated[
        float | str | None,
        "Threshold pace as 'M:SS' over the sport's pace unit "
        "(e.g. '4:30' for 4:30/km running, '1:45' for 1:45/100m swimming), "
        "or a number of seconds",
    ] = None,
    pace_units: Annotated[
        str | None,
        "Pace unit: MINS_KM, MINS_MILE, SECS_100M, SECS_100Y or SECS_500M",
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Create new sport-specific settings for one or more activity types.

    Note that intervals.icu creates the entry with the athlete's default values
    first, so any thresholds given here are written in a follow-up update.

    Args:
        sport_types: Activity types the settings apply to
        ftp: Functional Threshold Power in watts (optional)
        indoor_ftp: Indoor Functional Threshold Power in watts (optional)
        threshold_hr: Threshold heart rate in bpm (optional)
        max_hr: Maximum heart rate in bpm (optional)
        threshold_pace: Threshold pace as 'M:SS' or seconds (optional)
        pace_units: Unit the pace is measured over (optional)

    Returns:
        Created sport settings
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        types = _parse_sport_types(sport_types)

        async with ICUClient(config) as client:
            # An activity type belongs to exactly one entry, and the API rejects a
            # duplicate with a message that does not name the entry holding it.
            taken = {t: s.id for s in await client.get_sport_settings() for t in s.types}
            clashes = {t: taken[t] for t in types if t in taken}
            if clashes:
                held = ", ".join(f"{t} (id {sid})" for t, sid in sorted(clashes.items()))
                return ResponseBuilder.build_error_response(
                    f"Already covered by existing sport settings: {held}. "
                    f"Use update_sport_settings on that id to change its thresholds.",
                    error_type="validation_error",
                )

            settings = await client.create_sport_settings({"types": types})

            payload = _build_update_payload(
                ftp,
                indoor_ftp,
                threshold_hr,
                max_hr,
                threshold_pace,
                pace_units,
                settings.pace_units,
            )

            if payload:
                try:
                    settings = await client.update_sport_settings(settings.id, payload)
                except ICUAPIError as e:
                    return ResponseBuilder.build_error_response(
                        f"Sport settings were created with id {settings.id}, but applying the "
                        f"thresholds failed: {e.message}. Retry with update_sport_settings or "
                        f"delete the entry.",
                        error_type="api_error",
                    )

            return ResponseBuilder.build_response(
                _summarize(settings),
                metadata={
                    "type": "sport_settings_created",
                    "message": "Sport settings created successfully",
                },
            )

    except SportSettingsInputError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), error_type="unexpected_error")


async def delete_sport_settings(
    sport_id: Annotated[int | str, "ID of the sport settings to delete"],
    ctx: Context | None = None,
) -> str:
    """Delete sport-specific settings.

    Args:
        sport_id: ID of the sport settings to delete

    Returns:
        Deletion confirmation
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        sport_id_value = _as_int(sport_id, "sport_id")
        assert sport_id_value is not None

        async with ICUClient(config) as client:
            await client.delete_sport_settings(sport_id_value)

            return ResponseBuilder.build_response(
                {"sport_id": sport_id_value, "deleted": True},
                metadata={
                    "type": "sport_settings_deleted",
                    "message": "Sport settings deleted successfully",
                },
            )

    except SportSettingsInputError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), error_type="unexpected_error")
