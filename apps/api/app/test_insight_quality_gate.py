"""Regression tests for softer insight module quality-gate policy."""

import unittest

from app.insight_contract import (
    finalize_insight_module,
    serialize_module_for_api,
)

THREE_SOURCE_PACKETS = [
    {
        "citation_id": 1,
        "document_name": "meridian_cold_chain_financial_package.pdf",
        "page": 7,
        "excerpt": "Warehouse overtime was 10.4% against a target below 7.5%.",
        "title": "Source evidence",
    },
    {
        "citation_id": 2,
        "document_name": "meridian_cold_chain_financial_package.pdf",
        "page": 8,
        "excerpt": "Days sales outstanding was 38.6 days versus a target below 35 days.",
        "title": "Source evidence",
    },
    {
        "citation_id": 3,
        "document_name": "meridian_cold_chain_financial_package.pdf",
        "page": 9,
        "excerpt": "Truck utilization remained below planned efficiency levels for the quarter.",
        "title": "Source evidence",
    },
]


class InsightQualityGateTests(unittest.TestCase):
    def test_two_sources_useful_direct_answer_stays_source_backed(self):
        generated = {
            "direct_answer": {
                "text": (
                    "Profit leakage appears concentrated in labor overtime, underutilized transportation "
                    "assets, deadhead miles, and slower cash conversion."
                ),
                "citation_ids": [1, 2],
            },
            "key_findings": [
                {
                    "text": "Warehouse overtime is above the stated operating target.",
                    "citation_ids": [1],
                },
            ],
            "recommended_actions": [
                {
                    "text": "Validate overtime by location, shift, and supervisor before reallocating labor.",
                    "citation_ids": [1],
                },
            ],
        }
        module, _ = finalize_insight_module(
            module_key="losses",
            module_id="loss-leakage",
            title="Where are we losing money?",
            generated=generated,
            source_packets=THREE_SOURCE_PACKETS[:2],
        )
        payload = serialize_module_for_api(module)

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(len(payload["sources"]), 2)
        self.assertIn("Profit leakage", payload["direct_answer"]["text"])
        self.assertNotIn("does not provide enough clean category-level detail", payload["direct_answer"]["text"])
        self.assertGreaterEqual(len(payload["key_findings"]), 2)
        self.assertIn(payload["confidence"], {"medium", "high"})

    def test_zero_sources_still_limited_evidence(self):
        module, reasons = finalize_insight_module(
            module_key="losses",
            module_id="loss-leakage",
            title="Where are we losing money?",
            generated={
                "direct_answer": {"text": "Warehouse overtime is elevated.", "citation_ids": []},
                "key_findings": [],
                "recommended_actions": [],
            },
            source_packets=[],
        )
        payload = serialize_module_for_api(module)
        self.assertEqual(payload["status"], "limited_evidence")
        self.assertEqual(payload["sources"], [])
        self.assertIn("no_usable_sources", reasons)


if __name__ == "__main__":
    unittest.main()
