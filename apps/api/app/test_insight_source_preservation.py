import json
import re
import unittest
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import (  # noqa: E402
    app,
    build_clean_source_packets,
    build_insight_prompt_context,
    finalize_insight_module_contract,
    serialize_module_for_api,
)
from app.insight_contract import (  # noqa: E402
    build_limited_evidence_module,
    build_public_sources,
    text_has_internal_artifact,
)

UUID_PATTERN = re.compile(
    r"\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b",
    re.IGNORECASE,
)
INTERNAL_ID_PATTERN = re.compile(r"\b(?:doc_id|chunk_id|source_id)\b", re.IGNORECASE)
CHUNK_PATTERN = re.compile(r"[0-9a-f-]{36}:\d+", re.IGNORECASE)

SAMPLE_MATCHES = [
    {
        "id": "fdc6d32b-0225-4f9e-a84b-8f3b5f496d4f:0",
        "score": 0.91,
        "text": "Warehouse overtime was 10.4% against a target below 7.5%. [fdc6d32b-0225-4f9e-a84b-8f3b5f496d4f:0]",
        "metadata": {"doc_id": "fdc6d32b-0225-4f9e-a84b-8f3b5f496d4f", "chunk_id": "0", "start": 21000},
    },
    {
        "id": "fdc6d32b-0225-4f9e-a84b-8f3b5f496d4f:1",
        "score": 0.88,
        "text": "Days sales outstanding was 38.6 days versus a target below 35 days.",
        "metadata": {"doc_id": "fdc6d32b-0225-4f9e-a84b-8f3b5f496d4f", "chunk_id": "1", "start": 24000},
    },
]


def _mock_doc_exists(mock_doc_path):
    mock_path = MagicMock()
    mock_path.exists.return_value = True
    mock_path.name = "meridian_cold_chain_financial_package.pdf"
    mock_doc_path.return_value = mock_path


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
    return texts


class InsightSourcePreservationTests(unittest.TestCase):
    def test_prompt_context_uses_clean_packets_only(self):
        packets, _ = build_clean_source_packets(
            SAMPLE_MATCHES,
            document_name="meridian_cold_chain_financial_package.pdf",
        )
        context, _ = build_insight_prompt_context(packets)
        self.assertIn("[1]", context)
        self.assertIn("Warehouse overtime", context)
        self.assertNotRegex(context, UUID_PATTERN)
        self.assertNotRegex(context, CHUNK_PATTERN)
        self.assertNotRegex(context, INTERNAL_ID_PATTERN)

    def test_generation_failure_preserves_sources(self):
        packets, _ = build_clean_source_packets(
            SAMPLE_MATCHES,
            document_name="meridian_cold_chain_financial_package.pdf",
        )
        module_output, reasons = finalize_insight_module_contract(
            module_key="losses",
            module_id="loss-leakage",
            title="Where are we losing money?",
            generated=None,
            source_packets=packets,
        )
        payload = serialize_module_for_api(module_output)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["confidence"], "medium")
        self.assertGreaterEqual(len(payload["sources"]), 2)
        self.assertTrue(reasons)
        for text in collect_visible_text_fields(payload):
            self.assertFalse(text_has_internal_artifact(text), text)
            self.assertNotRegex(text, UUID_PATTERN)
        findings = payload.get("key_findings") or []
        self.assertGreaterEqual(len(findings), 2)
        self.assertTrue(any(item.get("citation_ids") for item in findings if isinstance(item, dict)))

    def test_limited_evidence_with_zero_packets_has_no_sources(self):
        module = build_limited_evidence_module(
            module_key="losses",
            module_id="loss-leakage",
            title="Where are we losing money?",
            sources=[],
        )
        payload = serialize_module_for_api(module)
        self.assertEqual(payload["status"], "limited_evidence")
        self.assertEqual(payload["sources"], [])

    @patch("app.main.get_gemini_client")
    @patch("app.main.generate_content_with_retry")
    @patch("app.main.retrieve_matches")
    @patch("app.main.get_doc_path")
    @patch("app.main.visible_module_uuid_locations")
    def test_uuid_scan_repair_preserves_sources(
        self,
        mock_uuid_scan,
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
                    "direct_answer": {"text": "Warehouse overtime is elevated.", "citation_ids": [1]},
                    "key_findings": [
                        {"text": "Collections timing is elevated.", "citation_ids": [1]},
                        {"text": "Truck utilization is below target.", "citation_ids": [2]},
                    ],
                    "recommended_actions": [
                        {"text": "Review overtime by site.", "citation_ids": []},
                        {"text": "Audit truck utilization by lane.", "citation_ids": []},
                    ],
                }
            )
        )
        mock_uuid_scan.side_effect = lambda module, module_index=None: (
            [{"path": "$.modules[0].direct_answer.text", "match": "uuid"}]
            if module.get("module_id") == "loss-leakage"
            else []
        )

        client = TestClient(app)
        response = client.post(
            "/v1/insights",
            json={"doc_id": "test-doc", "top_k": 3},
            headers={"x-request-id": "uuid-repair-test"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        losses = next(m for m in payload["modules"] if m["module_id"] == "loss-leakage")
        self.assertEqual(losses["status"], "limited_evidence")
        self.assertGreaterEqual(len(losses["sources"]), 1)
        for text in collect_visible_text_fields(losses):
            self.assertNotRegex(text, UUID_PATTERN)

    @patch("app.main.get_gemini_client")
    @patch("app.main.generate_content_with_retry")
    @patch("app.main.retrieve_matches")
    @patch("app.main.get_doc_path")
    def test_prompt_never_receives_raw_match_text(
        self,
        mock_doc_path,
        mock_retrieve,
        mock_generate,
        _mock_gemini,
    ):
        _mock_doc_exists(mock_doc_path)
        mock_retrieve.return_value = SAMPLE_MATCHES
        captured_prompts: list[str] = []

        def capture_prompt(*args, **kwargs):
            prompt = kwargs.get("full_prompt")
            if prompt is None and len(args) > 2:
                prompt = args[2]
            captured_prompts.append(prompt or "")
            return MagicMock(text=None)

        mock_generate.side_effect = capture_prompt

        client = TestClient(app)
        response = client.post(
            "/v1/insights",
            json={"doc_id": "test-doc", "top_k": 3},
            headers={"x-request-id": "prompt-clean-test"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(captured_prompts), 0)
        for prompt in captured_prompts:
            self.assertNotRegex(prompt, UUID_PATTERN)
            self.assertNotRegex(prompt, CHUNK_PATTERN)
            self.assertNotIn("fdc6d32b-0225-4f9e-a84b-8f3b5f496d4f:0", prompt)


if __name__ == "__main__":
    unittest.main()
