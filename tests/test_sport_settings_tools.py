"""Tests for sport settings tools."""

import json
from unittest.mock import MagicMock

import pytest
from httpx import Response

from intervals_icu_mcp.tools.sport_settings import (
    SportSettingsInputError,
    _as_int,
    _build_update_payload,
    _pace_to_mps,
    create_sport_settings,
    format_pace,
    get_sport_settings,
    update_sport_settings,
)


@pytest.fixture
def mock_ctx(mock_config):
    """Context stub that hands tools the mock config."""
    ctx = MagicMock()
    ctx.get_state.return_value = mock_config
    return ctx


@pytest.fixture
def run_settings():
    """A running sport settings entry as the API returns it."""
    return {
        "id": 42,
        "types": ["Run", "TrailRun"],
        "ftp": None,
        "lthr": 173,
        "max_hr": 191,
        "threshold_pace": 3.7037037,  # 4:30 /km
        "pace_units": "MINS_KM",
    }


class TestPaceConversion:
    """threshold_pace is stored in meters per second, not min/km."""

    @pytest.mark.parametrize(
        ("value", "units", "expected_mps"),
        [
            ("4:30", "MINS_KM", 1000 / 270),
            (270, "MINS_KM", 1000 / 270),
            ("1:45", "SECS_100M", 100 / 105),
            ("2:00", "SECS_500M", 500 / 120),
            ("7:15", "MINS_MILE", 1609.344 / 435),
        ],
    )
    def test_pace_to_mps(self, value, units, expected_mps):
        assert _pace_to_mps(value, units, "threshold_pace") == pytest.approx(expected_mps)

    def test_pace_round_trips_through_formatting(self):
        mps = _pace_to_mps("4:30", "MINS_KM", "threshold_pace")
        assert format_pace(mps, "MINS_KM") == "4:30 /km"

    def test_swim_pace_uses_hundred_meters(self):
        assert format_pace(0.8333333, "SECS_100M") == "2:00 /100m"

    def test_missing_units_fall_back_to_km(self):
        assert format_pace(1000 / 300, None) == "5:00 /km"

    @pytest.mark.parametrize("value", ["", "abc", "4:xx", 0, -5])
    def test_invalid_pace_is_rejected(self, value):
        with pytest.raises(SportSettingsInputError):
            _pace_to_mps(value, "MINS_KM", "threshold_pace")


class TestIntCoercion:
    """Some MCP clients send numbers as JSON strings."""

    @pytest.mark.parametrize(("value", "expected"), [(170, 170), ("170", 170), ("170.0", 170)])
    def test_numeric_strings_are_accepted(self, value, expected):
        assert _as_int(value, "fthr") == expected

    def test_none_stays_none(self):
        assert _as_int(None, "ftp") is None

    @pytest.mark.parametrize("value", ["abc", "", True])
    def test_non_numeric_is_rejected(self, value):
        with pytest.raises(SportSettingsInputError):
            _as_int(value, "ftp")


class TestUpdatePayload:
    """The payload must use the field names the intervals.icu API expects."""

    def test_maps_tool_arguments_to_api_fields(self):
        payload = _build_update_payload(
            ftp="250",
            indoor_ftp=None,
            threshold_hr="170",
            max_hr=195,
            threshold_pace="4:30",
            pace_units=None,
            current_pace_units="MINS_KM",
        )

        assert payload["ftp"] == 250
        assert payload["lthr"] == 170
        assert payload["max_hr"] == 195
        assert payload["threshold_pace"] == pytest.approx(1000 / 270)
        assert "fthr" not in payload
        assert "pace_threshold" not in payload

    def test_explicit_units_win_over_current_units(self):
        payload = _build_update_payload(None, None, None, None, "1:45", "SECS_100M", "MINS_KM")
        assert payload["threshold_pace"] == pytest.approx(100 / 105)
        assert payload["pace_units"] == "SECS_100M"

    def test_unknown_pace_units_are_rejected(self):
        with pytest.raises(SportSettingsInputError):
            _build_update_payload(None, None, None, None, None, "MINS_FURLONG", None)

    def test_omitted_fields_are_left_out(self):
        assert _build_update_payload(None, None, None, None, None, None, "MINS_KM") == {}


class TestGetSportSettings:
    async def test_reports_types_and_thresholds(self, mock_ctx, respx_mock, run_settings):
        respx_mock.get("/athlete/i123456/sport-settings").mock(
            return_value=Response(200, json=[run_settings])
        )

        entry = json.loads(await get_sport_settings(ctx=mock_ctx))["data"]["sport_settings"][0]

        assert entry["types"] == ["Run", "TrailRun"]
        assert entry["threshold_hr_bpm"] == 173
        assert entry["max_hr_bpm"] == 191
        assert entry["threshold_pace"] == "4:30 /km"


class TestUpdateSportSettings:
    async def test_sends_api_field_names(self, mock_ctx, respx_mock, run_settings):
        respx_mock.get("/athlete/i123456/sport-settings").mock(
            return_value=Response(200, json=[run_settings])
        )
        route = respx_mock.put("/athlete/i123456/sport-settings/42").mock(
            return_value=Response(200, json={**run_settings, "lthr": 170})
        )

        result = json.loads(
            await update_sport_settings(sport_id="42", threshold_hr="170", ctx=mock_ctx)
        )

        assert json.loads(route.calls.last.request.content) == {"lthr": 170}
        assert result["data"]["threshold_hr_bpm"] == 170

    async def test_unknown_id_reports_not_found(self, mock_ctx, respx_mock, run_settings):
        respx_mock.get("/athlete/i123456/sport-settings").mock(
            return_value=Response(200, json=[run_settings])
        )

        result = json.loads(await update_sport_settings(sport_id=999, ftp=250, ctx=mock_ctx))

        assert result["error"]["type"] == "not_found"

    async def test_no_fields_reports_validation_error(self, mock_ctx, respx_mock, run_settings):
        respx_mock.get("/athlete/i123456/sport-settings").mock(
            return_value=Response(200, json=[run_settings])
        )

        result = json.loads(await update_sport_settings(sport_id=42, ctx=mock_ctx))

        assert result["error"]["type"] == "validation_error"


class TestCreateSportSettings:
    async def test_posts_types_array_then_applies_values(self, mock_ctx, respx_mock, run_settings):
        created = {**run_settings, "lthr": 173, "threshold_pace": None}
        post = respx_mock.post("/athlete/i123456/sport-settings").mock(
            return_value=Response(200, json=created)
        )
        put = respx_mock.put("/athlete/i123456/sport-settings/42").mock(
            return_value=Response(200, json=run_settings)
        )

        result = json.loads(
            await create_sport_settings(
                sport_types="Run", threshold_hr="170", threshold_pace="4:30", ctx=mock_ctx
            )
        )

        # The API rejects a create without "types" ("Missing types") and ignores
        # any thresholds sent alongside, so values go out in a follow-up update.
        assert json.loads(post.calls.last.request.content) == {"types": ["Run"]}
        put_body = json.loads(put.calls.last.request.content)
        assert put_body["lthr"] == 170
        assert put_body["threshold_pace"] == pytest.approx(1000 / 270)
        assert result["data"]["types"] == ["Run", "TrailRun"]

    async def test_accepts_a_list_of_types(self, mock_ctx, respx_mock, run_settings):
        post = respx_mock.post("/athlete/i123456/sport-settings").mock(
            return_value=Response(200, json=run_settings)
        )

        await create_sport_settings(sport_types=["Run", "TrailRun"], ctx=mock_ctx)

        assert json.loads(post.calls.last.request.content) == {"types": ["Run", "TrailRun"]}

    async def test_skips_update_when_no_values_given(self, mock_ctx, respx_mock, run_settings):
        respx_mock.post("/athlete/i123456/sport-settings").mock(
            return_value=Response(200, json=run_settings)
        )
        put = respx_mock.put("/athlete/i123456/sport-settings/42")

        await create_sport_settings(sport_types="Run", ctx=mock_ctx)

        assert not put.called

    async def test_reports_id_when_follow_up_update_fails(self, mock_ctx, respx_mock, run_settings):
        respx_mock.post("/athlete/i123456/sport-settings").mock(
            return_value=Response(200, json=run_settings)
        )
        respx_mock.put("/athlete/i123456/sport-settings/42").mock(
            return_value=Response(422, json={"error": "nope"})
        )

        result = json.loads(await create_sport_settings(sport_types="Run", ftp=250, ctx=mock_ctx))

        assert result["error"]["type"] == "api_error"
        assert "42" in result["error"]["message"]
