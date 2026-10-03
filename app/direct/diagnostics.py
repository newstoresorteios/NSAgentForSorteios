"""Allowlisted API diagnostics, without exception messages or request bodies."""
import re

from openai import APIStatusError


def safe_error_details(exc):
    details = {"error_type": type(exc).__name__}
    if not isinstance(exc, APIStatusError):
        return details
    details["status_code"] = exc.status_code
    request_id = exc.request_id
    if isinstance(request_id, str) and re.fullmatch(r"req_[a-fA-F0-9-]{16,64}", request_id):
        details["request_id"] = request_id
    if isinstance(exc.code, str) and exc.code in {"invalid_value", "invalid_type", "invalid_request_error",
                    "array_above_max_length", "missing_required_parameter",
                    "unsupported_parameter", "rate_limit_exceeded", "insufficient_quota"}:
        details["error_code"] = exc.code
    if isinstance(exc.param, str) and re.fullmatch(
        r"items(?:\[\d{1,2}\])?(?:\.(?:role|content|type))?", exc.param
    ):
        details["error_param"] = exc.param
    return details
