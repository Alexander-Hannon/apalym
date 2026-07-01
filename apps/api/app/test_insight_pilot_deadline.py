import json
import re
import time
import unittest
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import (  # noqa: E402
    INSIGHT_PIPELINE_VERSION,
    app,
    finalize_insight_module_contract,
    serialize_module_for_api,
)
from app.insight_contract import text_has_internal_artifact  # noqa: E402

UUID_PATTERN = re.compile(
    r"\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b",
    re.IGNORECASE,
)
CHUNK_PATTERN = re.compile(r"[0-9a-f-]{36}:\d+", re.IGNORECASE)
BRACKET_CITATION = re.compile(r"\[[^\]]*:[^\]]+\]|\[\s*\d")
FORBIDDEN_MODULE_KEYS = {"matches", "citations", "doc_id", "chunk_id", "source_id", "metadata", "namespace"}


def _mock_doc_exists(mock_doc_path):
    mock_path = MagicMock()
    mock_path.exists.return_value = True
    mock_path.name = "meridian_cold_chain_financial_package.pdf"
    mock_doc_path.return_value = mock_path

SAMPLE_MATCHES = [
    {
        "id": "fdc6d32b-0225-4f9e-a84b-8f3b5f496d4f:0",
        "score": 0.91,
        "text": "Warehouse overtime was 10.4% against a target below 7.5%.",
        "metadata": {"doc_id": "fdc6d32b-0225-4f9e-a84b-8f3b5f496d4f", "chunk_id": "0", "start": 21000},
    },
    {
        "id": "fdc6d32b-0225-4f9e-a84b-8f3b5f496d4f:1",
        "score": 0.88,
        "text": "Days sales outstanding was 38.6 days versus a target below 35 days.",
        "metadata": {"doc_id": "fdc6d32b-0225-4f9e-a84b-8f3b5f496d4f", "chunk_id": "1", "start": 24000},
    },
]

SOURCE_PACKETS = [
    {
        "citation_id": 1,
        "document_name": "meridian_cold_chain_financial_package.pdf",
        "page": 7,
        "excerpt": "Warehouse overtime was 10.4% against a target below 7.5%.",
        "title": "Source evidence",
        "label": "[1]",
    }
]


def collect_visible_text_fields(module: dict) -> list[str]:
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


class InsightPilotDeadlineTests(unittest.TestCase):
    def test_invalid_generated_artifact_returns_typed_limited_evidence(self):
        module_output, reasons = finalize_insight_module_contract(
            module_key="risks",
            module_id="key-risks",
            title="Key risks",
            generated={
                "direct_answer": {
                    "text": "ductivity problem [fdc6d32b-0225-4f9e-a84b-8f3b5f496d4f:41]",
                    "citation_ids": [1],
                },
                "key_findings": [
                    {"text": "Medium Medium ROI tracking issue.", "citation_ids": [1]},
                ],
                "recommended_actions": [],
            },
            source_packets=SOURCE_PACKETS,
        )
        payload = serialize_module_for_api(module_output)
        self.assertEqual(payload["status"], "limited_evidence")
        self.assertTrue(reasons)
        for text in collect_visible_text_fields(payload):
            self.assertFalse(text_has_internal_artifact(text), text)
            self.assertNotRegex(text, UUID_PATTERN)
            self.assertNotRegex(text, BRACKET_CITATION)

    @patch("app.main.get_gemini_client")
    @patch("app.main.generate_content_with_retry")
    @patch("app.main.retrieve_matches")
    @patch("app.main.get_doc_path")
    def test_deadline_exhaustion_returns_five_typed_modules(
        self,
        mock_doc_path,
        mock_retrieve,
        mock_generate,
        _mock_gemini,
    ):
        _mock_doc_exists(mock_doc_path)
        mock_retrieve.return_value = SAMPLE_MATCHES

        call_count = {"n": 0}

        def fake_remaining(_deadline_at: float) -> float:
            call_count["n"] += 1
            return 20.0 if call_count["n"] <= 8 else 0.5

        with patch("app.main._insight_remaining_seconds", side_effect=fake_remaining):
            mock_generate.return_value = MagicMock(
                text=json.dumps(
                    {
                        "direct_answer": {"text": "Clean answer.", "citation_ids": [1]},
                        "key_findings": [
                            {"text": "Finding one.", "citation_ids": [1]},
                            {"text": "Finding two.", "citation_ids": [1]},
                        ],
                        "recommended_actions": [
                            {"text": "Action one.", "citation_ids": [1]},
                            {"text": "Action two.", "citation_ids": [1]},
                        ],
                    }
                )
            )
            client = TestClient(app)
            response = client.post(
                "/v1/insights",
                json={"doc_id": "test-doc", "top_k": 3},
                headers={"x-request-id": "deadline-test"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["modules"]), 5)
        limited_count = sum(1 for module in payload["modules"] if module["status"] == "limited_evidence")
        self.assertGreaterEqual(limited_count, 1)
        for module in payload["modules"]:
            self.assertIn(module["status"], {"ready", "limited_evidence"})
            for text in collect_visible_text_fields(module):
                self.assertNotRegex(text, UUID_PATTERN)
                self.assertNotRegex(text, CHUNK_PATTERN)
                self.assertNotRegex(text, BRACKET_CITATION)

    @patch("app.main.get_gemini_client")
    @patch("app.main.generate_content_with_retry")
    @patch("app.main.retrieve_matches")
    @patch("app.main.get_doc_path")
    def test_response_modules_exclude_internal_fields(
        self,
        mock_doc_path,
        mock_retrieve,
        mock_generate,
        _mock_gemini,
    ):
        _mock_doc_exists(mock_doc_path)
        mock_retrieve.return_value = SAMPLE_MATCHES
        mock_generate.return_value = MagicMock(
            text=json.dumps(
                {
                    "direct_answer": {"text": "Warehouse overtime is above target.", "citation_ids": [1]},
                    "key_findings": [
                        {"text": "Collections timing is elevated.", "citation_ids": [1]},
                        {"text": "Truck utilization is below target.", "citation_ids": [1]},
                    ],
                    "recommended_actions": [
                        {"text": "Review overtime by site.", "citation_ids": [1]},
                        {"text": "Audit truck utilization by lane.", "citation_ids": [1]},
                    ],
                }
            )
        )

        client = TestClient(app)
        response = client.post(
            "/v1/insights",
            json={"doc_id": "test-doc", "top_k": 3},
            headers={"x-request-id": "shape-test"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        dumped = json.dumps(payload)
        self.assertNotIn('"matches"', dumped)
        self.assertNotIn('"citations"', dumped)

        for module in payload["modules"]:
            self.assertFalse(FORBIDDEN_MODULE_KEYS.intersection(module.keys()))
            for text in collect_visible_text_fields(module):
                self.assertNotRegex(text, BRACKET_CITATION)
            for key in ("direct_answer", "summary"):
                value = module.get(key)
                if isinstance(value, dict):
                    self.assertIn("text", value)
                    self.assertIn("citation_ids", value)
                    self.assertIsInstance(value["citation_ids"], list)

    @patch("app.main.get_gemini_client")
    @patch("app.main.generate_content_with_retry")
    @patch("app.main.retrieve_matches")
    @patch("app.main.get_doc_path")
    def test_slow_failed_model_returns_under_deadline_budget(
        self,
        mock_doc_path,
        mock_retrieve,
        mock_generate,
        _mock_gemini,
    ):
        _mock_doc_exists(mock_doc_path)
        mock_retrieve.return_value = SAMPLE_MATCHES

        def slow_fail(*_args, **_kwargs):
            time.sleep(0.05)
            raise RuntimeError("simulated model failure")

        mock_generate.side_effect = slow_fail

        client = TestClient(app)
        response = client.post(
            "/v1/insights",
            json={"doc_id": "test-doc", "top_k": 3},
            headers={"x-request-id": "elapsed-test"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["pipeline_version"], INSIGHT_PIPELINE_VERSION)
        self.assertLess(payload["elapsed_ms"], 75_000)
        self.assertEqual(response.headers.get("x-apalym-insight-pipeline"), INSIGHT_PIPELINE_VERSION)
        self.assertIsNotNone(response.headers.get("x-apalym-insight-elapsed-ms"))
        self.assertEqual(len(payload["modules"]), 5)
        for module in payload["modules"]:
            self.assertEqual(module["status"], "limited_evidence")


if __name__ == "__main__":
    unittest.main()
