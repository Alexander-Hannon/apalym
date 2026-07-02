import unittest

from app.insight_contract import (
    CitedField,
    InsightModuleOutput,
    EvidenceLimitations,
    SourceEvidence,
    build_limited_evidence_module,
    finalize_insight_module,
    parse_cited_field,
    serialize_module_for_api,
    strip_inline_citations_from_text,
    text_has_citation_syntax,
    text_has_internal_artifact,
    validate_module_output,
)


class InsightContractResetTests(unittest.TestCase):
    def test_text_fields_strip_citation_syntax_but_preserve_prose(self):
        cited = parse_cited_field(
            {"text": "Labor pressure is rising. [1, 2]", "citation_ids": [1, 2]},
            max_sources=3,
        )
        self.assertEqual(cited.citation_ids, [1, 2])
        self.assertEqual(cited.text, "Labor pressure is rising.")
        self.assertTrue(text_has_citation_syntax("Labor pressure is rising. [1, 2]"))

    def test_internal_uuid_bracket_rejected(self):
        text = "Issue found [c394bf1b-9bce-4a35-9601-23632478f468:67]"
        self.assertTrue(text_has_internal_artifact(text))
        cleaned = strip_inline_citations_from_text(text)
        self.assertNotIn("[", cleaned)

    def test_chopped_fragment_rejected(self):
        self.assertTrue(text_has_internal_artifact("holds Financial Capex execution risk"))
        self.assertTrue(text_has_internal_artifact("Medium Medium ROI tracking"))

    def test_ready_module_validates(self):
        module = InsightModuleOutput(
            module_key="losses",
            module_id="loss-leakage",
            title="Where are we losing money?",
            status="ready",
            confidence="high",
            direct_answer=CitedField(text="Warehouse overtime is above target and is pressuring margins.", citation_ids=[1]),
            key_findings=[
                CitedField(text="Truck utilization is below the operating target.", citation_ids=[1]),
                CitedField(text="Collections timing may be affecting cash conversion.", citation_ids=[2]),
            ],
            recommended_actions=[
                CitedField(text="Review overtime by site and shift for the last 30 days.", citation_ids=[1]),
                CitedField(text="Audit truck utilization by lane and customer.", citation_ids=[2]),
            ],
            evidence_limitations=EvidenceLimitations(message=None, validation_questions=[]),
            sources=[
                SourceEvidence(
                    citation_id=1,
                    document_name="meridian_cold_chain_financial_package.pdf",
                    page=7,
                    excerpt="Warehouse overtime was 10.4% against a target below 7.5%.",
                ),
                SourceEvidence(
                    citation_id=2,
                    document_name="meridian_cold_chain_financial_package.pdf",
                    page=8,
                    excerpt="Days sales outstanding was 38.6 days versus a target below 35 days.",
                ),
            ],
        )
        ok, reasons = validate_module_output(module)
        self.assertTrue(ok, reasons)

    def test_finalize_returns_limited_evidence_for_bad_generation(self):
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
        self.assertEqual(module.status, "limited_evidence")
        self.assertGreaterEqual(len(module.evidence_limitations.validation_questions), 2)
        self.assertFalse(text_has_internal_artifact(module.direct_answer.text))
        self.assertNotIn("ductivity", module.direct_answer.text.lower())
        self.assertTrue(reasons)

    def test_serialize_rebuilds_contaminated_ready_module_with_one_source(self):
        contaminated = InsightModuleOutput(
            module_key="losses",
            module_id="loss-leakage",
            title="Where are we losing money?",
            status="ready",
            confidence="high",
            direct_answer=CitedField(text="ductivity problem Warehouse overtime % 11.8%", citation_ids=[1]),
            key_findings=[
                CitedField(text="Finding one.", citation_ids=[1]),
                CitedField(text="Finding two.", citation_ids=[1]),
            ],
            recommended_actions=[
                CitedField(text="Action one.", citation_ids=[1]),
                CitedField(text="Action two.", citation_ids=[1]),
            ],
            evidence_limitations=EvidenceLimitations(message=None, validation_questions=[]),
            sources=[
                SourceEvidence(
                    citation_id=1,
                    document_name="meridian_cold_chain_financial_package.pdf",
                    page=7,
                    excerpt="Warehouse overtime was 10.4% against a target below 7.5%.",
                )
            ],
        )
        payload = serialize_module_for_api(contaminated)
        self.assertEqual(payload["status"], "limited_evidence")
        self.assertFalse(text_has_internal_artifact(payload["direct_answer"]["text"]))

    def test_serialize_preserves_ready_module_with_three_sources(self):
        contaminated = InsightModuleOutput(
            module_key="losses",
            module_id="loss-leakage",
            title="Where are we losing money?",
            status="ready",
            confidence="high",
            direct_answer=CitedField(
                text="Warehouse overtime is above target and appears to be a recurring operational issue.",
                citation_ids=[1],
            ),
            key_findings=[
                CitedField(text="Truck utilization is below the operating target.", citation_ids=[2]),
            ],
            recommended_actions=[
                CitedField(text="Review overtime by site and shift.", citation_ids=[1]),
            ],
            evidence_limitations=EvidenceLimitations(message=None, validation_questions=[]),
            sources=[
                SourceEvidence(
                    citation_id=1,
                    document_name="meridian_cold_chain_financial_package.pdf",
                    page=7,
                    excerpt="Warehouse overtime was 10.4% against a target below 7.5%.",
                ),
                SourceEvidence(
                    citation_id=2,
                    document_name="meridian_cold_chain_financial_package.pdf",
                    page=8,
                    excerpt="Days sales outstanding was 38.6 days versus a target below 35 days.",
                ),
                SourceEvidence(
                    citation_id=3,
                    document_name="meridian_cold_chain_financial_package.pdf",
                    page=9,
                    excerpt="Truck utilization remained below planned efficiency levels.",
                ),
            ],
        )
        payload = serialize_module_for_api(contaminated)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["confidence"], "medium")
        self.assertGreaterEqual(len(payload["key_findings"]), 2)
        self.assertGreaterEqual(len(payload["recommended_actions"]), 2)
        self.assertEqual(len(payload["sources"]), 3)


if __name__ == "__main__":
    unittest.main()
