"""Incremental BatchScheduler coverage for the Document workflow."""

from e2b_bench.batch_scheduler import GroupRunner


def test_document_batch_delta_succeeds_when_new_tasks_all_pass():
    success, error = GroupRunner._evaluate_document_task_delta((10, 2), (13, 2))

    assert success is True
    assert error is None


def test_document_batch_delta_fails_when_no_task_runs():
    success, error = GroupRunner._evaluate_document_task_delta((10, 2), (10, 2))

    assert success is False
    assert "without executing" in error


def test_document_batch_delta_fails_when_a_new_task_fails():
    success, error = GroupRunner._evaluate_document_task_delta((10, 2), (13, 3))

    assert success is False
    assert "1/3 failed" in error


def test_document_batch_delta_ignores_failures_from_previous_tasks():
    success, error = GroupRunner._evaluate_document_task_delta((10, 2), (12, 2))

    assert success is True
    assert error is None
