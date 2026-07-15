"""Regression tests for insight contract completion vs limited-evidence decisions."""

import re
import unittest

from app.insight_contract import (
    build_limited_evidence_module,
    build_public_sources,
    finalize_insight_module,
    parse_cited_field,
    serialize_module_for_api,
    text_has_internal_artifact,
)

UUID_PATTERN = re.compile(
    r"\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b",
    re.IGNORECASE,
)

THREE_SOURCE_PACKETS = [
    {
        "citation_id": 1,
        "document_name": "fdc6d32b-0225-4f9e-a84b-8f3b5f496d4f.pdf",
        "page": 7,
        "excerpt": "Warehouse overtime was 10.4% against a target below 7.5%.",
        "title": "Source evidence",
    },
    {
        "citation_id": 2,
        "document_name": "fdc6d32b-0225-4f9e-a84b-8f3b5f496d4f.pdf",
        "page": 8,
        "excerpt": "Days sales outstanding was 38.6 days versus a target below 35 days.",
        "title": "Source evidence",
    },
    {
        "citation_id": 3,
        "document_name": "fdc6d32b-0225-4f9e-a84b-8f3b5f496d4f.pdf",
        "page": 9,
        "excerpt": "Truck utilization remained below planned efficiency levels for the quarter.",
        "title": "Source evidence",
    },
]


def collect_visible_diagnosis_text(module: dict) -> list[str]:
    texts: list[str] = []
    for key in ("direct_answer", "summary"):
        value = module.get(key)
        if isinstance(value, dict) and isinstance(value.get("text"), str):
            texts.append(value["text"])
    for key in ("key_findings", "recommended_actions"):
        for item in module.get(key) or []:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
    limitations = module.get("evidence_limitations") or {}
    if isinstance(limitations.get("message"), str):
        texts.append(limitations["message"])
    for question in module.get("validation_questions") or limitations.get("validation_questions") or []:
        if isinstance(question, str):
            texts.append(question)
    return texts


class InsightContractCompletionTests(unittest.TestCase):
    def test_case_a_three_sources_one_action_returns_ready_with_supplement(self):
        generated = {
            "direct_answer": {
                "text": (
                    "Warehouse overtime is above target and appears to be a recurring operational issue."
                ),
                "citation_ids": [1],
            },
            "key_findings": [
                {
                    "text": "Truck utilization is below the operating target for several lanes.",
                    "citation_ids": [2],
                },
                {
                    "text": "Collections timing may be affecting cash conversion this quarter.",
                    "citation_ids": [3],
                },
            ],
            "recommended_actions": [
                {"text": "Review overtime by site and shift for the last 30 days.", "citation_ids": [1]},
            ],
        }
        module, reasons = finalize_insight_module(
            module_key="losses",
            module_id="loss-leakage",
            title="Where are we losing money?",
            generated=generated,
            source_packets=THREE_SOURCE_PACKETS,
        )
        payload = serialize_module_for_api(module)

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["confidence"], "medium")
        self.assertIn("recurring operational issue", payload["direct_answer"]["text"])
        self.assertGreaterEqual(len(payload["recommended_actions"]), 2)
        self.assertIn("recommended_actions_supplemented", reasons)
        self.assertEqual(len(payload["sources"]), 3)
        for text in collect_visible_diagnosis_text(payload):
            self.assertNotIn("Warehouse overtime was 10.4%", text)

    def test_case_b_trailing_uuid_stripped_sentence_retained(self):
        cited = parse_cited_field(
            {
                "text": (
                    "Warehouse overtime is above target and appears to be a recurring operational issue. "
                    "fdc6d32b-0225-4f9e-a84b-8f3b5f496d4f"
                ),
                "citation_ids": [1],
            },
            max_sources=3,
        )
        self.assertEqual(cited.citation_ids, [1])
        self.assertIn("recurring operational issue", cited.text)
        self.assertNotRegex(cited.text, UUID_PATTERN)
        self.assertFalse(text_has_internal_artifact(cited.text))

    def test_case_c_raw_table_excerpt_not_in_visible_fields(self):
        table_excerpt = (
            "Row 1: 120 45 (12) 88 33 Row 2: 140 52 (15) 91 37 Row 3: 118 49 (11) 86 31"
        )
        packets = [
            {
                "citation_id": 1,
                "document_name": "report.pdf",
                "page": 4,
                "excerpt": table_excerpt,
                "title": "Source evidence",
            },
            {
                "citation_id": 2,
                "document_name": "report.pdf",
                "page": 5,
                "excerpt": "Warehouse overtime was 10.4% against a target below 7.5%.",
                "title": "Source evidence",
            },
            {
                "citation_id": 3,
                "document_name": "report.pdf",
                "page": 6,
                "excerpt": "Truck utilization remained below planned efficiency levels.",
                "title": "Source evidence",
            },
        ]
        generated = {
            "direct_answer": {"text": table_excerpt, "citation_ids": [1]},
            "key_findings": [
                {"text": table_excerpt, "citation_ids": [1]},
                {"text": "Collections timing may be affecting cash conversion.", "citation_ids": [2]},
            ],
            "recommended_actions": [
                {"text": "Review overtime by site and shift.", "citation_ids": [2]},
                {"text": "Audit truck utilization by lane.", "citation_ids": [3]},
            ],
        }
        module, _ = finalize_insight_module(
            module_key="trends",
            module_id="trend-signals",
            title="What trends matter?",
            generated=generated,
            source_packets=packets,
        )
        payload = serialize_module_for_api(module)

        self.assertEqual(payload["status"], "ready")
        for text in collect_visible_diagnosis_text(payload):
            self.assertNotIn("Row 1:", text)
        self.assertIn(table_excerpt, payload["sources"][0]["excerpt"])

    def test_case_d_zero_sources_returns_limited_evidence(self):
        module, reasons = finalize_insight_module(
            module_key="risks",
            module_id="key-risks",
            title="Key risks",
            generated={
                "direct_answer": {"text": "Capital execution risk is elevated.", "citation_ids": []},
                "key_findings": [],
                "recommended_actions": [],
            },
            source_packets=[],
        )
        payload = serialize_module_for_api(module)

        self.assertEqual(payload["status"], "limited_evidence")
        self.assertEqual(payload["confidence"], "low")
        self.assertEqual(payload["sources"], [])
        self.assertGreaterEqual(len(payload["validation_questions"]), 2)
        self.assertIn("no_usable_sources", reasons)

    def test_case_e_one_weak_source_invalid_generation_limited_evidence(self):
        module, reasons = finalize_insight_module(
            module_key="risks",
            module_id="key-risks",
            title="Key risks",
            generated={
                "direct_answer": {
                    "text": "holds Financial Capex execution risk [c394bf1b-9bce-4a35-9601-23632478f468:67]",
                    "citation_ids": [1],
                },
                "key_findings": [],
                "recommended_actions": [],
            },
            source_packets=[
                {
                    "citation_id": 1,
                    "document_name": "meridian_cold_chain_financial_package.pdf",
                    "page": 3,
                    "excerpt": "YTD capex spending remains elevated while automation savings are delayed.",
                    "title": "Source evidence",
                }
            ],
        )
        payload = serialize_module_for_api(module)

        self.assertEqual(payload["status"], "limited_evidence")
        self.assertEqual(len(payload["sources"]), 1)
        self.assertEqual(payload["sources"][0]["title"], "Source excerpt 1")
        self.assertNotIn(
            "YTD capex spending remains elevated",
            payload["direct_answer"]["text"],
        )
        for text in collect_visible_diagnosis_text(payload):
            self.assertNotIn("YTD capex spending", text)
        self.assertIn(reasons[0], {"single_source_insufficient_generation", "single_source_partial_generation"})

    def test_limited_evidence_never_injects_raw_source_excerpts_into_findings(self):
        sources = build_public_sources(THREE_SOURCE_PACKETS)
        module = build_limited_evidence_module(
            module_key="losses",
            module_id="loss-leakage",
            title="Where are we losing money?",
            sources=sources,
        )
        payload = serialize_module_for_api(module)

        for finding in payload["key_findings"]:
            self.assertNotIn("10.4%", finding["text"])
            self.assertNotIn("38.6 days", finding["text"])
        for text in collect_visible_diagnosis_text(payload):
            self.assertNotIn("Warehouse overtime was 10.4%", text)


if __name__ == "__main__":
    unittest.main()
