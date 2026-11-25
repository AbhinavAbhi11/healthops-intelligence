
"""Regression tests for the structured HealthOps analytics engine."""
import unittest

import pandas as pd

from analytics_engine import answer_question


class AnalyticalEngineTests(unittest.TestCase):
    def setUp(self):
        self.data = pd.DataFrame({
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
            "Total Charges": [150000, 95000, 180000, 125000, 110000],
            "Admissions": [120, 85, 145, 90, 75],
        })

        self.tables = {"healthops_test": self.data}

        self.analysis = {
            "goal": "Hospital operations and patient flow"
        }

    def ask(self, question):
        return answer_question(
            question,
            self.tables,
            self.analysis,
        )

    def test_average_of_total_charges(self):
        result = self.ask(
            "What is the average of Total Charges?"
        )

        self.assertIn("average Total Charges", result["answer"])
        self.assertIn("132,000", result["answer"])
        self.assertEqual(
            result["query_plan"]["statistic"],
            "mean",
        )

    def test_explicit_total_is_a_sum(self):
        result = self.ask(
            "What is the total of Total Charges?"
        )

        self.assertIn("sum Total Charges", result["answer"])
        self.assertIn("660,000", result["answer"])
        self.assertEqual(
            result["query_plan"]["statistic"],
            "sum",
        )

    def test_top_hospitals_by_total_charges(self):
        result = self.ask(
            "Show the top 2 hospitals by total charges"
        )

        self.assertIn("Apollo Test Facility", result["answer"])
        self.assertIn("City Hospital", result["answer"])
        self.assertEqual(
            result["query_plan"]["operation"],
            "rank",
        )
        self.assertEqual(
            result["query_plan"]["statistic"],
            "sum",
        )

    def test_filter_by_city_before_aggregation(self):
        result = self.ask(
            "What is the average of Total Charges in Chennai?"
        )

        self.assertIn("152,500", result["answer"])
        self.assertEqual(
            result["query_plan"]["rows_after_filter"],
            2,
        )
        self.assertEqual(
            result["query_plan"]["filters"],
            [{
                "field": "City",
                "operator": "equals",
                "value": "Chennai",
            }],
        )

    def test_distinct_cities_exclude_missing_values(self):
        tables = {
            "providers": pd.DataFrame({
                "Provider City": [
                    "Austin",
                    "Austin",
                    "Boston",
                    None,
                ]
            })
        }

        result = answer_question(
            "What are the total recorded cities?",
            tables,
            self.analysis,
        )

        self.assertIn(
            "Distinct Provider City values",
            result["answer"],
        )
        self.assertIn("2", result["answer"])
        self.assertIn(
            "1 missing values excluded",
            result["answer"],
        )

    def test_multiple_inferred_filters_are_not_silently_dropped(self):
        result = self.ask(
            "What is the average of Total Charges in Chennai and Cardiology?"
        )

        self.assertIn(
            "more than one possible category filter",
            result["answer"].lower(),
        )

    def test_count_records_by_department(self):
        result = self.ask(
            "Show record count by Department"
        )

        self.assertIn(
            "Cardiology = 2",
            result["answer"],
        )
        self.assertEqual(
            result["query_plan"]["operation"],
            "group_count",
        )

    def test_filter_and_source_are_not_mutated(self):
        before = self.data.copy(deep=True)

        self.ask(
            "What is the average of Total Charges in Chennai?"
        )

        pd.testing.assert_frame_equal(
            self.data,
            before,
        )

    def test_ties_at_rank_boundary_are_retained(self):
        tables = {
            "providers": pd.DataFrame({
                "Hospital": ["A", "B", "C"],
                "Total Charges": [100, 100, 100],
            })
        }

        result = answer_question(
            "Show the top 1 hospitals by Total Charges",
            tables,
            self.analysis,
        )

        self.assertIn("A = 100", result["answer"])
        self.assertIn("B = 100", result["answer"])
        self.assertIn("C = 100", result["answer"])
        self.assertEqual(
            result["query_plan"]["groups_returned"],
            3,
        )

    def test_sum_of_average_valued_field_is_blocked(self):
        tables = {
            "service_metrics": pd.DataFrame({
                "Department": ["A", "B"],
                "Average Charges": [100, 200],
            })
        }

        result = answer_question(
            "What is the total of Average Charges?",
            tables,
            self.analysis,
        )

        self.assertEqual(
            result["query_plan"]["operation"],
            "blocked_unsafe_sum",
        )
        self.assertIn(
            "would not produce a valid total",
            result["answer"],
        )

    def test_patient_identifiers_cannot_be_used_as_dimensions(self):
        tables = {
            "patients": pd.DataFrame({
                "Patient Name": [
                    "Person A",
                    "Person B",
                ],
                "Charges": [10, 20],
            })
        }

        result = answer_question(
            "Show charges by Patient Name",
            tables,
            self.analysis,
        )

        self.assertIn(
            "no safe category field",
            result["answer"].lower(),
        )
        self.assertNotIn(
            "Person A",
            result["answer"],
        )

    def test_ambiguous_measure_across_tables_requests_clarification(self):
        tables = {
            "inpatient": pd.DataFrame({
                "Total Charges": [100, 200]
            }),
            "outpatient": pd.DataFrame({
                "Total Charges": [50, 75]
            }),
        }

        result = answer_question(
            "What is the average of Total Charges?",
            tables,
            self.analysis,
        )

        self.assertIn(
            "multiple tables",
            result["answer"].lower(),
        )

    def test_unsupported_prediction_is_not_guessed(self):
        result = self.ask(
            "Will this hospital reduce costs by 20% next year?"
        )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
