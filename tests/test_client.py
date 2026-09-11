"""Tests for ICUClient athlete id normalization."""

from intervals_icu_mcp.client import ICUClient


class TestResolveAthleteId:
    """Tests for ICUClient._resolve_athlete_id (R2 normalization)."""

    def test_already_prefixed_id_is_unchanged(self, mock_config):
        client = ICUClient(mock_config)
        assert client._resolve_athlete_id("i186312") == "i186312"

    def test_bare_number_gets_i_prefix(self, mock_config):
        client = ICUClient(mock_config)
        assert client._resolve_athlete_id("186312") == "i186312"

    def test_none_falls_back_to_own_athlete(self, mock_config):
        client = ICUClient(mock_config)
        assert client._resolve_athlete_id(None) == mock_config.intervals_icu_athlete_id

    def test_empty_string_falls_back_to_own_athlete(self, mock_config):
        client = ICUClient(mock_config)
        assert client._resolve_athlete_id("") == mock_config.intervals_icu_athlete_id

    def test_zero_falls_back_to_own_athlete(self, mock_config):
        client = ICUClient(mock_config)
        assert client._resolve_athlete_id("0") == mock_config.intervals_icu_athlete_id

    def test_me_falls_back_to_own_athlete(self, mock_config):
        client = ICUClient(mock_config)
        assert client._resolve_athlete_id("me") == mock_config.intervals_icu_athlete_id
        assert client._resolve_athlete_id("ME") == mock_config.intervals_icu_athlete_id
