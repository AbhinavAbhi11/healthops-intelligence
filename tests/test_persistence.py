from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from persistence import LocalPersistence


class LocalPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="healthops-storage-test-"
        )
        self.root = Path(self.temp.name)
        self.store = LocalPersistence(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_workspace_round_trip_preserves_frames_and_metadata(self):
        source = pd.DataFrame({
            "provider_id": ["001", "002", None],
            "charges": [100.25, 200.5, None],
            "recorded_at": pd.to_datetime(
                [
                    "2026-01-01T10:00:00Z",
                    "2026-01-02T11:30:00Z",
                    None,
                ],
                utc=True,
            ),
        })
        cleaned = source.iloc[:2].copy()

        state = {
            "tables": {"provider metrics": source},
            "source_tables": {"provider metrics": source.copy()},
            "cleaned": {"provider metrics": cleaned},
            "clean_active": False,
            "last_analysis": {"rows": 3, "goal": "Revenue cycle"},
            "goal": "Revenue cycle",
            "clean_log": ["trim text whitespace"],
            "generation": 7,
            "source_db": None,
            "refreshed_at": "2026-10-29T00:00:00+00:00",
        }

        receipt = self.store.save_workspace(state)
        restored = self.store.load_workspace()

        self.assertIsNotNone(receipt)
        self.assertEqual(restored["goal"], "Revenue cycle")
        self.assertEqual(restored["generation"], 7)
        self.assertEqual(restored["last_analysis"]["rows"], 3)
        self.assertEqual(
            restored["tables"]["provider metrics"]["provider_id"].tolist()[:2],
            ["001", "002"],
        )
        self.assertEqual(
            restored["tables"]["provider metrics"]["charges"].iloc[0],
            100.25,
        )
        self.assertTrue(
            pd.isna(
                restored["tables"]["provider metrics"]
                ["provider_id"].iloc[2]
            )
        )
        self.assertEqual(
            len(restored["source_tables"]["provider metrics"]),
            3,
        )
        self.assertEqual(
            len(restored["cleaned"]["provider metrics"]),
            2,
        )
        self.assertFalse(restored["clean_active"])

    def test_table_snapshots_are_json_not_python_pickle(self):
        self.store.save_workspace({
            "tables": {"sample": pd.DataFrame({"value": [1, 2]})},
            "source_tables": None,
            "cleaned": {},
            "goal": "test",
        })

        files = list(self.root.rglob("*"))
        self.assertTrue(
            any(
                item.suffix == ".json"
                for item in files
                if item.is_file()
            )
        )
        self.assertFalse(
            any(
                item.suffix in {".pkl", ".pickle"}
                for item in files
                if item.is_file()
            )
        )

    def test_latest_complete_snapshot_is_restored(self):
        self.store.save_workspace({
            "tables": {"sample": pd.DataFrame({"value": [1]})},
            "source_tables": None,
            "cleaned": {},
            "goal": "first",
        })
        self.store.save_workspace({
            "tables": {"sample": pd.DataFrame({"value": [2]})},
            "source_tables": None,
            "cleaned": {},
            "goal": "second",
        })

        restored = self.store.load_workspace()
        self.assertEqual(restored["goal"], "second")
        self.assertEqual(
            restored["tables"]["sample"]["value"].tolist(),
            [2],
        )

    def test_jobs_round_trip_and_interrupted_jobs_are_recovered(self):
        self.store.save_job("job_12345", {
            "status": "processing",
            "analysis": None,
            "error": None,
        })
        self.store.save_job("job_done_12345", {
            "status": "complete",
            "analysis": {"rows": 12},
            "error": None,
        })

        self.assertEqual(self.store.recover_interrupted_jobs(), 1)

        interrupted = self.store.get_job("job_12345")
        completed = self.store.get_job("job_done_12345")

        self.assertEqual(interrupted["status"], "error")
        self.assertTrue(interrupted["recovered_after_restart"])
        self.assertIn("server restarted", interrupted["error"].lower())
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["analysis"]["rows"], 12)

    def test_missing_workspace_returns_none(self):
        self.assertIsNone(self.store.load_workspace())

    def test_disabled_store_is_a_noop(self):
        disabled = LocalPersistence(enabled=False)
        self.assertIsNone(disabled.load_workspace())
        self.assertIsNone(disabled.save_workspace({}))
        self.assertIsNone(disabled.get_job("job_12345"))
        self.assertEqual(disabled.recover_interrupted_jobs(), 0)

    def test_invalid_job_identifier_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid job identifier"):
            self.store.save_job("../bad", {"status": "complete"})


if __name__ == "__main__":
    unittest.main(verbosity=2)