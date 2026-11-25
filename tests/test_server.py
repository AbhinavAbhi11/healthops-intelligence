
from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import server


class FileParsingTests(unittest.TestCase):
    def test_csv_and_tsv_are_loaded_as_tables(self):
        csv_tables = server.read_bytes(
            "providers.csv",
            b"city,charges\nAustin,100\nBoston,200\n",
        )
        tsv_tables = server.read_bytes(
            "providers.tsv",
            b"city\tcharges\nAustin\t100\nBoston\t200\n",
        )

        self.assertEqual(list(csv_tables), ["providers"])
        self.assertEqual(list(tsv_tables), ["providers"])
        self.assertEqual(csv_tables["providers"].shape, (2, 2))
        self.assertEqual(tsv_tables["providers"].iloc[1]["city"], "Boston")

    def test_json_array_and_named_arrays(self):
        array = server.read_bytes(
            "items.json",
            b'[{"id":1},{"id":2}]',
        )
        named = server.read_bytes(
            "export.json",
            b'{"patients":[{"id":"p1"}],"visits":[{"id":"v1"}]}',
        )

        self.assertEqual(list(array), ["items"])
        self.assertEqual(len(array["items"]), 2)
        self.assertEqual(set(named), {"patients", "visits"})

    def test_jsonl_is_loaded(self):
        tables = server.read_bytes(
            "events.ndjson",
            b'{"id":1}\n{"id":2}\n',
        )
        self.assertEqual(len(tables["events"]), 2)

    def test_unsupported_format_has_actionable_error(self):
        with self.assertRaisesRegex(ValueError, "Unsupported format"):
            server.read_bytes("clinical.pdf", b"not a PDF")

    def test_zip_bundle_loads_multiple_supported_members(self):
        data = io.BytesIO()

        with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "providers.csv",
                "city,charges\nAustin,100\n",
            )
            archive.writestr(
                "claims.json",
                '[{"claim_id":"c1"}]',
            )
            archive.writestr("notes.txt", "ignored")

        tables = server.load_upload("bundle.zip", data.getvalue())

        self.assertEqual(len(tables), 2)
        self.assertTrue(any("providers" in name for name in tables))
        self.assertTrue(any("claims" in name for name in tables))

    def test_zip_traversal_path_is_rejected(self):
        data = io.BytesIO()

        with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("../escape.csv", "a\n1\n")

        with self.assertRaisesRegex(ValueError, "unsafe path"):
            server.load_upload("bundle.zip", data.getvalue())

    def test_upload_limit_is_enforced(self):
        with patch.object(server, "MAX_UPLOAD", 4):
            with self.assertRaisesRegex(ValueError, "up to 0 MB"):
                server.load_upload("tiny.csv", b"012345")


class SQLiteConnectorTests(unittest.TestCase):
    def test_sqlite_is_read_as_read_only_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.db"

            # A sqlite3.Connection context manager handles transactions,
            # but does not automatically close the connection.
            # Close it explicitly so Windows can remove the database file.
            connection = sqlite3.connect(path)
            try:
                with connection:
                    connection.execute(
                        "CREATE TABLE providers "
                        "(provider_id TEXT, city TEXT)"
                    )
                    connection.execute(
                        "INSERT INTO providers VALUES ('001', 'Austin')"
                    )
            finally:
                connection.close()

            tables = server.read_sqlite_tables(str(path))

            self.assertEqual(list(tables), ["providers"])
            self.assertEqual(
                tables["providers"].iloc[0]["provider_id"],
                "001",
            )

            # Verify that the connector did not modify the source database.
            connection = sqlite3.connect(path)
            try:
                row_count = connection.execute(
                    "SELECT COUNT(*) FROM providers"
                ).fetchone()[0]
            finally:
                connection.close()

            self.assertEqual(row_count, 1)

    def test_missing_sqlite_path_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "existing local SQLite"):
            server.read_sqlite_tables(
                "/not/a/real/path/healthops.db"
            )


class AnalyticsRegressionTests(unittest.TestCase):
    def setUp(self):
        self.previous_session = dict(server.SESSION)

        server.SESSION.update({
            "tables": {
                "provider_metrics": pd.DataFrame({
                    "Provider City": [
                        "Austin",
                        "Austin",
                        "Boston",
                        None,
                    ],
                    "Total Charges": [
                        100.0,
                        200.0,
                        150.0,
                        125.0,
                    ],
                    "Provider Id": [
                        "001",
                        "002",
                        "003",
                        "004",
                    ],
                })
            },
            "source_tables": None,
            "cleaned": {},
            "clean_active": False,
            "last_analysis": None,
            "goal": "Revenue, billing and insurance",
            "clean_log": [],
            "generation": 1,
            "source_db": None,
            "refreshed_at": "2026-10-29T00:00:00+00:00",
        })

        server.SESSION.pop("_analysis_static", None)

        self.analysis = server.build_analysis(
            "Revenue, billing and insurance"
        )
        server.SESSION["last_analysis"] = self.analysis

    def tearDown(self):
        server.SESSION.clear()
        server.SESSION.update(self.previous_session)

    def test_profile_captures_dimensions_missingness_and_identifiers(self):
        table = self.analysis["tables"][0]
        columns = {
            column["name"]: column
            for column in table["columns"]
        }

        self.assertEqual(table["rows"], 4)
        self.assertEqual(columns["Provider City"]["missing"], 1)

        # Provider IDs are identifiers, not numerical measures.
        self.assertNotIn(
            "Provider Id",
            self.analysis["visual_fields"][0]["measures"],
        )

    def test_distinct_city_question_returns_exact_count_and_excludes_null(self):
        result = server.chat_answer(
            "What are the total recorded cities?",
            self.analysis,
        )

        self.assertIn("2", result["answer"])
        self.assertIn("distinct", result["answer"].lower())
        self.assertIn(
            "1 missing values excluded",
            result["answer"],
        )

    def test_average_field_named_total_charges_is_not_misread_as_sum(self):
        result = server.chat_answer(
            "What is the average of Total Charges?",
            self.analysis,
        )

        self.assertIn("average Total Charges", result["answer"])
        self.assertIn("143.75", result["answer"])
        self.assertNotIn("sum Total Charges", result["answer"])

    def test_explicit_total_of_total_charges_is_a_sum(self):
        result = server.chat_answer(
            "What is the total of Total Charges?",
            self.analysis,
        )

        self.assertIn("sum Total Charges", result["answer"])
        self.assertIn("575", result["answer"])

    def test_summary_answers_are_grounded_in_loaded_data(self):
        result = server.chat_answer(
            "Summarize the analysis",
            self.analysis,
        )

        self.assertIn("4 rows", result["answer"])
        self.assertIn("1 missing cells", result["answer"])

    def test_unsupported_question_does_not_fabricate_an_answer(self):
        result = server.chat_answer(
            "Will this hospital reduce costs by 20% next year?",
            self.analysis,
        )

        self.assertIn("cannot reliably predict", result["answer"].lower())
        self.assertIn("20% reduction is a target", result["answer"].lower())
        self.assertEqual(
            result["query_plan"]["operation"],
            "unsupported_forecast",
        )
        self.assertFalse(result["executed"])

    def test_visualization_uses_selected_live_fields(self):
        result = server.build_visualization({
            "table": "provider_metrics",
            "dimension": "Provider City",
            "measure": "Total Charges",
            "aggregation": "mean",
            "visual_type": "bar",
        })["visualization"]

        self.assertEqual(
            set(result["labels"]),
            {"Austin", "Boston", "Unknown"},
        )
        self.assertEqual(len(result["values"]), 3)

    def test_category_share_recommendation_uses_its_own_table_denominator(self):
        server.SESSION["tables"]["other_table"] = pd.DataFrame({
            "Department": ["ER"] * 20
        })

        server.SESSION.pop("_analysis_static", None)

        analysis = server.build_analysis(
            "Hospital operations and patient flow"
        )

        recommendation = next(
            item["text"]
            for item in analysis["recommendations"]
            if item["category"] == "OPERATIONS & PATIENT FLOW"
        )

        self.assertIn(
            "2 of 4 records in provider_metrics (50.00%)",
            recommendation,
        )
        self.assertNotIn("2 of 24 records", recommendation)

    def test_analytical_number_formatting_hides_float_representation_noise(self):
        self.assertEqual(
            server.format_metric(147441.33399999997),
            "147,441.33",
        )
        self.assertEqual(server.format_metric(575.0), "575")
        self.assertEqual(server.format_metric(float("nan")), "—")

    def test_visualization_rejects_unavailable_fields(self):
        with self.assertRaisesRegex(ValueError, "no longer available"):
            server.build_visualization({
                "table": "provider_metrics",
                "dimension": "Not a field",
                "measure": "Total Charges",
                "aggregation": "mean",
                "visual_type": "bar",
            })


class HttpSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            server.Handler,
        )

        cls.thread = threading.Thread(
            target=cls.httpd.serve_forever,
            daemon=True,
        )
        cls.thread.start()

        cls.base = (
            f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        )

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.previous_session = dict(server.SESSION)

    def tearDown(self):
        server.SESSION.clear()
        server.SESSION.update(self.previous_session)

    def get(self, path):
        with urllib.request.urlopen(
            self.base + path,
            timeout=3,
        ) as response:
            return (
                response.status,
                response.headers,
                response.read(),
            )

    def post(self, path, body, content_type="application/json"):
        request = urllib.request.Request(
            self.base + path,
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )

        with urllib.request.urlopen(
            request,
            timeout=5,
        ) as response:
            return (
                response.status,
                response.headers,
                response.read(),
            )

    def test_health_endpoint(self):
        status, headers, body = self.get("/api/health")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")
        self.assertEqual(
            headers.get("X-Content-Type-Options"),
            "nosniff",
        )

    def test_html_references_external_assets_and_branding_is_rendered(self):
        status, headers, body = self.get("/")
        text = body.decode("utf-8")

        self.assertEqual(status, 200)
        self.assertIn("HealthOps Intelligence", text)
        self.assertIn("/static/css/app.css", text)
        self.assertIn("/static/js/app.js", text)
        self.assertNotIn("<style", text.lower())
        self.assertNotIn("<script>", text.lower())
        self.assertNotIn("{{PRODUCT_NAME}}", text)
        self.assertIn(
            "script-src 'self'",
            headers.get("Content-Security-Policy", ""),
        )

    def test_external_assets_are_served_with_correct_content_types(self):
        _, css_headers, css = self.get("/static/css/app.css")
        _, js_headers, js = self.get("/static/js/app.js")

        self.assertIn(
            "text/css",
            css_headers.get("Content-Type", ""),
        )
        self.assertIn(
            "javascript",
            js_headers.get("Content-Type", ""),
        )
        self.assertIn(b"--surface-0", css)
        self.assertIn(b"fmtNumber", js)

    def test_upload_analysis_and_chat_work_end_to_end(self):
        boundary = "----healthops-test-boundary"

        pieces = [
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="files"; '
                'filename="providers.csv"\r\n'
                "Content-Type: text/csv\r\n\r\n"
            ).encode(),
            b"Provider City,Total Charges\n"
            b"Austin,100\n"
            b"Austin,200\n"
            b"Boston,150\n",
            (
                f"\r\n--{boundary}\r\n"
                'Content-Disposition: form-data; name="goal"\r\n\r\n'
                "Revenue, billing and insurance"
            ).encode(),
            f"\r\n--{boundary}--\r\n".encode(),
        ]

        status, _, body = self.post(
            "/api/upload",
            b"".join(pieces),
            f"multipart/form-data; boundary={boundary}",
        )

        self.assertEqual(status, 202)

        upload = json.loads(body)
        self.assertIn("job_id", upload)

        import time

        deadline = time.monotonic() + 5
        job = None

        while time.monotonic() < deadline:
            _, _, job_body = self.get(
                "/api/jobs/" + upload["job_id"]
            )
            job = json.loads(job_body)

            if job.get("status") in {
                "complete",
                "error",
                "cancelled",
            }:
                break

            time.sleep(0.05)

        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "complete", job)
        self.assertEqual(job["analysis"]["row_count"], 3)

        status, _, answer_body = self.post(
            "/api/chat",
            json.dumps({
                "question": "What is the average of Total Charges?"
            }).encode(),
        )

        self.assertEqual(status, 200)

        answer = json.loads(answer_body)["answer"]

        self.assertIn("150", answer)
        self.assertIn("average Total Charges", answer)

    def test_static_asset_path_traversal_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                self.base + "/static/%2e%2e/server.py",
                timeout=3,
            )

        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
