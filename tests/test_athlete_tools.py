"""Tests for athlete tools."""

import json
from unittest.mock import MagicMock

from httpx import Response

from intervals_icu_mcp.tools.athlete import get_athlete_profile, get_fitness_summary, list_athletes


class TestGetAthleteProfile:
    """Tests for get_athlete_profile tool."""

    async def test_get_athlete_profile_success(
        self,
        mock_config,
        respx_mock,
        mock_athlete_data,
    ):
        """Test successful athlete profile retrieval."""
        # Create mock context with config
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        # Mock the API endpoint
        respx_mock.get("/athlete/i123456").mock(return_value=Response(200, json=mock_athlete_data))

        result = await get_athlete_profile(ctx=mock_ctx)

        # Check for JSON response with expected fields
        import json

        response = json.loads(result)
        assert "data" in response
        assert "profile" in response["data"]
        assert response["data"]["profile"]["name"] == "Test Athlete"
        assert response["data"]["profile"]["id"] == "i123456"
        assert response["data"]["profile"]["email"] == "test@example.com"
        assert response["data"]["profile"]["weight_kg"] == 70.0

    async def test_get_athlete_profile_foreign_athlete(
        self,
        mock_config,
        respx_mock,
    ):
        """athlete_id is passed through to the API and the foreign athlete's own data comes back."""
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        foreign_data = {"id": "i999999", "name": "Partner Athlete"}
        respx_mock.get("/athlete/i999999").mock(return_value=Response(200, json=foreign_data))

        result = await get_athlete_profile(athlete_id="i999999", ctx=mock_ctx)

        response = json.loads(result)
        assert response["data"]["profile"]["id"] == "i999999"
        assert response["data"]["profile"]["name"] == "Partner Athlete"

    async def test_get_athlete_profile_unauthorized_foreign_athlete(
        self,
        mock_config,
        respx_mock,
    ):
        """A 403 on a foreign athlete_id must not fall back to the caller's own data."""
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        respx_mock.get("/athlete/i999999").mock(return_value=Response(403, json={}))

        result = await get_athlete_profile(athlete_id="i999999", ctx=mock_ctx)

        response = json.loads(result)
        assert "error" in response
        assert response["error"]["type"] == "forbidden"
        assert "i999999" in response["error"]["message"]
        assert "data" not in response


class TestGetFitnessSummary:
    """Tests for get_fitness_summary tool."""

    async def test_get_fitness_summary_success(
        self,
        mock_config,
        respx_mock,
        mock_athlete_data,
        mock_fitness_wellness_data,
    ):
        """Test successful fitness summary retrieval."""
        # Create mock context with config
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        # Mock the API endpoints
        respx_mock.get("/athlete/i123456").mock(return_value=Response(200, json=mock_athlete_data))
        respx_mock.get("/athlete/i123456/wellness").mock(
            return_value=Response(200, json=mock_fitness_wellness_data)
        )

        result = await get_fitness_summary(ctx=mock_ctx)

        # Check for JSON response with expected fields
        import json

        response = json.loads(result)
        assert "data" in response
        assert "fitness_metrics" in response["data"]
        # Metrics come from the most recent wellness record that has CTL
        assert response["data"]["fitness_metrics"]["ctl"]["value"] == 50.0
        assert response["data"]["fitness_metrics"]["atl"]["value"] == 35.0

    async def test_get_fitness_summary_with_high_ramp_rate(
        self,
        mock_config,
        respx_mock,
        mock_athlete_data,
        mock_fitness_wellness_data,
    ):
        """Test fitness summary with high ramp rate warning."""
        # Create mock context with config
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        # Modify the latest wellness record to have a high ramp rate
        wellness_data = [dict(record) for record in mock_fitness_wellness_data]
        wellness_data[-1]["rampRate"] = 10.0

        respx_mock.get("/athlete/i123456").mock(return_value=Response(200, json=mock_athlete_data))
        respx_mock.get("/athlete/i123456/wellness").mock(
            return_value=Response(200, json=wellness_data)
        )

        result = await get_fitness_summary(ctx=mock_ctx)

        # Check for JSON response with ramp rate analysis
        import json

        response = json.loads(result)
        assert "analysis" in response
        assert "ramp_rate_status" in response["analysis"]
        assert response["analysis"]["ramp_rate_status"] == "high_risk"

    async def test_get_fitness_summary_without_fitness_data(
        self,
        mock_config,
        respx_mock,
        mock_athlete_data,
    ):
        """Test fitness summary when no wellness record carries CTL data."""
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        respx_mock.get("/athlete/i123456").mock(return_value=Response(200, json=mock_athlete_data))
        respx_mock.get("/athlete/i123456/wellness").mock(return_value=Response(200, json=[]))

        result = await get_fitness_summary(ctx=mock_ctx)

        import json

        response = json.loads(result)
        assert response["error"]["type"] == "no_data"

    async def test_get_fitness_summary_foreign_athlete(
        self,
        mock_config,
        respx_mock,
        mock_fitness_wellness_data,
    ):
        """athlete_id is passed through to both the athlete and wellness calls."""
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        foreign_athlete = {"id": "i999999", "name": "Partner Athlete"}
        respx_mock.get("/athlete/i999999").mock(return_value=Response(200, json=foreign_athlete))
        respx_mock.get("/athlete/i999999/wellness").mock(
            return_value=Response(200, json=mock_fitness_wellness_data)
        )

        result = await get_fitness_summary(athlete_id="i999999", ctx=mock_ctx)

        response = json.loads(result)
        assert response["data"]["athlete_name"] == "Partner Athlete"
        assert response["data"]["fitness_metrics"]["ctl"]["value"] == 50.0


class TestListAthletes:
    """Tests for list_athletes tool."""

    async def test_single_athlete_without_known_athletes(
        self,
        mock_config,
        respx_mock,
        mock_athlete_data,
    ):
        """Without INTERVALS_ICU_KNOWN_ATHLETES, exactly one (own) entry is returned."""
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        respx_mock.get("/athlete/i123456").mock(return_value=Response(200, json=mock_athlete_data))

        result = await list_athletes(ctx=mock_ctx)

        response = json.loads(result)
        athletes = response["data"]["athletes"]
        assert len(athletes) == 1
        assert athletes[0]["id"] == "i123456"
        assert athletes[0]["is_own_athlete"] is True
        assert athletes[0]["accessible"] is True

    async def test_known_athlete_accessible(
        self,
        mock_config,
        respx_mock,
        mock_athlete_data,
    ):
        """A configured known athlete that responds 200 is reported as accessible."""
        config = mock_config.model_copy(update={"intervals_icu_known_athletes": "i999999:Partner"})
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = config

        partner_data = {"id": "i999999", "name": "Partner Athlete"}
        respx_mock.get("/athlete/i123456").mock(return_value=Response(200, json=mock_athlete_data))
        respx_mock.get("/athlete/i999999").mock(return_value=Response(200, json=partner_data))

        result = await list_athletes(ctx=mock_ctx)

        response = json.loads(result)
        athletes = {a["id"]: a for a in response["data"]["athletes"]}
        assert len(athletes) == 2
        assert athletes["i999999"]["label"] == "Partner"
        assert athletes["i999999"]["accessible"] is True
        assert athletes["i999999"]["name"] == "Partner Athlete"
        assert athletes["i999999"]["is_own_athlete"] is False

    async def test_known_athlete_not_accessible(
        self,
        mock_config,
        respx_mock,
        mock_athlete_data,
    ):
        """A configured known athlete that responds 403 is reported as inaccessible,
        without leaking the caller's own data into that entry."""
        config = mock_config.model_copy(update={"intervals_icu_known_athletes": "i999999:Partner"})
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = config

        respx_mock.get("/athlete/i123456").mock(return_value=Response(200, json=mock_athlete_data))
        respx_mock.get("/athlete/i999999").mock(return_value=Response(403, json={}))

        result = await list_athletes(ctx=mock_ctx)

        response = json.loads(result)
        athletes = {a["id"]: a for a in response["data"]["athletes"]}
        assert athletes["i999999"]["accessible"] is False
        assert "name" not in athletes["i999999"]
        assert "i999999" in athletes["i999999"]["error"]
