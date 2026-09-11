"""Tests for auth.parse_known_athletes (R6 configuration)."""

from intervals_icu_mcp.auth import parse_known_athletes


class TestParseKnownAthletes:
    """Tests for parsing INTERVALS_ICU_KNOWN_ATHLETES."""

    def test_empty_string_yields_empty_list(self):
        assert parse_known_athletes("") == []

    def test_single_pair(self):
        assert parse_known_athletes("i186312:Benedikt") == [("i186312", "Benedikt")]

    def test_multiple_pairs(self):
        assert parse_known_athletes("i186312:Benedikt,i222222:Partner") == [
            ("i186312", "Benedikt"),
            ("i222222", "Partner"),
        ]

    def test_whitespace_around_entries_is_stripped(self):
        assert parse_known_athletes(" i186312 : Benedikt , i222222:Partner ") == [
            ("i186312", "Benedikt"),
            ("i222222", "Partner"),
        ]

    def test_missing_label_falls_back_to_id(self):
        assert parse_known_athletes("i186312") == [("i186312", "i186312")]

    def test_blank_entries_are_skipped(self):
        assert parse_known_athletes("i186312:Benedikt,,i222222:Partner") == [
            ("i186312", "Benedikt"),
            ("i222222", "Partner"),
        ]
