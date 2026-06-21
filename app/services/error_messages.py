"""User-facing error copy (T174, 2026-06-20).

Single source of truth for messages shown to end users when processing fails.
NEVER expose raw upstream/exception text (httpx 530, tracebacks, HTTP codes) to
users — Tony's 2026 incident was a raw `str(exc)` ("Server error '530 <none>'
for url ...") passed straight through. Map every failure to one of these.
"""
from __future__ import annotations

# Upstream inference unavailable (tunnel 530, 5xx, timeout, connection refused).
USER_FACING_UPSTREAM_MSG = (
    "Our restoration service is experiencing high demand right now. "
    "Please wait a moment and try again."
)

# Any other processing failure — still no raw text.
GENERIC_PROCESSING_MSG = (
    "Something went wrong while processing your photo. Please try again in a moment."
)

# Substrings that indicate a raw/leaky error string slipped through. Used by the
# user-boundary sanitizer as a belt-and-suspenders guard.
_RAW_LEAK_SIGNATURES = (
    "server error", "for url", "httpx", "httpstatuserror", "traceback",
    "connection", "timeout", "errno", "connecterror", "readtimeout",
    "530", "502", "503", "504", "520", "521", "522", "524",
    "exception", "unexpected error", "upstream",
)


def to_user_message(raw: str | None, error_code: str | None = None) -> str:
    """Map a backend error/error_code to safe user-facing copy.

    - error_code == 'upstream_unavailable' -> high-demand message
    - raw text matching a leak signature   -> generic message (never the raw)
    - empty                                -> generic message
    - otherwise (already clean copy)       -> returned as-is
    """
    if error_code == "upstream_unavailable":
        return USER_FACING_UPSTREAM_MSG
    if not raw:
        return GENERIC_PROCESSING_MSG
    low = str(raw).lower()
    if any(sig in low for sig in _RAW_LEAK_SIGNATURES):
        return GENERIC_PROCESSING_MSG
    return str(raw)
