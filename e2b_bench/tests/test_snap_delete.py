"""Unit tests for e2b_bench.snap.delete logic (SDK mocked)."""

from unittest.mock import patch

from e2b_bench.snap import delete


class TestFilterActiveSnapshots:
    """Tests for load_active_snapshot_ids_from_json."""

    def test_skips_deleted_entries(self):
        data = {
            "template": "uu",
            "count": 3,
            "snapshots": [
                {"snapshot_id": "snap1", "status": "success"},
                {"snapshot_id": "snap2", "status": "deleted"},
                {"snapshot_id": "snap3", "status": "success"},
            ],
        }
        ids = delete.load_active_snapshot_ids_from_json(data)
        assert ids == ["snap1", "snap3"]

    def test_skips_entries_without_snapshot_id(self):
        data = {"snapshots": [{"status": "success"}, {"snapshot_id": "snap1", "status": "success"}]}
        ids = delete.load_active_snapshot_ids_from_json(data)
        assert ids == ["snap1"]

    def test_count_limit_applies(self):
        data = {"snapshots": [{"snapshot_id": f"s{i}", "status": "success"} for i in range(5)]}
        ids = delete.load_active_snapshot_ids_from_json(data, count=2)
        assert ids == ["s0", "s1"]

    def test_empty_returns_empty(self):
        assert delete.load_active_snapshot_ids_from_json({"snapshots": []}) == []


class TestDeleteSingle:
    """Tests for delete_single_snapshot."""

    def _fake_deleted_true(self, snapshot_id, **opts):
        return True

    def _fake_deleted_false(self, snapshot_id, **opts):
        return False

    def _fake_raises(self, snapshot_id, **opts):
        raise RuntimeError("boom")

    def test_success_status(self):
        with patch.object(delete, "Sandbox") as sandbox_cls:
            sandbox_cls.delete_snapshot = self._fake_deleted_true
            r = delete.delete_single_snapshot("snap1", index=1)
        assert r["status"] == "success"
        assert r["snapshot_id"] == "snap1"
        assert r["error"] == ""
        assert r["delete_elapsed_s"] >= 0

    def test_not_found_status(self):
        with patch.object(delete, "Sandbox") as sandbox_cls:
            sandbox_cls.delete_snapshot = self._fake_deleted_false
            r = delete.delete_single_snapshot("snap1", index=1)
        assert r["status"] == "not_found"
        assert r["error"] == ""

    def test_failed_status_captures_error(self):
        with patch.object(delete, "Sandbox") as sandbox_cls:
            sandbox_cls.delete_snapshot = self._fake_raises
            r = delete.delete_single_snapshot("snap1", index=1)
        assert r["status"] == "failed"
        assert "boom" in r["error"]


class TestUpdateJsonLedger:
    """Tests for update_json_ledger."""

    def test_marks_deleted_entries(self):
        data = {
            "snapshots": [
                {"snapshot_id": "snap1", "status": "success"},
                {"snapshot_id": "snap2", "status": "success"},
            ]
        }
        results = [
            {"snapshot_id": "snap1", "status": "success"},
            {"snapshot_id": "snap2", "status": "failed", "error": "x"},
        ]
        updated = delete.update_json_ledger(data, results)
        assert updated["snapshots"][0]["status"] == "deleted"
        assert "deleted_at" in updated["snapshots"][0]
        assert updated["snapshots"][1]["status"] == "delete_failed"
        assert "deleted_at" in updated["snapshots"][1]

    def test_leaves_unmatched_entries_alone(self):
        data = {"snapshots": [{"snapshot_id": "snap9", "status": "success"}]}
        results = [{"snapshot_id": "snap1", "status": "success"}]
        updated = delete.update_json_ledger(data, results)
        assert updated["snapshots"][0]["status"] == "success"
