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

function makeRequestId() {
  return `WEB-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function asErrorMessage(error: unknown, fallback: string) {
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

function statusClassName(state: "idle" | "ready" | "working" | "complete") {
  return `status-chip status-${state}`;
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
  return text
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

function confidenceBand(score: number | null | undefined) {
  if (typeof score !== "number") return { label: "No score", tone: "low" as const };
  if (score >= 0.75) return { label: "High confidence", tone: "high" as const };
  if (score >= 0.55) return { label: "Medium confidence", tone: "medium" as const };
  return { label: "Low confidence", tone: "low" as const };
}

function formatModuleStatus(status: InsightModuleStatus): string {
  if (status === "ok") return "Ready";
  if (status === "fallback") return "Fallback";
  if (status === "insufficient_evidence") return "Limited evidence";
  return "Failed";
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

function scoreInsightCoverage(modules: InsightModule[]) {
  return modules.reduce(
    (acc, module) => {
      if (module.status === "ok") acc.ok += 1;
      if (module.status === "fallback") acc.fallback += 1;
      if (module.status === "insufficient_evidence") acc.insufficient += 1;
      if (module.status === "failed") acc.failed += 1;
      return acc;
    },
    { ok: 0, fallback: 0, insufficient: 0, failed: 0 }
  );
}

export default function Home() {
  const tenantId = "demo-tenant";

  const [requestId, setRequestId] = useState(makeRequestId());
  const [file, setFile] = useState<File | null>(null);

  const [doc, setDoc] = useState<UploadResp | null>(null);
  const [indexed, setIndexed] = useState<IndexResp | null>(null);
  const [insights, setInsights] = useState<InsightModule[]>([]);
  const [insightsMeta, setInsightsMeta] = useState<{ moduleCount: number; successfulModules: number } | null>(null);

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
  const hasAssistantAnswer = chat.some((entry) => entry.role === "assistant");
  const latestQuestion = [...chat].reverse().find((entry) => entry.role === "user")?.text ?? "";
  const latestAssistantMessage = [...chat].reverse().find((entry) => entry.role === "assistant")?.text ?? "";

  const citationOrder = useMemo(() => buildCitationOrder(citations), [citations]);

  const matchesBySource = useMemo(() => {
    const lookup = new Map<string, RetrievalMatch>();
    sourceMatches.forEach((match) => {
      lookup.set(match.id, match);
    });
    return lookup;
  }, [sourceMatches]);

  const insightsCoverage = useMemo(() => scoreInsightCoverage(insights), [insights]);

  const insightBrief = useMemo(
    () => buildInsightBrief(latestAssistantMessage, latestQuestion, briefFromServer),
    [latestAssistantMessage, latestQuestion, briefFromServer]
  );

  async function fetchInsights(docId: string, clearOldInsights = false) {
    if (clearOldInsights) {
      setInsights([]);
      setInsightsMeta(null);
    }

    setBusy("insights");
    try {
      const resp = (await postJson("/v1/insights", { doc_id: docId, top_k: 4 }, headers)) as InsightsResp;
      setInsights(resp.modules ?? []);
      setInsightsMeta({
        moduleCount: resp.module_count ?? (resp.modules?.length ?? 0),
        successfulModules: resp.successful_modules ?? 0,
      });
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

  const uploadStatus =
    busy === "upload"
      ? { label: "Processing", tone: "working" as const }
      : indexed
        ? { label: "Indexed", tone: "complete" as const }
        : file
          ? { label: "Ready", tone: "ready" as const }
          : { label: "Awaiting file", tone: "idle" as const };

  const insightsStatus =
    busy === "insights"
      ? { label: "Diagnosing", tone: "working" as const }
      : insights.length > 0
        ? { label: "Ready", tone: "complete" as const }
        : indexed
          ? { label: "Queued", tone: "ready" as const }
          : { label: "Upload first", tone: "idle" as const };

  const askStatus =
    busy === "ask"
      ? { label: "Generating answer", tone: "working" as const }
      : hasAssistantAnswer
        ? { label: "Answered", tone: "complete" as const }
        : indexed
          ? { label: "Ready", tone: "ready" as const }
          : { label: "Upload first", tone: "idle" as const };

  return (
    <main className="demo-shell">
      <header className="hero-block">
        <p className="hero-kicker">Product Demo</p>
        <h1>Apalym</h1>
        <p className="hero-subtitle">
          Upload once, then get an automatic business diagnosis dashboard grounded in citations from your document.
        </p>

        <div className="meta-strip">
          <span className="meta-pill">
            <span className="meta-pill-label">tenant</span>
            <code>{tenantId}</code>
          </span>
          <span className="meta-pill">
            <span className="meta-pill-label">request_id</span>
            <code>{requestId}</code>
          </span>
          <button onClick={() => setRequestId(makeRequestId())} className="secondary-btn meta-refresh-btn">
            New request ID
          </button>
        </div>
      </header>

      <div className="workflow-grid">
        <section className="surface-card">
          <div className="section-heading">
            <div>
              <p className="step-kicker">Step 1</p>
              <h2>Upload document</h2>
            </div>
            <span className={statusClassName(uploadStatus.tone)}>{uploadStatus.label}</span>
          </div>

          <p className="section-copy">
            Choose a PDF, CSV, or XLSX file. The app uploads and indexes it automatically with stable defaults.
          </p>

          <label className="file-input-wrap">
            <span className="file-input-label">Document file (PDF, CSV, or XLSX)</span>
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
            {busy === "upload" ? "Uploading and indexing..." : "Upload file"}
          </button>

          <div className="meta-table">
            <div className="meta-row">
              <span className="meta-key">Document</span>
              <span className="meta-value">{doc?.filename ?? "--"}</span>
            </div>
            <div className="meta-row">
              <span className="meta-key">doc_id</span>
              <span className="meta-value">{doc?.doc_id ?? "--"}</span>
            </div>
            <div className="meta-row">
              <span className="meta-key">Indexed chunks</span>
              <span className="meta-value">{indexed ? `${indexed.upserted_count}` : "--"}</span>
            </div>
          </div>
        </section>

        <div className="workflow-main">
          <section className="surface-card">
            <div className="section-heading">
              <div>
                <p className="step-kicker">Step 2</p>
                <h2>Automatic insights dashboard</h2>
              </div>
              <span className={statusClassName(insightsStatus.tone)}>{insightsStatus.label}</span>
            </div>

            <p className="section-copy">
              After indexing, business insight modules are generated automatically using document-scoped retrieval.
            </p>

            {indexed && (
              <div className="dashboard-actions">
                <button
                  className="secondary-btn"
                  disabled={!canRegenerateInsights}
                  onClick={() => fetchInsights(indexed.doc_id)}
                >
                  {busy === "insights" ? "Generating insights..." : "Regenerate insights"}
                </button>
              </div>
            )}

            {!indexed && <p className="helper-note">Upload and index a document to generate insights automatically.</p>}

            {busy === "insights" && (
              <div className="empty-state">
                Generating insight modules: where losses occur, cost drivers, risks, margin opportunities, and anomalies.
              </div>
            )}

            {indexed && !busy && insights.length === 0 && (
              <div className="empty-state">
                No insight modules yet. Use <strong>Regenerate insights</strong> to run diagnostics for this document.
              </div>
            )}

            {insights.length > 0 && (
              <>
                <div className="insights-summary-row">
                  <div className="insights-summary-card">
                    <span>Modules</span>
                    <strong>{insightsMeta?.moduleCount ?? insights.length}</strong>
                  </div>
                  <div className="insights-summary-card">
                    <span>Successful</span>
                    <strong>{insightsMeta?.successfulModules ?? insightsCoverage.ok}</strong>
                  </div>
                  <div className="insights-summary-card">
                    <span>Fallbacks</span>
                    <strong>{insightsCoverage.fallback}</strong>
                  </div>
                </div>

                {insights.map((module) => {
                  const moduleCitationOrder = buildCitationOrder(module.citations ?? []);
                  const moduleMatchesBySource = new Map<string, RetrievalMatch>();
                  (module.matches ?? []).forEach((match) => moduleMatchesBySource.set(match.id, match));

                  return (
                    <article key={module.module_id} className="insight-card module-card">
                      <div className="insight-card-header">
                        <h4>{module.title}</h4>
                        <span className={`module-status ${module.status}`}>{formatModuleStatus(module.status)}</span>
                      </div>

                      <p className="insight-direct-answer">
                        {renderTextWithCitationChips(module.direct_answer || module.summary, moduleCitationOrder)}
                      </p>

                      <div className="insight-grid">
                        <section className="insight-section findings">
                          <h5>Key findings</h5>
                          <ul>
                            {(module.key_findings ?? []).map((finding, index) => (
                              <li key={index}>{renderTextWithCitationChips(finding, moduleCitationOrder)}</li>
                            ))}
                          </ul>
                        </section>

                        <section className="insight-section actions">
                          <h5>Recommended actions</h5>
                          <ul>
                            {(module.recommended_actions ?? []).map((action, index) => (
                              <li key={index}>{action}</li>
                            ))}
                          </ul>
                        </section>
                      </div>

                      {module.error && <p className="module-error">Module note: {module.error}</p>}

                      {(module.citations ?? []).length > 0 && (
                        <div className="module-citations">
                          <h5 className="module-citations-title">Citations ({module.citations.length})</h5>
                          <div className="sources-list">
                            {module.citations.map((citation, index) => {
                              const match = moduleMatchesBySource.get(citation.source_id);
                              const confidence = confidenceBand(citation.score);
                              const docLabel = citation.doc_id ? citation.doc_id.slice(0, 8) : "unknown";
                              const chunkLabel = citation.chunk_id ?? "?";
                              const start = citation.start ?? "-";
                              const end = citation.end ?? "-";
                              const score = typeof citation.score === "number" ? citation.score.toFixed(3) : "--";

                              return (
                                <details key={`${module.module_id}-${citation.source_id}`} className="source-card">
                                  <summary>
                                    <span className="source-rank">[{index + 1}]</span>
                                    <div className="source-summary-main">
                                      <span className="source-id">
                                        {docLabel}...:{chunkLabel}
                                      </span>
                                      <span className={`source-confidence ${confidence.tone}`}>{confidence.label}</span>
                                    </div>
                                    <span className="source-range">
                                      score {score} • chars {start}-{end}
                                    </span>
                                  </summary>
                                  {match?.text && <p className="source-excerpt">{match.text}</p>}
                                  <div className="source-note">
                                    Source ID: <code>{citation.source_id}</code>
                                  </div>
                                </details>
                              );
                            })}
                          </div>
                        </div>
                      )}
                    </article>
                  );
                })}
              </>
            )}
          </section>

          <section className="surface-card">
            <div className="section-heading">
              <div>
                <p className="step-kicker">Step 3</p>
                <h2>Ask and review answer</h2>
              </div>
              <span className={statusClassName(askStatus.tone)}>{askStatus.label}</span>
            </div>

            <p className="section-copy">You can still ask follow-up questions against the same indexed document.</p>

            <div className="ask-row">
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Ask a follow-up question about your document..."
                className="query-input"
              />
              <button onClick={ask} disabled={!canAsk} className="primary-btn ask-btn">
                {busy === "ask" ? "Asking..." : "Ask"}
              </button>
            </div>

            {!indexed && <p className="helper-note">Upload and index a file to enable Ask.</p>}

            <div className="answer-panel">
              <div className="response-module-header">
                <h3>Response</h3>
                <span className={`response-state ${hasAssistantAnswer ? "ready" : "idle"}`}>
                  {hasAssistantAnswer ? "Grounded answer" : "Waiting for question"}
                </span>
              </div>
              <p className="response-copy">Ask remains available for deeper investigation after auto-insights.</p>

              {latestQuestion === "" ? (
                <div className="empty-state">Your conversation appears here after you ask a question.</div>
              ) : (
                <div className="brief-shell">
                  <div className="question-brief">
                    <span className="question-brief-label">Question</span>
                    <p>{latestQuestion}</p>
                  </div>

                  {latestAssistantMessage && (
                    <article className="insight-card">
                      <div className="insight-card-header">
                        <h4>Direct answer</h4>
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
                          <h5>Recommended next actions</h5>
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
                <p className="sources-copy">Ranked evidence that grounds the answer and enables quick verification.</p>

                <div className="sources-list">
                  {citations.map((citation, index) => {
                    const match = matchesBySource.get(citation.source_id);
                    const confidence = confidenceBand(citation.score);
                    const docLabel = citation.doc_id ? citation.doc_id.slice(0, 8) : "unknown";
                    const chunkLabel = citation.chunk_id ?? "?";
                    const start = citation.start ?? "-";
                    const end = citation.end ?? "-";
                    const score = typeof citation.score === "number" ? citation.score.toFixed(3) : "--";

                    return (
                      <details key={citation.source_id} className="source-card">
                        <summary>
                          <span className="source-rank">[{index + 1}]</span>
                          <div className="source-summary-main">
                            <span className="source-id">
                              {docLabel}...:{chunkLabel}
                            </span>
                            <span className={`source-confidence ${confidence.tone}`}>{confidence.label}</span>
                          </div>
                          <span className="source-range">
                            score {score} • chars {start}-{end}
                          </span>
                        </summary>
                        {match?.text && <p className="source-excerpt">{match.text}</p>}
                        <div className="source-note">
                          Source ID: <code>{citation.source_id}</code>
                        </div>
                      </details>
                    );
                  })}
                </div>
              </div>
            )}
          </section>
        </div>
      </div>

      {err && (
        <div className="error-banner">
          <strong>Problem:</strong> {err}
        </div>
      )}
    </main>
  );
}
