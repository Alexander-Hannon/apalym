"use client";

import { type ReactNode, useMemo, useState } from "react";
import { postForm, postJson } from "./lib/api";

type UploadResp = {
  ok: boolean;
  doc_id: string;
  filename: string;
  size_bytes: number;
  tenant_id: string;
  request_id: string;
};

type IndexResp = {
  ok: boolean;
  doc_id: string;
  tenant_id: string;
  num_chunks: number;
  embed_dim: number;
  upserted_count: number;
  request_id: string;
};

type Citation = {
  source_id: string;
  doc_id?: string | null;
  chunk_id?: string | null;
  start?: number | null;
  end?: number | null;
  score?: number | null;
};

type RetrievalMatch = {
  id: string;
  score?: number;
  text: string;
  metadata?: {
    doc_id?: string;
    chunk_id?: string;
    start?: number;
    end?: number;
  };
};

type AskResp = {
  ok: boolean;
  tenant_id: string;
  request_id: string;
  query: string;
  answer?: string;
  answer_mode?: "rag" | "extractive";
  brief?: {
    direct_answer: string;
    key_findings: string[];
    recommended_next_actions: string[];
  };
  citations?: Citation[];
  matches?: RetrievalMatch[];
  error?: string;
  message?: string;
};

type InsightModuleStatus = "ok" | "fallback" | "failed" | "insufficient_evidence";

type InsightModule = {
  module_id: string;
  title: string;
  status: InsightModuleStatus;
  summary: string;
  direct_answer: string;
  key_findings: string[];
  recommended_actions: string[];
  citations: Citation[];
  matches: RetrievalMatch[];
  error?: string;
};

type InsightsResp = {
  ok: boolean;
  doc_id: string;
  tenant_id: string;
  request_id: string;
  module_count: number;
  successful_modules: number;
  modules: InsightModule[];
};

type ChatMessage = { role: "user" | "assistant"; text: string };

type InsightBrief = {
  directAnswer: string;
  keyFindings: string[];
  recommendedActions: string[];
};

const INSIGHT_TABS = [
  { moduleId: "loss-leakage", label: "Loss" },
  { moduleId: "top-cost-drivers", label: "Costs" },
  { moduleId: "key-risks", label: "Risks" },
  { moduleId: "margin-opportunities", label: "Margins" },
  { moduleId: "trends-anomalies", label: "Trends" },
] as const;

function makeRequestId() {
  return `WEB-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function asErrorMessage(error: unknown, fallback: string) {
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

function normalizeListLine(line: string) {
  return line
    .replace(/^((?:[-•*]|\d+[.)])\s+)+/, "")
    .replace(/\s+/g, " ")
    .trim();
}

function normalizeCitationToken(raw: string): string {
  return raw.replace(/^\[|\]$/g, "").replace(/\s+/g, "").toLowerCase();
}

function splitCitationCandidates(tagPayload: string): string[] {
  return tagPayload
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => normalizeCitationToken(part));
}

function stripRawUuidCitationNoise(text: string): string {
  if (!text) return text;
  // Remove stray raw UUID citation bundles from model output while keeping prose intact.
  return text
    .replace(/\[[0-9a-f]{8}-[0-9a-f-]{27}(?:\s*,\s*[0-9a-f]{8}-[0-9a-f-]{27})*\]/gi, "")
    .replace(/\s{2,}/g, " ")
    .trim();
}

function cleanExecutiveActionText(text: string): string {
  if (!text) return "";
  return stripRawUuidCitationNoise(text).replace(/\[[^\]]*:[^\]]*\]/g, "").replace(/\s{2,}/g, " ").trim();
}

function splitSentences(text: string) {
  return text
    .split(/(?<=[.!?])\s+/)
    .map((segment) => segment.trim())
    .filter(Boolean);
}

function buildDynamicActions(question: string, findings: string[]): string[] {
  const q = question.trim().toLowerCase();
  const findingPreview = findings[0] ?? "";

  if (/(aging|receivable|a\/r|invoice|overdue)/.test(q)) {
    return [
      "Review AR aging over 60 days and assign collection owners for the largest overdue balances.",
      "Compare overdue exposure by customer segment and escalate the highest-risk accounts this cycle.",
    ];
  }
  if (/(fleet|vehicle|van|maintenance)/.test(q)) {
    return [
      "Audit fleet maintenance costs by vehicle and prioritize units with repeat high-cost repairs.",
      "Separate preventive vs. reactive maintenance spend and schedule interventions for outlier vans.",
    ];
  }
  if (/(discount|pricing|margin|category)/.test(q)) {
    return [
      "Check discounting by category and flag segments where discount depth is eroding margin.",
      "Review high-discount transactions for approval-policy breaches and adjust pricing controls.",
    ];
  }

  const focusedQuestion =
    question.trim().length > 90 ? `${question.trim().slice(0, 87).trimEnd()}...` : question.trim();
  const evidenceHint = findingPreview ? ` Start with: ${findingPreview}` : "";
  return [
    `Drill into '${focusedQuestion}' by segment, owner, and period to isolate the highest-impact outliers.${evidenceHint}`,
    "Translate the top cited finding into an owner, target metric, and review date for this reporting cycle.",
  ];
}

function buildInsightBrief(
  answerText: string,
  question: string,
  serverBrief?: AskResp["brief"]
): InsightBrief {
  if (serverBrief) {
    return {
      directAnswer: serverBrief.direct_answer,
      keyFindings: serverBrief.key_findings,
      recommendedActions: serverBrief.recommended_next_actions,
    };
  }

  const normalizedAnswer = answerText.replace(/\r/g, "").trim();
  const lines = normalizedAnswer
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean);

  const sentenceCandidates = splitSentences(lines.join(" "));
  const bulletFindings = lines
    .filter((line) => /^((?:[-•*])|(?:\d+[.)]))\s+/.test(line))
    .map(normalizeListLine)
    .filter(Boolean);

  const directAnswer = sentenceCandidates[0] ?? "No answer available yet.";
  const keyFindings =
    bulletFindings.length > 0
      ? bulletFindings.slice(0, 4)
      : sentenceCandidates.slice(1, 5).length > 0
        ? sentenceCandidates.slice(1, 5)
        : [directAnswer];

  const compactQuestion = question.trim().replace(/\s+/g, " ");
  const shortQuestion =
    compactQuestion.length > 70 ? `${compactQuestion.slice(0, 67).trimEnd()}...` : compactQuestion;

  const recommendedActions = buildDynamicActions(shortQuestion, keyFindings);
  return { directAnswer, keyFindings, recommendedActions };
}

function renderInlineMarkdown(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const regex = /(\*\*[^*]+\*\*|\*[^*]+\*|_[^_]+_)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(<span key={`${keyPrefix}-txt-${lastIndex}`}>{text.slice(lastIndex, match.index)}</span>);
    }

    const token = match[0];
    if (token.startsWith("**") && token.endsWith("**")) {
      nodes.push(<strong key={`${keyPrefix}-bold-${match.index}`}>{token.slice(2, -2)}</strong>);
    } else {
      nodes.push(<em key={`${keyPrefix}-em-${match.index}`}>{token.slice(1, -1)}</em>);
    }
    lastIndex = match.index + token.length;
  }

  if (lastIndex < text.length) {
    nodes.push(<span key={`${keyPrefix}-tail-${lastIndex}`}>{text.slice(lastIndex)}</span>);
  }

  return nodes.length > 0 ? nodes : [<span key={`${keyPrefix}-full`}>{text}</span>];
}

function renderTextWithCitationChips(text: string, citationOrder: Map<string, number>): ReactNode[] {
  const cleanedText = stripRawUuidCitationNoise(text);
  return cleanedText
    .split(/(\[[^\]]+:[^\]]+\])/g)
    .filter(Boolean)
    .flatMap((part, index) => {
      const tag = part.match(/^\[([^\]]+:[^\]]+)\]$/)?.[1];
      if (!tag) return renderInlineMarkdown(part, `txt-${index}`);

      const candidateOrders = splitCitationCandidates(tag)
        .map((candidate) => citationOrder.get(candidate))
        .filter((value): value is number => typeof value === "number");
      const uniqueOrders = Array.from(new Set(candidateOrders));
      if (uniqueOrders.length === 0) return [];

      return uniqueOrders.map((order, citationIdx) => (
        <span key={`tag-${index}-${citationIdx}`} className="inline-citation-chip">
          [{order}]
        </span>
      ));
    });
}

function buildCitationOrder(citations: Citation[]): Map<string, number> {
  const lookup = new Map<string, number>();
  citations.forEach((citation, index) => {
    lookup.set(normalizeCitationToken(citation.source_id), index + 1);
    if (citation.doc_id && citation.chunk_id) {
      lookup.set(normalizeCitationToken(`${citation.doc_id}:${citation.chunk_id}`), index + 1);
    }
  });
  return lookup;
}

function statusHintForInsight(status: InsightModuleStatus): string {
  if (status === "ok") return "";
  if (status === "insufficient_evidence") return "Limited evidence is available for this module in the current document.";
  if (status === "fallback") return "Using fallback synthesis due to temporary model constraints.";
  return "This module could not be fully generated for the current run.";
}

export default function Home() {
  const tenantId = "demo-tenant";
  const [requestId] = useState(makeRequestId());
  const [file, setFile] = useState<File | null>(null);
  const [doc, setDoc] = useState<UploadResp | null>(null);
  const [indexed, setIndexed] = useState<IndexResp | null>(null);
  const [insights, setInsights] = useState<InsightModule[]>([]);
  const [activeInsightId, setActiveInsightId] = useState<string>(INSIGHT_TABS[0].moduleId);

  const [query, setQuery] = useState("");
  const [chat, setChat] = useState<ChatMessage[]>([]);
  const [citations, setCitations] = useState<Citation[]>([]);
  const [sourceMatches, setSourceMatches] = useState<RetrievalMatch[]>([]);
  const [briefFromServer, setBriefFromServer] = useState<AskResp["brief"]>();
  const [answerMode, setAnswerMode] = useState<AskResp["answer_mode"] | null>(null);
  const [busy, setBusy] = useState<null | "upload" | "insights" | "ask">(null);
  const [err, setErr] = useState<string | null>(null);

  const headers = useMemo(
    () => ({
      "x-request-id": requestId,
      "x-tenant-id": tenantId,
    }),
    [requestId, tenantId]
  );

  const canUpload = Boolean(file) && busy === null;
  const canRegenerateInsights = Boolean(indexed) && busy === null;
  const canAsk = Boolean(indexed) && busy === null && query.trim().length > 0;
  const latestQuestion = [...chat].reverse().find((entry) => entry.role === "user")?.text ?? "";
  const latestAssistantMessage = [...chat].reverse().find((entry) => entry.role === "assistant")?.text ?? "";

  const insightsById = useMemo(() => {
    const map = new Map<string, InsightModule>();
    insights.forEach((module) => map.set(module.module_id, module));
    return map;
  }, [insights]);

  const selectedInsightId = useMemo(() => {
    if (insightsById.has(activeInsightId)) return activeInsightId;
    const firstAvailable = INSIGHT_TABS.find((tab) => insightsById.has(tab.moduleId));
    return firstAvailable?.moduleId ?? activeInsightId;
  }, [activeInsightId, insightsById]);

  const selectedInsight = insightsById.get(selectedInsightId);

  const selectedCitationOrder = useMemo(
    () => buildCitationOrder(selectedInsight?.citations ?? []),
    [selectedInsight]
  );

  const selectedMatchesBySource = useMemo(() => {
    const lookup = new Map<string, RetrievalMatch>();
    (selectedInsight?.matches ?? []).forEach((match) => lookup.set(match.id, match));
    return lookup;
  }, [selectedInsight]);

  const citationOrder = useMemo(() => buildCitationOrder(citations), [citations]);

  const matchesBySource = useMemo(() => {
    const lookup = new Map<string, RetrievalMatch>();
    sourceMatches.forEach((match) => {
      lookup.set(match.id, match);
    });
    return lookup;
  }, [sourceMatches]);

  const insightBrief = useMemo(
    () => buildInsightBrief(latestAssistantMessage, latestQuestion, briefFromServer),
    [latestAssistantMessage, latestQuestion, briefFromServer]
  );

  async function fetchInsights(docId: string, clearOldInsights = false) {
    if (clearOldInsights) setInsights([]);

    setBusy("insights");
    try {
      const resp = (await postJson("/v1/insights", { doc_id: docId, top_k: 4 }, headers)) as InsightsResp;
      setInsights(resp.modules ?? []);
    } catch (e: unknown) {
      setErr(asErrorMessage(e, "Insights generation failed"));
    } finally {
      setBusy(null);
    }
  }

  async function uploadAndIndex() {
    if (!file) return;
    setErr(null);
    setBusy("upload");

    try {
      const form = new FormData();
      form.append("file", file);
      const up = (await postForm("/v1/docs/upload", form, headers)) as UploadResp;
      setDoc(up);

      const CHUNK_SIZE = 700;
      const OVERLAP = 80;
      const idx = (await postJson(
        `/v1/docs/${up.doc_id}/index?chunk_size=${CHUNK_SIZE}&overlap=${OVERLAP}`,
        {},
        headers
      )) as IndexResp;
      setIndexed(idx);
      setActiveInsightId(INSIGHT_TABS[0].moduleId);
      setInsights([]);

      setQuery("");
      setChat([]);
      setCitations([]);
      setSourceMatches([]);
      setBriefFromServer(undefined);
      setAnswerMode(null);

      setBusy(null);
      await fetchInsights(up.doc_id, true);
    } catch (e: unknown) {
      setBusy(null);
      setErr(asErrorMessage(e, "Upload/index failed"));
    }
  }

  async function ask() {
    const trimmedQuery = query.trim();
    if (!trimmedQuery) return;
    const activeDocId = indexed?.doc_id ?? doc?.doc_id;
    if (!activeDocId) {
      setErr("Upload and index a document before asking a question.");
      return;
    }

    setErr(null);
    setBusy("ask");
    setAnswerMode(null);
    setBriefFromServer(undefined);
    setChat((c) => [...c, { role: "user", text: trimmedQuery }]);

    try {
      const resp = (await postJson(
        "/v1/ask",
        { query: trimmedQuery, top_k: 3, doc_id: activeDocId },
        headers
      )) as AskResp;

      if (!resp.ok) {
        const msg = resp.message || "Ask failed";
        setChat((c) => [...c, { role: "assistant", text: `⚠️ ${msg}` }]);
        setCitations(resp.citations ?? []);
        setSourceMatches(resp.matches ?? []);
        setBriefFromServer(resp.brief);
        setAnswerMode(resp.answer_mode ?? null);
        return;
      }

      setChat((c) => [...c, { role: "assistant", text: resp.answer ?? "" }]);
      setCitations(resp.citations ?? []);
      setSourceMatches(resp.matches ?? []);
      setBriefFromServer(resp.brief);
      setAnswerMode(resp.answer_mode ?? "rag");
    } catch (e: unknown) {
      setErr(asErrorMessage(e, "Ask failed"));
      setSourceMatches([]);
      setBriefFromServer(undefined);
    } finally {
      setBusy(null);
    }
  }

  const uploadHint =
    busy === "upload"
      ? "Uploading and indexing your document..."
      : indexed
        ? "Document ready for diagnosis."
        : file
          ? "File selected. Start diagnosis."
          : "Choose a document to begin.";

  return (
    <main className="demo-shell executive-shell">
      <header className="hero-block executive-hero">
        <h1>Executive Brief</h1>
        <p className="hero-subtitle">
          Upload one business document and instantly receive a focused diagnosis with findings, actions, and sources.
        </p>
      </header>

      <section className="surface-card upload-card">
        <div className="surface-heading">
          <h2>Document upload</h2>
          <p className="section-copy">PDF, CSV, or XLSX are indexed automatically.</p>
        </div>

        <label className="file-input-wrap">
          <span className="file-input-label">Document file</span>
          <input
            className="file-input"
            type="file"
            accept="application/pdf,text/csv,.csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,.xlsx"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </label>

        {file && (
          <p className="selected-file">
            Selected: <strong>{file.name}</strong>
          </p>
        )}

        <button onClick={uploadAndIndex} disabled={!canUpload} className="primary-btn full-width">
          {busy === "upload" ? "Uploading..." : "Upload and diagnose"}
        </button>

        <p className="status-note">{uploadHint}</p>
        {doc?.filename && <p className="document-pill">Current document: {doc.filename}</p>}
      </section>

      <section className="surface-card brief-card">
        <div className="surface-heading">
          <h2>Business diagnosis</h2>
          <p className="section-copy">One insight at a time, tuned for executive review.</p>
        </div>

        {indexed && (
          <div className="insight-nav" role="tablist" aria-label="Insight modules">
            {INSIGHT_TABS.map((tab) => (
              <button
                key={tab.moduleId}
                type="button"
                className={`insight-tab ${selectedInsightId === tab.moduleId ? "active" : ""}`}
                onClick={() => setActiveInsightId(tab.moduleId)}
              >
                {tab.label}
              </button>
            ))}
          </div>
        )}

        {!indexed && <div className="empty-state">Upload and index a document to generate your executive brief.</div>}

        {busy === "insights" && (
          <div className="empty-state">
            Building your diagnosis: loss leakage, cost pressure, risk, margin opportunities, and trend signals.
          </div>
        )}

        {indexed && !busy && !selectedInsight && (
          <div className="empty-state">No insight data yet. Regenerate the brief after indexing finishes.</div>
        )}

        {selectedInsight && (
          <article className="brief-focus">
            <div className="brief-head">
              <h3 className="brief-title">{selectedInsight.title}</h3>
              <button className="secondary-btn" disabled={!canRegenerateInsights} onClick={() => fetchInsights(indexed!.doc_id)}>
                Regenerate
              </button>
            </div>

            {statusHintForInsight(selectedInsight.status) && (
              <p className="brief-status-note">{statusHintForInsight(selectedInsight.status)}</p>
            )}

            <section className="brief-section">
              <h4>Direct answer</h4>
              <p className="brief-direct">
                {renderTextWithCitationChips(
                  selectedInsight.direct_answer || selectedInsight.summary,
                  selectedCitationOrder
                )}
              </p>
            </section>

            <section className="brief-section">
              <h4>Key findings</h4>
              <ul className="brief-findings">
                {selectedInsight.key_findings.map((finding, index) => (
                  <li key={index}>{renderTextWithCitationChips(finding, selectedCitationOrder)}</li>
                ))}
              </ul>
            </section>

            <section className="brief-section brief-actions-wrap">
              <h4>Recommended actions</h4>
              <ul className="brief-actions">
                {selectedInsight.recommended_actions.map((action, index) => (
                  <li key={index}>{cleanExecutiveActionText(action)}</li>
                ))}
              </ul>
            </section>

            <section className="brief-section">
              <h4>Sources</h4>
              {selectedInsight.citations.length === 0 ? (
                <p className="sources-empty">No source excerpts were available for this section.</p>
              ) : (
                <div className="brief-sources">
                  {selectedInsight.citations.map((citation, index) => {
                    const match = selectedMatchesBySource.get(citation.source_id);

                    return (
                      <details key={`${selectedInsight.module_id}-${citation.source_id}`} className="source-card compact-source">
                        <summary>
                          <span className="source-rank">[{index + 1}]</span>
                          <div className="source-summary-main">
                            <span className="source-id">Source excerpt {index + 1}</span>
                          </div>
                        </summary>
                        {match?.text && <p className="source-excerpt">{match.text}</p>}
                      </details>
                    );
                  })}
                </div>
              )}
            </section>
          </article>
        )}
      </section>

      <section className="surface-card ask-card">
        <div className="surface-heading">
          <h2>Follow-up Q&A</h2>
          <p className="section-copy">Ask targeted questions against the same document when you need deeper detail.</p>
        </div>

        <div className="ask-row">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Ask a follow-up question..."
            className="query-input"
          />
          <button onClick={ask} disabled={!canAsk} className="primary-btn ask-btn">
            {busy === "ask" ? "Asking..." : "Ask"}
          </button>
        </div>

        {!indexed && <p className="helper-note">Upload and index a document to enable follow-up Q&A.</p>}

        <div className="answer-panel ask-answer-panel">
          {latestQuestion === "" ? (
            <div className="empty-state">Your follow-up response appears here.</div>
          ) : (
            <div className="brief-shell">
              <div className="question-brief">
                <span className="question-brief-label">Question</span>
                <p>{latestQuestion}</p>
              </div>

              {latestAssistantMessage && (
                <article className="insight-card">
                  <div className="insight-card-header">
                    <h4>Answer</h4>
                    {answerMode && (
                      <span className={`answer-mode-chip ${answerMode}`}>
                        {answerMode === "rag" ? "AI-grounded" : "Extractive fallback"}
                      </span>
                    )}
                  </div>

                  <p className="insight-direct-answer">
                    {renderTextWithCitationChips(insightBrief.directAnswer, citationOrder)}
                  </p>

                  <div className="insight-grid">
                    <section className="insight-section findings">
                      <h5>Key findings</h5>
                      <ul>
                        {insightBrief.keyFindings.map((finding, index) => (
                          <li key={index}>{renderTextWithCitationChips(finding, citationOrder)}</li>
                        ))}
                      </ul>
                    </section>

                    <section className="insight-section actions">
                      <h5>Recommended actions</h5>
                      <ul>
                        {insightBrief.recommendedActions.map((action, index) => (
                          <li key={index}>{action}</li>
                        ))}
                      </ul>
                    </section>
                  </div>
                </article>
              )}
            </div>
          )}
        </div>

        {citations.length > 0 && (
          <div className="sources-block">
            <h3>Supporting sources ({citations.length})</h3>
            <div className="sources-list">
              {citations.map((citation, index) => {
                const match = matchesBySource.get(citation.source_id);

                return (
                  <details key={citation.source_id} className="source-card compact-source">
                    <summary>
                      <span className="source-rank">[{index + 1}]</span>
                      <div className="source-summary-main">
                        <span className="source-id">Source excerpt {index + 1}</span>
                      </div>
                    </summary>
                    {match?.text && <p className="source-excerpt">{match.text}</p>}
                  </details>
                );
              })}
            </div>
          </div>
        )}
      </section>

      {err && (
        <div className="error-banner">
          <strong>Problem:</strong> {err}
        </div>
      )}
    </main>
  );
}
