
"""Tests proving that the live server chat function uses Engine v2."""

import unittest

import pandas as pd

import server


class EngineIntegrationTests(unittest.TestCase):

    def setUp(self):
        self.previous_session = dict(server.SESSION)

        server.SESSION.update({
            "tables": {
                "healthops_persistence_test": pd.DataFrame({
                    "Hospital": [
                        "City Hospital",
                        "City Hospital",
                        "Apollo Test Facility",
                        "Apollo Test Facility",
                        "General Test Hospital",
                    ],
                    "City": [
                        "Hyderabad",
                        "Hyderabad",
                        "Chennai",
                        "Chennai",
                        "Bengaluru",
                    ],
                    "Department": [
                        "Cardiology",
                        "Neurology",
                        "Cardiology",
                        "Orthopedics",
                        "Neurology",
                    ],
                    "Total Charges": [
                        150000,
                        95000,
                        180000,
                        125000,
                        110000,
                    ],
                    "Admissions": [120, 85, 145, 90, 75],
                })
            },
            "source_tables": None,
            "cleaned": {},
            "clean_active": False,
            "last_analysis": None,
            "goal": "Hospital operations and patient flow",
            "clean_log": [],
            "generation": 1,
            "source_db": None,
            "refreshed_at": None,
        })

        server.SESSION.pop("_analysis_static", None)

        self.analysis = server.build_analysis(
            "Hospital operations and patient flow"
        )

        server.SESSION["last_analysis"] = self.analysis

    def tearDown(self):
        server.SESSION.clear()
        server.SESSION.update(self.previous_session)

    def test_chat_average_filter_uses_only_matching_city(self):
        result = server.chat_answer(
            "What is the average of Total Charges in Chennai?",
            self.analysis,
        )

        self.assertIn("152,500", result["answer"])
        self.assertIn("2 non-missing numeric values", result["answer"])
        self.assertEqual(
            result["query_plan"]["operation"],
            "aggregate",
        )
        self.assertEqual(
            result["query_plan"]["filters"][0]["value"],
            "Chennai",
        )

    def test_chat_record_count_by_department_uses_grouping(self):
        result = server.chat_answer(
            "Show record count by Department",
            self.analysis,
        )

        answer = result["answer"]

        self.assertIn("Cardiology = 2", answer)
        self.assertIn("Neurology = 2", answer)
        self.assertIn("Orthopedics = 1", answer)

        self.assertEqual(
            result["query_plan"]["operation"],
            "group_count",
        )

    def test_chat_does_not_invent_future_cost_reduction(self):
        result = server.chat_answer(
            "Will this hospital reduce costs by 20% next year?",
            self.analysis,
        )

        answer = result["answer"].lower()

        self.assertIn("cannot reliably predict", answer)
        self.assertIn("20% reduction is a target", answer)
        self.assertIn("cannot reliably predict whether", answer)

        self.assertEqual(
            result["query_plan"]["operation"],
            "unsupported_forecast",
        )
        self.assertFalse(result["executed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
