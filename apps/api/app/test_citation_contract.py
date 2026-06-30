import json
import re
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import (  # noqa: E402
    _build_public_sources,
    _contains_raw_visible_artifact,
    _parse_cited_field,
    build_clean_source_packets,
    build_limited_evidence_module,
    clean_source_excerpt,
    strip_source_artifacts,
    validate_module_response_contract,
)

UUID_PATTERN = re.compile(
    r"\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b",
    re.IGNORECASE,
)
DOC_CHUNK_PATTERN = re.compile(r"[0-9a-f-]{36}:\d+", re.IGNORECASE)
GENERIC_FILLER = (
    "Additional detail is needed to fully assess this finding.",
    "Validate this area with the relevant department owner.",
    "Investigate the highest-impact cited item first.",
    "This module could not produce a clean diagnosis.",
)


def collect_visible_strings(module: dict) -> list[str]:
    values: list[str] = []

    def walk(value):
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            if isinstance(value.get("text"), str):
                values.append(value["text"])
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for key in ("direct_answer", "summary", "key_findings", "recommended_actions", "sources", "evidence_limitations"):
        if key in module:
            walk(module[key])
    return values


class CitationContractTests(unittest.TestCase):
    def test_clean_source_excerpt_strips_uuid_and_broken_leading(self):
        raw = "ductivity was 76.9% [cd3d2a1b-1111-2222-3333-4444-555566667777:0]"
        cleaned = clean_source_excerpt(raw)
        self.assertNotRegex(cleaned, UUID_PATTERN)
        self.assertNotIn("ductivity", cleaned.lower())

    def test_strip_source_artifacts_removes_bracketed_ids(self):
        text = "Warehouse overtime is elevated [cd3d2a1b-1111-2222-3333-4444-555566667777:2]."
        cleaned = strip_source_artifacts(text)
        self.assertNotRegex(cleaned, DOC_CHUNK_PATTERN)
        self.assertNotIn("[", cleaned)

    def test_parse_cited_field_extracts_ids_and_cleans_prose(self):
        cited = _parse_cited_field("Labor pressure is rising. [1, 2]", max_sources=3)
        self.assertEqual(cited["citation_ids"], [1, 2])
        self.assertNotIn("[", cited["text"])

    def test_build_public_sources_omit_internal_ids(self):
        packets, _ = build_clean_source_packets(
            [
                {
                    "id": "uuid-123:0",
                    "score": 0.8,
                    "text": "Warehouse overtime was 10.4% against a target below 7.5%.",
                    "metadata": {"doc_id": "uuid-123", "chunk_id": "0", "start": 21000},
                }
            ],
            document_name="meridian_cold_chain_financial_package.pdf",
        )
        sources = _build_public_sources(packets)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["document_name"], "meridian_cold_chain_financial_package.pdf")
        self.assertNotIn("source_id", sources[0])
        self.assertNotIn("uuid-123", json.dumps(sources))

    def test_validate_module_contract_accepts_ready_module(self):
        module = {
            "status": "ready",
            "direct_answer": {"text": "Warehouse overtime is above target.", "citation_ids": [1]},
            "key_findings": [
                {"text": "Truck utilization is below target.", "citation_ids": [1]},
                {"text": "Collections pressure is elevated.", "citation_ids": [2]},
            ],
            "recommended_actions": [
                {"text": "Review overtime by site and shift.", "citation_ids": [1]},
                {"text": "Audit truck utilization by lane.", "citation_ids": [2]},
            ],
            "sources": [
                {
                    "citation_id": 1,
                    "title": "Source evidence",
                    "page": 7,
                    "excerpt": "Warehouse overtime was 10.4% against a target below 7.5%.",
                    "document_name": "meridian_cold_chain_financial_package.pdf",
                },
                {
                    "citation_id": 2,
                    "title": "Source evidence",
                    "page": 8,
                    "excerpt": "Days sales outstanding was 38.6 days versus a target below 35 days.",
                    "document_name": "meridian_cold_chain_financial_package.pdf",
                },
            ],
            "evidence_limitations": {
                "has_limitations": False,
                "message": None,
                "validation_questions": [],
            },
        }
        ok, reasons = validate_module_response_contract(module)
        self.assertTrue(ok, reasons)

    def test_validate_module_contract_rejects_uuid_in_prose(self):
        module = {
            "status": "ready",
            "direct_answer": {
                "text": "Issue found [cd3d2a1b-1111-2222-3333-4444-555566667777:0]",
                "citation_ids": [1],
            },
            "key_findings": [
                {"text": "Finding one.", "citation_ids": [1]},
                {"text": "Finding two.", "citation_ids": [1]},
            ],
            "recommended_actions": [
                {"text": "Action one.", "citation_ids": [1]},
                {"text": "Action two.", "citation_ids": [1]},
            ],
            "sources": [
                {
                    "citation_id": 1,
                    "title": "Source evidence",
                    "page": None,
                    "excerpt": "Clean excerpt.",
                    "document_name": "meridian_cold_chain_financial_package.pdf",
                }
            ],
            "evidence_limitations": {"has_limitations": False, "message": None, "validation_questions": []},
        }
        ok, reasons = validate_module_response_contract(module)
        self.assertFalse(ok)
        self.assertIn("direct_answer_raw_artifact", reasons)

    def test_limited_evidence_module_shape(self):
        packets, _ = build_clean_source_packets(
            [
                {
                    "id": "uuid-123:1",
                    "score": 0.7,
                    "text": "Labor costs increased 8.2% year over year.",
                    "metadata": {"doc_id": "uuid-123", "chunk_id": "1", "start": 5000},
                }
            ],
            document_name="meridian_cold_chain_financial_package.pdf",
        )
        module = build_limited_evidence_module("loss-leakage", "Where are we losing money?", packets)
        self.assertEqual(module["status"], "limited_evidence")
        self.assertTrue(module["evidence_limitations"]["has_limitations"])
        self.assertGreaterEqual(len(module["evidence_limitations"]["validation_questions"]), 2)
        visible = collect_visible_strings(module)
        for text in visible:
            self.assertFalse(_contains_raw_visible_artifact(text), text)
            self.assertFalse(any(filler.lower() in text.lower() for filler in GENERIC_FILLER), text)


if __name__ == "__main__":
    unittest.main()
