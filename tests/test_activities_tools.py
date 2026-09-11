"""Tests for the athlete_id parameter on activities tools (R1/R7)."""

import json
from unittest.mock import MagicMock

from httpx import Response

from intervals_icu_mcp.tools.activities import get_recent_activities


class TestGetRecentActivitiesAthleteId:
    """Tests for get_recent_activities' athlete_id parameter."""

    async def test_without_athlete_id_uses_own_athlete(
        self,
        mock_config,
        respx_mock,
        mock_activity_data,
    ):
        """Unchanged behavior (R7): no athlete_id means the caller's own athlete."""
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        respx_mock.get("/athlete/i123456/activities").mock(
            return_value=Response(200, json=[mock_activity_data])
        )

        result = await get_recent_activities(ctx=mock_ctx)

        response = json.loads(result)
        assert response["data"]["count"] == 1
        assert response["data"]["activities"][0]["id"] == "12345"

    async def test_with_athlete_id_queries_foreign_athlete(
        self,
        mock_config,
        respx_mock,
        mock_activity_data,
    ):
        """athlete_id is passed through and the foreign athlete's activities come back."""
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        foreign_activity = {**mock_activity_data, "id": "99999"}
        respx_mock.get("/athlete/i999999/activities").mock(
            return_value=Response(200, json=[foreign_activity])
        )

        result = await get_recent_activities(athlete_id="i999999", ctx=mock_ctx)

        response = json.loads(result)
        assert response["data"]["activities"][0]["id"] == "99999"

    async def test_unauthorized_foreign_athlete_id_does_not_fall_back(
        self,
        mock_config,
        respx_mock,
    ):
        """A 403 on a foreign athlete_id must not silently return the own athlete's data."""
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        respx_mock.get("/athlete/i999999/activities").mock(return_value=Response(403, json={}))

        result = await get_recent_activities(athlete_id="i999999", ctx=mock_ctx)

        response = json.loads(result)
        assert response["error"]["type"] == "forbidden"
        assert "i999999" in response["error"]["message"]
        assert "data" not in response
