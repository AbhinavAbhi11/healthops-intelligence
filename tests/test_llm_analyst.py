
"""Offline tests for LLM planning and local execution (no API key required)."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

import llm_analyst


class LlmAnalystExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tables = {
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
        }

    def plan(self, operation, **overrides):
        plan = {
            "operation": operation,
            "table": "healthops_persistence_test",
            "measure": None,
            "group_by": [],
            "aggregation": None,
            "filters": [],
            "field": None,
            "limit": 10,
            "sort_order": "desc",
            "include_percentages": False,
            "clarification": "",
        }
        plan.update(overrides)
        return plan

    def test_average_in_chennai_uses_only_matching_rows(self):
        result = llm_analyst._execute_plan(
            self.plan(
                "aggregate",
                measure="Total Charges",
                aggregation="mean",
                filters=[{
                    "field": "City",
                    "operator": "eq",
                    "value": "Chennai",
                }],
            ),
            self.tables,
        )

        self.assertIn("152,500", result["answer"])
        self.assertEqual(
            result["query_plan"]["rows_after_filter"],
            2,
        )
        self.assertTrue(result["executed"])

    def test_average_in_hyderabad_uses_only_matching_rows(self):
        result = llm_analyst._execute_plan(
            self.plan(
                "aggregate",
                measure="Total Charges",
                aggregation="mean",
                filters=[{
                    "field": "City",
                    "operator": "eq",
                    "value": "Hyderabad",
                }],
            ),
            self.tables,
        )

        self.assertIn("122,500", result["answer"])

    def test_record_count_by_department(self):
        result = llm_analyst._execute_plan(
            self.plan(
                "group_count",
                group_by=["Department"],
                limit=50,
            ),
            self.tables,
        )

        self.assertIn("Cardiology = 2", result["answer"])
        self.assertIn("Neurology = 2", result["answer"])
        self.assertIn("Orthopedics = 1", result["answer"])

    def test_percentage_by_department(self):
        result = llm_analyst._execute_plan(
            self.plan(
                "group_count",
                group_by=["Department"],
                limit=50,
                include_percentages=True,
            ),
            self.tables,
        )

        self.assertIn("40.00%", result["answer"])
        self.assertIn("20.00%", result["answer"])

    def test_average_charges_by_department(self):
        result = llm_analyst._execute_plan(
            self.plan(
                "group_aggregate",
                measure="Total Charges",
                group_by=["Department"],
                aggregation="mean",
            ),
            self.tables,
        )

        self.assertIn("Cardiology = 165,000", result["answer"])
        self.assertIn("Neurology = 102,500", result["answer"])
        self.assertIn("Orthopedics = 125,000", result["answer"])

    def test_highest_average_charges_by_city(self):
        result = llm_analyst._execute_plan(
            self.plan(
                "rank",
                measure="Total Charges",
                group_by=["City"],
                aggregation="mean",
                limit=1,
            ),
            self.tables,
        )

        self.assertIn("Chennai = 152,500", result["answer"])

    def test_compare_admissions_across_hospitals(self):
        result = llm_analyst._execute_plan(
            self.plan(
                "group_aggregate",
                measure="Admissions",
                group_by=["Hospital"],
                aggregation="sum",
            ),
            self.tables,
        )

        self.assertIn("City Hospital = 205", result["answer"])
        self.assertIn("Apollo Test Facility = 235", result["answer"])

    def test_duplicate_rows_are_counted_without_returning_rows(self):
        frame = self.tables["healthops_persistence_test"]

        self.tables["healthops_persistence_test"] = pd.concat(
            [frame, frame.iloc[[0]]],
            ignore_index=True,
        )

        result = llm_analyst._execute_plan(
            self.plan("duplicate_rows"),
            self.tables,
        )

        self.assertIn("1 across 6 rows", result["answer"])

    def test_column_types_are_returned(self):
        result = llm_analyst._execute_plan(
            self.plan("column_types"),
            self.tables,
        )

        self.assertIn("Total Charges = numeric", result["answer"])
        self.assertIn("Department = text", result["answer"])

    def test_missing_values_are_reported_by_field(self):
        self.tables["healthops_persistence_test"].loc[0, "City"] = None

        result = llm_analyst._execute_plan(
            self.plan("missing_values", field="City"),
            self.tables,
        )

        self.assertIn("City = 1", result["answer"])

    def test_identifier_field_cannot_be_used_as_group(self):
        self.tables["healthops_persistence_test"]["Patient ID"] = [
            "p1",
            "p2",
            "p3",
            "p4",
            "p5",
        ]

        result = llm_analyst._execute_plan(
            self.plan(
                "group_count",
                group_by=["Patient ID"],
            ),
            self.tables,
        )

        self.assertFalse(result["executed"])
        self.assertEqual(
            result["query_plan"]["operation"],
            "clarify",
        )

    def test_forecast_is_refused_without_api_call(self):
        result = llm_analyst.answer_with_llm(
            "Predict next year's admissions.",
            self.tables,
        )

        self.assertFalse(result["executed"])
        self.assertEqual(
            result["query_plan"]["operation"],
            "unsupported_forecast",
        )
        self.assertIn(
            "cannot reliably predict",
            result["answer"].lower(),
        )

    def test_schema_does_not_contain_raw_values_or_sensitive_fields(self):
        self.tables["healthops_persistence_test"]["Patient Name"] = [
            "A Person",
            "A Person",
            "A Person",
            "A Person",
            "A Person",
        ]

        schema = llm_analyst._schema(self.tables)
        serialized = json.dumps(schema)

        self.assertNotIn("A Person", serialized)
        self.assertNotIn("Patient Name", serialized)
        self.assertIn("excluded_sensitive_field_count", serialized)


    def test_llm_function_call_uses_schema_only_and_local_execution(self):
        plan = self.plan(
            "aggregate",
            table="table_1",
            measure="Total Charges",
            aggregation="mean",
            filters=[
                {"field": "City", "operator": "eq", "value": "Chennai"}
            ],
        )

        function_call = SimpleNamespace(
            name="plan_healthcare_analysis",
            args=plan,
        )

        class FakeModels:
            def __init__(self):
                self.calls = []

            def generate_content(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(function_calls=[function_call])

        fake_client = SimpleNamespace(models=FakeModels())

        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "test-key",
            "LLM_PROVIDER": "gemini",
            "GEMINI_MODEL": "gemini-3.5-flash-lite",
            "HEALTHOPS_LLM_ENABLED": "true",
        }):
            with patch.object(
                llm_analyst,
                "_get_client",
                return_value=fake_client,
            ):
                result = llm_analyst.answer_with_llm(
                    "What is the average of Total Charges in Chennai?",
                    self.tables,
                )

        self.assertIn("152,500", result["answer"])
        self.assertEqual(result["query_plan"]["operation"], "aggregate")
        self.assertEqual(len(fake_client.models.calls), 1)

        request = fake_client.models.calls[0]
        payload = json.dumps(request["contents"])

        self.assertNotIn("Apollo Test Facility", payload)
        self.assertNotIn("healthops_persistence_test", payload)
        self.assertNotIn("180000", payload)
        self.assertEqual(
            result["query_plan"]["table"],
            "healthops_persistence_test",
        )
        self.assertEqual(
            request["model"],
            "gemini-3.5-flash-lite",
        )

if __name__ == "__main__":
    unittest.main(verbosity=2)
