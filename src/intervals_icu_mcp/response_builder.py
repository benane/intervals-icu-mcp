"""Response builder utilities for structured JSON output.

This module provides utilities for building consistent, structured JSON responses
across all MCP tools. All tools return JSON with a standard structure:

{
    "data": {...},           # Main data payload
    "analysis": {...},       # Optional insights and computed metrics
    "metadata": {...}        # Query metadata, timestamps, includes
}
"""

import json
from datetime import datetime
from typing import Any, cast

from .client import ICUAPIError


def athlete_error_message(error: ICUAPIError, athlete_id: str | None) -> tuple[str, str]:
    """Turn a failed athlete-scoped API call into a (message, error_type) pair.

    Only produces an athlete-specific message when the caller explicitly
    requested a foreign athlete (``athlete_id`` is not None) - errors on the
    caller's own athlete keep the existing generic message, since there is no
    foreign id to name.

    Args:
        error: The ICUAPIError raised by the client
        athlete_id: The raw athlete_id argument the caller passed (None if the
            caller used the default/own athlete)

    Returns:
        (message, error_type) tuple
    """
    if athlete_id is not None:
        if error.status_code == 403:
            return (
                f"No access to athlete '{athlete_id}'. It hasn't been shared with "
                "this API key, or the key isn't authorized for it. Call list_athletes "
                "to see which athletes are accessible.",
                "forbidden",
            )
        if error.status_code == 404:
            return f"Athlete '{athlete_id}' does not exist.", "not_found"
        if error.status_code == 401:
            return "API key invalid or not configured correctly.", "unauthorized"

    return error.message, "api_error"


def _convert_datetimes(obj: Any) -> Any:  # type: ignore[misc]
    """Recursively convert datetime objects to ISO strings."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {str(k): _convert_datetimes(v) for k, v in obj.items()}  # type: ignore[misc]
    elif isinstance(obj, list):
        return [_convert_datetimes(item) for item in obj]  # type: ignore[misc]
    return obj


class ResponseBuilder:
    """Builder for standardized JSON responses."""

    @staticmethod
    def format_date_with_day(dt: datetime | str | None) -> dict[str, str] | None:
        """Format a date/datetime with explicit day-of-week information.

        Args:
            dt: datetime object or ISO string or None

        Returns:
            Dict with datetime, date, day_of_week, and formatted string, or None if input is None
        """
        if dt is None:
            return None

        # Parse the datetime if it's a string, otherwise use it directly
        if isinstance(dt, str):
            parsed_dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        else:
            parsed_dt = dt

        return {
            "datetime": dt if isinstance(dt, str) else dt.isoformat(),
            "date": parsed_dt.strftime("%Y-%m-%d"),
            "day_of_week": parsed_dt.strftime("%A"),  # e.g., "Monday"
            "formatted": parsed_dt.strftime(
                "%A, %B %d, %Y at %I:%M %p"
            ),  # e.g., "Monday, October 15, 2025 at 02:30 PM"
        }

    @staticmethod
    def build_response(
        data: dict[str, Any],
        analysis: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        query_type: str | None = None,
    ) -> str:
        """Build standardized JSON response.

        Args:
            data: Main data payload
            analysis: Optional analysis and insights
            metadata: Optional metadata (will be enriched with timestamp)
            query_type: Optional query type for metadata

        Returns:
            JSON string with structure:
            {
                "data": {...},
                "analysis": {...},
                "metadata": {
                    "fetched_at": "ISO timestamp",
                    "query_type": "...",
                    ...
                }
            }
        """
        # Convert datetime objects to ISO strings
        converted_data = cast(dict[str, Any], _convert_datetimes(data))
        converted_analysis: dict[str, Any] | None = None
        if analysis:
            converted_analysis = cast(dict[str, Any], _convert_datetimes(analysis))

        response: dict[str, Any] = {"data": converted_data}

        if converted_analysis:
            response["analysis"] = converted_analysis

        # Build metadata with timestamp
        meta = metadata or {}
        converted_meta = cast(dict[str, Any], _convert_datetimes(meta))
        converted_meta["fetched_at"] = datetime.now().isoformat()
        if query_type:
            converted_meta["query_type"] = query_type

        response["metadata"] = converted_meta

        return json.dumps(response, separators=(",", ":"))

    @staticmethod
    def build_error_response(
        error_message: str,
        error_type: str = "error",
        suggestions: list[str] | None = None,
    ) -> str:
        """Build standardized error response.

        Args:
            error_message: Human-readable error message
            error_type: Type of error (e.g., "not_found", "rate_limit", "validation")
            suggestions: Optional list of suggestions to resolve the error

        Returns:
            JSON string with error structure
        """
        response: dict[str, dict[str, str | list[str]]] = {
            "error": {
                "message": error_message,
                "type": error_type,
                "timestamp": datetime.now().isoformat(),
            }
        }

        if suggestions:
            response["error"]["suggestions"] = suggestions

        return json.dumps(response, separators=(",", ":"))

    @staticmethod
    def build_athlete_error_response(error: ICUAPIError, athlete_id: str | None) -> str:
        """Build an error response for a failed athlete-scoped API call.

        See :func:`athlete_error_message` for how the message is chosen.

        Args:
            error: The ICUAPIError raised by the client
            athlete_id: The raw athlete_id argument the tool received (None if
                the caller used the default/own athlete)

        Returns:
            JSON string with error structure
        """
        message, error_type = athlete_error_message(error, athlete_id)
        return ResponseBuilder.build_error_response(message, error_type=error_type)
