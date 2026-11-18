from __future__ import annotations

import pytest

from bench_core.transients import is_transient_sandbox_error


class _Err(Exception):
    pass


@pytest.mark.parametrize(
    "msg",
    [
        "502 Bad Gateway",
        "503 Service Unavailable",
        "504 Gateway Timeout",
        "429 Too Many Requests",
        "rate limit exceeded",
        "context deadline exceeded",
        "Connection reset by peer",
        "Connection refused",
        "aborted",
        "temporarily unavailable",
    ],
)
def test_transient_markers_classify_true(msg):
    assert is_transient_sandbox_error(_Err(msg)) is True


@pytest.mark.parametrize("msg", ["permission denied", "sandbox not found", "invalid template", ""])
def test_non_transient_classify_false(msg):
    assert is_transient_sandbox_error(_Err(msg)) is False


def test_none_is_not_transient():
    assert is_transient_sandbox_error(None) is False
