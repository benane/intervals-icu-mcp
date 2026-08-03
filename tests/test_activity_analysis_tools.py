"""Tests for activity analysis tools (streams, intervals)."""

import json
from unittest.mock import MagicMock

from httpx import Response

from intervals_icu_mcp.tools.activity_analysis import (
    get_activity_intervals,
    get_activity_streams,
)


def _mock_ctx(mock_config) -> MagicMock:
    mock_ctx = MagicMock()
    mock_ctx.get_state.return_value = mock_config
    return mock_ctx


class TestGetActivityStreams:
    """Tests for get_activity_streams tool."""

    async def test_streams_as_list_filters_request(self, mock_config, respx_mock):
        """A real list of stream types should be forwarded as-is to the API."""
        route = respx_mock.get("/activity/12345/streams.json").mock(
            return_value=Response(
                200,
                json=[
                    {"type": "watts", "data": [100, 110, 120]},
                    {"type": "heartrate", "data": [140, 141, 142]},
                ],
            )
        )

        result = await get_activity_streams(
            activity_id="12345",
            streams=["watts", "heartrate"],
            ctx=_mock_ctx(mock_config),
        )

        assert route.calls.last.request.url.params["types"] == "watts,heartrate"

        response = json.loads(result)
        assert set(response["data"]["available_streams"]) == {"watts", "heartrate"}
        assert response["data"]["streams"]["watts"] == [100, 110, 120]

    async def test_streams_as_comma_string_is_accepted(self, mock_config, respx_mock):
        """A comma-separated string should be split and forwarded like a list."""
        route = respx_mock.get("/activity/12345/streams.json").mock(
            return_value=Response(200, json=[{"type": "watts", "data": [100]}])
        )

        result = await get_activity_streams(
            activity_id="12345",
            streams="watts,heartrate,cadence",
            ctx=_mock_ctx(mock_config),
        )

        assert route.calls.last.request.url.params["types"] == "watts,heartrate,cadence"
        response = json.loads(result)
        assert "error" not in response

    async def test_streams_as_python_repr_string_is_accepted(self, mock_config, respx_mock):
        """Some clients stringify a list argument, e.g. \"['watts', 'heartrate']\"."""
        route = respx_mock.get("/activity/12345/streams.json").mock(
            return_value=Response(200, json=[{"type": "watts", "data": [100]}])
        )

        result = await get_activity_streams(
            activity_id="12345",
            streams="['watts', 'heartrate']",
            ctx=_mock_ctx(mock_config),
        )

        assert route.calls.last.request.url.params["types"] == "watts,heartrate"
        response = json.loads(result)
        assert "error" not in response

    async def test_unknown_stream_type_returns_validation_error(self, mock_config, respx_mock):
        """An unknown stream type should fail fast with a helpful error, not hit the API."""
        route = respx_mock.get("/activity/12345/streams.json").mock(
            return_value=Response(200, json=[])
        )

        result = await get_activity_streams(
            activity_id="12345",
            streams=["not_a_real_stream"],
            ctx=_mock_ctx(mock_config),
        )

        response = json.loads(result)
        assert response["error"]["type"] == "validation_error"
        assert "not_a_real_stream" in response["error"]["message"]
        assert not route.calls


class TestGetActivityIntervals:
    """Tests for get_activity_intervals tool."""

    async def test_intervals_expose_min_max_and_decoupling(self, mock_config, respx_mock):
        """min_heartrate/min_watts should surface, and decoupling only on WORK intervals."""
        respx_mock.get("/activity/12345/intervals").mock(
            return_value=Response(
                200,
                json={
                    "icu_intervals": [
                        {
                            "id": 1,
                            "type": "WORK",
                            "average_watts": 250,
                            "max_watts": 300,
                            "min_watts": 200,
                            "average_heartrate": 150,
                            "max_heartrate": 160,
                            "min_heartrate": 130,
                            "decoupling": 3.2,
                        },
                        {
                            "id": 2,
                            "type": "RECOVERY",
                            "average_heartrate": 120,
                            "max_heartrate": 140,
                            "min_heartrate": 95,
                            "decoupling": 5.0,
                        },
                    ]
                },
            )
        )

        result = await get_activity_intervals(activity_id="12345", ctx=_mock_ctx(mock_config))
        response = json.loads(result)

        work = response["data"]["intervals"][0]
        assert work["performance"]["min_heartrate"] == 130
        assert work["performance"]["min_watts"] == 200
        assert work["performance"]["max_watts"] == 300
        assert work["performance"]["decoupling_percent"] == 3.2

        recovery = response["data"]["intervals"][1]
        assert recovery["performance"]["min_heartrate"] == 95
        # decoupling is only meaningful/reported for WORK intervals
        assert "decoupling_percent" not in recovery["performance"]
