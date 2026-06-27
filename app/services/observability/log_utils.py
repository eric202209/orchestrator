"""Log Sorting and Deduplication Utilities

Provides utilities for sorting, deduplicating, and formatting log entries.
"""

from typing import List, Dict, Any, Optional
from datetime import datetime
import json


def parse_log_timestamp(timestamp_str: str) -> datetime:
    """
    Parse log timestamp string to datetime object

    Args:
        timestamp_str: ISO format timestamp string

    Returns:
        Parsed datetime object
    """
    # Handle various formats
    formats = [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(
                timestamp_str.replace("Z", "+0000").rstrip("+0000"),
                fmt.replace("Z", "").rstrip("."),
            )
        except ValueError:
            continue

    # Fallback: try fromisoformat
    try:
        return datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    except:
        return datetime.utcnow()


def sort_logs(
    logs: List[Dict[str, Any]], order: str = "asc", deduplicate: bool = True
) -> List[Dict[str, Any]]:
    """
    Sort and optionally deduplicate log entries

    Args:
        logs: List of log entries
        order: "asc" for oldest first, "desc" for newest first
        deduplicate: Remove duplicate log entries

    Returns:
        Sorted (and optionally deduplicated) list of logs
    """
    if not logs:
        return logs

    # Sort by timestamp
    logs_sorted = sorted(
        logs,
        key=lambda x: parse_log_timestamp(x.get("timestamp", "")),
        reverse=(order == "desc"),
    )

    if deduplicate:
        logs_sorted = deduplicate_logs(logs_sorted)

    return logs_sorted


def deduplicate_logs(logs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Remove duplicate log entries based on timestamp and message

    Args:
        logs: List of log entries

    Returns:
        Deduplicated list of logs
    """
    seen = set()
    unique_logs = []

    for log in logs:
        # Create a unique key from timestamp and message
        key = (log.get("timestamp", ""), log.get("message", ""), log.get("level", ""))

        if key not in seen:
            seen.add(key)
            unique_logs.append(log)

    return unique_logs


def format_log_entry(log: Dict[str, Any], include_time: bool = True) -> str:
    """
    Format a single log entry for display

    Args:
        log: Log entry dictionary
        include_time: Include timestamp in output

    Returns:
        Formatted log string
    """
    timestamp = log.get("timestamp", "")
    level = log.get("level", "INFO")
    message = log.get("message", "")

    if include_time:
        try:
            dt = parse_log_timestamp(timestamp)
            time_str = dt.strftime("%m/%d/%Y, %I:%M:%S %p")
        except:
            time_str = timestamp[:19] if len(timestamp) > 19 else timestamp

        return f"{time_str}[{level}]{message}"
    else:
        return f"[{level}]{message}"


def format_logs_batch(
    logs: List[Dict[str, Any]],
    order: str = "asc",
    deduplicate: bool = True,
    include_time: bool = True,
) -> str:
    """
    Format multiple log entries for display

    Args:
        logs: List of log entries
        order: "asc" or "desc"
        deduplicate: Remove duplicates
        include_time: Include timestamps

    Returns:
        Formatted log string (one per line)
    """
    sorted_logs = sort_logs(logs, order=order, deduplicate=deduplicate)

    return "\n".join(
        format_log_entry(log, include_time=include_time) for log in sorted_logs
    )


def group_logs_by_session(
    logs: List[Dict[str, Any]],
) -> Dict[int, List[Dict[str, Any]]]:
    """
    Group logs by session ID

    Args:
        logs: List of log entries

    Returns:
        Dictionary mapping session_id to list of logs
    """
    grouped = {}

    for log in logs:
        session_id = log.get("session_id")
        if session_id:
            if session_id not in grouped:
                grouped[session_id] = []
            grouped[session_id].append(log)

    return grouped


def get_log_summary(logs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Generate summary statistics for logs

    Args:
        logs: List of log entries

    Returns:
        Summary dictionary with counts and time range
    """
    if not logs:
        return {"total": 0, "by_level": {}, "time_range": None}

    # Count by level
    by_level = {}
    for log in logs:
        level = log.get("level", "UNKNOWN")
        by_level[level] = by_level.get(level, 0) + 1

    # Time range
    timestamps = [parse_log_timestamp(log.get("timestamp", "")) for log in logs]
    time_range = {
        "earliest": min(timestamps).isoformat(),
        "latest": max(timestamps).isoformat(),
    }

    return {"total": len(logs), "by_level": by_level, "time_range": time_range}


def filter_logs_by_level(
    logs: List[Dict[str, Any]], levels: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    Filter logs by log level

    Args:
        logs: List of log entries
        levels: List of levels to include (e.g., ["INFO", "ERROR"])

    Returns:
        Filtered list of logs
    """
    if not levels:
        return logs

    return [log for log in logs if log.get("level") in levels]


def search_logs(
    logs: List[Dict[str, Any]], query: str, case_sensitive: bool = False
) -> List[Dict[str, Any]]:
    """
    Search logs for matching text

    Args:
        logs: List of log entries
        query: Search query
        case_sensitive: Whether search is case-sensitive

    Returns:
        Matching log entries
    """
    if not query:
        return logs

    search_func = str.startswith if case_sensitive else str.lower.startswith
    query_func = query if case_sensitive else query.lower()

    if case_sensitive:
        return [log for log in logs if query_func in log.get("message", "")]
    else:
        return [log for log in logs if query_func in log.get("message", "").lower()]
