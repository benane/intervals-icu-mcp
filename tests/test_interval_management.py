"""Tests for the interval write tools."""

import json
from unittest.mock import MagicMock

from httpx import Response

from intervals_icu_mcp.tools.interval_management import (
    create_intervals,
    mark_climbs_as_intervals,
    replace_intervals,
)


def _ctx(mock_config) -> MagicMock:
    ctx = MagicMock()
    ctx.get_state.return_value = mock_config
    return ctx


def _dto(intervals: list[dict]) -> dict:
    return {"id": "i1", "analyzed": "2026-08-28T00:00:00Z", "icu_intervals": intervals}


def _iv(start: int, end: int, itype: str = "WORK", label: str | None = None) -> dict:
    d = {
        "start_index": start,
        "end_index": end,
        "type": itype,
        "start_time": start,
        "end_time": end,
    }
    if label is not None:
        d["label"] = label
    return d


class TestCreateIntervals:
    async def test_adds_interval_without_touching_existing(self, mock_config, respx_mock):
        existing = [_iv(0, 100, "RECOVERY"), _iv(100, 500)]
        respx_mock.get("/activity/123/intervals").mock(
            return_value=Response(200, json=_dto(existing))
        )
        put = respx_mock.put("/activity/123/intervals").mock(
            return_value=Response(200, json=_dto(existing + [_iv(600, 800, "WORK", "Sprint")]))
        )

        result = await create_intervals(
            activity_id="123",
            intervals=json.dumps([{"start_index": 600, "end_index": 800, "label": "Sprint"}]),
            ctx=_ctx(mock_config),
        )
        response = json.loads(result)

        assert "error" not in response
        assert put.called
        assert put.calls.last.request.url.params["all"] == "false"
        sent = json.loads(put.calls.last.request.content)
        assert sent == [{"start_index": 600, "end_index": 800, "type": "WORK", "label": "Sprint"}]
        assert response["data"]["added"][0]["label"] == "Sprint"
        assert response["metadata"]["added_count"] == 1
        assert "icu_intervals_edited" in response["metadata"]["note"]

    async def test_carves_existing_interval_and_reports_it(self, mock_config, respx_mock):
        respx_mock.get("/activity/123/intervals").mock(
            return_value=Response(200, json=_dto([_iv(0, 500, "RECOVERY", "Effort")]))
        )
        put = respx_mock.put("/activity/123/intervals").mock(
            return_value=Response(
                200,
                json=_dto(
                    [
                        _iv(0, 400, "RECOVERY", "Effort"),
                        _iv(400, 700, "WORK", "Sprint"),
                    ]
                ),
            )
        )

        result = await create_intervals(
            activity_id="123",
            intervals=json.dumps([{"start_index": 400, "end_index": 700, "label": "Sprint"}]),
            ctx=_ctx(mock_config),
        )
        response = json.loads(result)

        assert "error" not in response
        assert put.called
        carved = response["data"]["carved_existing"]
        assert len(carved) == 1
        assert carved[0]["label"] == "Effort"
        assert carved[0]["carved_by"] == ["Sprint"]
        assert response["metadata"]["carved_count"] == 1

    async def test_rejects_overlap_within_request(self, mock_config, respx_mock):
        respx_mock.get("/activity/123/intervals").mock(return_value=Response(200, json=_dto([])))

        result = await create_intervals(
            activity_id="123",
            intervals=json.dumps(
                [
                    {"start_index": 0, "end_index": 300, "label": "A"},
                    {"start_index": 200, "end_index": 500, "label": "B"},
                ]
            ),
            ctx=_ctx(mock_config),
        )
        response = json.loads(result)
        assert response["error"]["type"] == "overlap_error"

    async def test_maps_seconds_to_indices(self, mock_config, respx_mock):
        # time stream: index i -> second i, but with a 100 s pause after index 300
        time_data = list(range(300)) + [t + 100 for t in range(300, 600)]
        respx_mock.get("/activity/123/streams.json").mock(
            return_value=Response(200, json=[{"type": "time", "data": time_data}])
        )
        respx_mock.get("/activity/123/intervals").mock(return_value=Response(200, json=_dto([])))
        put = respx_mock.put("/activity/123/intervals").mock(
            return_value=Response(200, json=_dto([_iv(250, 450, "WORK", "climb")]))
        )

        result = await create_intervals(
            activity_id="123",
            intervals=json.dumps([{"start_time_s": 250, "end_time_s": 550, "label": "climb"}]),
            ctx=_ctx(mock_config),
        )
        response = json.loads(result)

        assert "error" not in response
        sent = json.loads(put.calls.last.request.content)
        # 250 s -> index 250; 550 s -> first index whose timestamp >= 550 == 450
        assert sent[0]["start_index"] == 250
        assert sent[0]["end_index"] == 450

    async def test_rejects_invalid_json(self, mock_config, respx_mock):
        result = await create_intervals(
            activity_id="123", intervals="not json", ctx=_ctx(mock_config)
        )
        assert json.loads(result)["error"]["type"] == "validation_error"

    async def test_rejects_empty_array(self, mock_config, respx_mock):
        result = await create_intervals(activity_id="123", intervals="[]", ctx=_ctx(mock_config))
        assert json.loads(result)["error"]["type"] == "validation_error"

    async def test_rejects_end_before_start(self, mock_config, respx_mock):
        result = await create_intervals(
            activity_id="123",
            intervals=json.dumps([{"start_index": 500, "end_index": 400}]),
            ctx=_ctx(mock_config),
        )
        assert json.loads(result)["error"]["type"] == "validation_error"


class TestReplaceIntervals:
    async def test_replaces_with_all_true(self, mock_config, respx_mock):
        respx_mock.get("/activity/123/intervals").mock(
            return_value=Response(200, json=_dto([_iv(0, 100), _iv(100, 200), _iv(200, 900)]))
        )
        put = respx_mock.put("/activity/123/intervals").mock(
            return_value=Response(200, json=_dto([_iv(10, 90, "WORK", "only one")]))
        )

        result = await replace_intervals(
            activity_id="123",
            intervals=json.dumps([{"start_index": 10, "end_index": 90, "label": "only one"}]),
            ctx=_ctx(mock_config),
        )
        response = json.loads(result)

        assert "error" not in response
        assert put.calls.last.request.url.params["all"] == "true"
        assert response["metadata"]["removed_count"] == 3
        assert response["metadata"]["new_count"] == 1

    async def test_rejects_internal_overlap(self, mock_config, respx_mock):
        put = respx_mock.put("/activity/123/intervals").mock(
            return_value=Response(200, json=_dto([]))
        )
        result = await replace_intervals(
            activity_id="123",
            intervals=json.dumps(
                [
                    {"start_index": 0, "end_index": 300, "label": "A"},
                    {"start_index": 100, "end_index": 200, "label": "B"},
                ]
            ),
            ctx=_ctx(mock_config),
        )
        assert json.loads(result)["error"]["type"] == "overlap_error"
        assert not put.called


def _climb_streams(dx: float = 1.0):
    """distance/altitude/time streams with one obvious ~60 m climb over ~800 m."""
    flat_a = [0.0] * 300
    up = [i * (60.0 / 800.0) for i in range(1, 801)]
    flat_b = [60.0] * 300
    altitude = flat_a + up + flat_b
    distance = [i * dx for i in range(len(altitude))]
    time = list(range(len(altitude)))
    return distance, altitude, time


class TestMarkClimbsAsIntervals:
    async def test_writes_detected_climbs(self, mock_config, respx_mock):
        distance, altitude, time = _climb_streams()
        respx_mock.get("/activity/123/streams.json").mock(
            return_value=Response(
                200,
                json=[
                    {"type": "distance", "data": distance},
                    {"type": "altitude", "data": altitude},
                    {"type": "time", "data": time},
                ],
            )
        )
        respx_mock.get("/activity/123/intervals").mock(return_value=Response(200, json=_dto([])))
        put = respx_mock.put("/activity/123/intervals").mock(
            return_value=Response(
                200, json=_dto([_iv(300, 1100, "WORK", "Anstieg 1: +60hm, 7.5%")])
            )
        )

        result = await mark_climbs_as_intervals(activity_id="123", ctx=_ctx(mock_config))
        response = json.loads(result)

        assert "error" not in response
        assert put.called
        sent = json.loads(put.calls.last.request.content)
        assert len(sent) == 1
        assert sent[0]["type"] == "WORK"
        assert sent[0]["label"].startswith("Anstieg 1: +")
        assert response["analysis"]["written_count"] == 1
        assert response["data"]["climbs_detected"][0]["gain_m"] > 50

    async def test_errors_without_elevation(self, mock_config, respx_mock):
        respx_mock.get("/activity/123/streams.json").mock(
            return_value=Response(
                200,
                json=[
                    {"type": "distance", "data": [0, 1, 2]},
                    {"type": "time", "data": [0, 1, 2]},
                ],
            )
        )

        result = await mark_climbs_as_intervals(activity_id="123", ctx=_ctx(mock_config))
        assert json.loads(result)["error"]["type"] == "validation_error"

    async def test_reports_no_climbs_without_error(self, mock_config, respx_mock):
        flat = [0.0] * 2000
        respx_mock.get("/activity/123/streams.json").mock(
            return_value=Response(
                200,
                json=[
                    {"type": "distance", "data": list(range(2000))},
                    {"type": "altitude", "data": flat},
                    {"type": "time", "data": list(range(2000))},
                ],
            )
        )

        result = await mark_climbs_as_intervals(activity_id="123", ctx=_ctx(mock_config))
        response = json.loads(result)
        assert "error" not in response
        assert response["data"]["climbs_detected"] == []
        assert response["data"]["written"] == []

    async def test_carves_existing_auto_intervals_for_climbs(self, mock_config, respx_mock):
        distance, altitude, time = _climb_streams()
        respx_mock.get("/activity/123/streams.json").mock(
            return_value=Response(
                200,
                json=[
                    {"type": "distance", "data": distance},
                    {"type": "altitude", "data": altitude},
                    {"type": "time", "data": time},
                ],
            )
        )
        # a single auto-detected interval covering the whole ride
        respx_mock.get("/activity/123/intervals").mock(
            return_value=Response(200, json=_dto([_iv(0, 1500, "RECOVERY", None)]))
        )
        put = respx_mock.put("/activity/123/intervals").mock(
            return_value=Response(
                200,
                json=_dto(
                    [
                        _iv(0, 300, "RECOVERY", None),
                        _iv(300, 1100, "WORK", "Anstieg 1: +60hm, 7.5%"),
                        _iv(1100, 1500, "RECOVERY", None),
                    ]
                ),
            )
        )

        result = await mark_climbs_as_intervals(activity_id="123", ctx=_ctx(mock_config))
        response = json.loads(result)

        assert "error" not in response
        assert put.called
        assert response["analysis"]["written_count"] == 1
        assert response["analysis"]["carved_count"] == 1
        assert response["data"]["carved_existing"][0]["start_index"] == 0
