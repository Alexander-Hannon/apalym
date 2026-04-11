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
  doc_id: string;
  chunk_id: string;
  start: number;
  end: number;
  score: number;
};

type AskResp = {
  ok: boolean;
  tenant_id: string;
  request_id: string;
  query: string;
  answer?: string;
  answer_mode?: "rag" | "extractive";
  citations?: Citation[];
  matches?: RetrievalMatch[];
  error?: string;
  message?: string;
};

type ChatMessage = { role: "user" | "assistant"; text: string };

type RetrievalMatch = {
  id: string;
  score: number;
  text: string;
  metadata?: {
    doc_id?: string;
    chunk_id?: string;
    start?: number;
    end?: number;
  };
};

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

function splitSentences(text: string) {
  return text
    .split(/(?<=[.!?])\s+/)
    .map((segment) => segment.trim())
    .filter(Boolean);
}

function buildInsightBrief(answerText: string, question: string, sourceCount: number): InsightBrief {
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

  const recommendedActions = [
    "Validate the direct answer against source [1] before acting.",
    sourceCount > 1
      ? "Cross-check one additional high-confidence source for consistency."
      : "Capture one additional supporting data point for higher-confidence decisions.",
    shortQuestion
      ? `Ask one focused follow-up on "${shortQuestion}" to close remaining ambiguity.`
      : "Ask a follow-up question to close any remaining ambiguity.",
  ];

  return { directAnswer, keyFindings, recommendedActions };
}

function renderTextWithCitationChips(text: string, citationOrder: Map<string, number>): ReactNode[] {
  return text.split(/(\[[^\]]+:[^\]]+\])/g).filter(Boolean).map((part, index) => {
    const tag = part.match(/^\[([^\]]+:[^\]]+)\]$/)?.[1];
    if (!tag) return <span key={`txt-${index}`}>{part}</span>;

    const order = citationOrder.get(tag);
    if (!order) {
      return (
        <span key={`tag-${index}`} className="inline-citation-chip unknown">
          [?]
        </span>
      );
    }
    return (
      <span key={`tag-${index}`} className="inline-citation-chip">
        [{order}]
      </span>
    );
  });
}

function confidenceBand(score: number) {
  if (score >= 0.75) return { label: "High confidence", tone: "high" as const };
  if (score >= 0.55) return { label: "Medium confidence", tone: "medium" as const };
  return { label: "Low confidence", tone: "low" as const };
}

export default function Home() {
  // demo tenant is fixed for non-technical demos
  const tenantId = "demo-tenant";

  const [requestId, setRequestId] = useState(makeRequestId());
  const [file, setFile] = useState<File | null>(null);

  const [doc, setDoc] = useState<UploadResp | null>(null);
  const [indexed, setIndexed] = useState<IndexResp | null>(null);

  const [query, setQuery] = useState("");
  const [chat, setChat] = useState<ChatMessage[]>([]);
  const [citations, setCitations] = useState<Citation[]>([]);
  const [sourceMatches, setSourceMatches] = useState<RetrievalMatch[]>([]);
  const [answerMode, setAnswerMode] = useState<AskResp["answer_mode"] | null>(null);
  const [busy, setBusy] = useState<null | "upload" | "ask">(null);
  const [err, setErr] = useState<string | null>(null);

  const headers = useMemo(
    () => ({
      "x-request-id": requestId,
      "x-tenant-id": tenantId,
    }),
    [requestId, tenantId]
  );

  const canUpload = Boolean(file) && busy === null;
  const canAsk = Boolean(indexed) && busy === null && query.trim().length > 0;
  const hasAssistantAnswer = chat.some((entry) => entry.role === "assistant");
  const latestQuestion = [...chat].reverse().find((entry) => entry.role === "user")?.text ?? "";
  const latestAssistantMessage = [...chat].reverse().find((entry) => entry.role === "assistant")?.text ?? "";

  const citationOrder = useMemo(() => {
    const lookup = new Map<string, number>();
    citations.forEach((citation, index) => {
      lookup.set(citation.source_id, index + 1);
    });
    return lookup;
  }, [citations]);

  const matchesBySource = useMemo(() => {
    const lookup = new Map<string, RetrievalMatch>();
    sourceMatches.forEach((match) => {
      lookup.set(match.id, match);
    });
    return lookup;
  }, [sourceMatches]);

  const insightBrief = useMemo(
    () => buildInsightBrief(latestAssistantMessage, latestQuestion, citations.length),
    [latestAssistantMessage, latestQuestion, citations.length]
  );

  async function uploadAndIndex() {
    if (!file) return;
    setErr(null);
    setBusy("upload");

    try {
      // 1) Upload
      const form = new FormData();
      form.append("file", file);
      const up = (await postForm("/v1/docs/upload", form, headers)) as UploadResp;
      setDoc(up);

      // 2) Index (hardcoded chunking — NOT user-controlled)
      const CHUNK_SIZE = 700;
      const OVERLAP = 80;

      const idx = (await postJson(
        `/v1/docs/${up.doc_id}/index?chunk_size=${CHUNK_SIZE}&overlap=${OVERLAP}`,
        {},
        headers
      )) as IndexResp;

      setIndexed(idx);
    } catch (e: unknown) {
      setErr(asErrorMessage(e, "Upload/index failed"));
    } finally {
      setBusy(null);
    }
  }

  async function ask() {
    const trimmedQuery = query.trim();
    if (!trimmedQuery) return;

    setErr(null);
    setBusy("ask");
    setAnswerMode(null);

    // Show the user question immediately
    setChat((c) => [...c, { role: "user", text: trimmedQuery }]);

    try {
      const resp = (await postJson("/v1/ask", { query: trimmedQuery, top_k: 5 }, headers)) as AskResp;

      if (!resp.ok) {
        const msg = resp.message || "Ask failed";
        setChat((c) => [...c, { role: "assistant", text: `⚠️ ${msg}` }]);
        setCitations(resp.citations ?? []);
        setSourceMatches(resp.matches ?? []);
        setAnswerMode(resp.answer_mode ?? null);
        return;
      }

      setChat((c) => [...c, { role: "assistant", text: resp.answer ?? "" }]);
      setCitations(resp.citations ?? []);
      setSourceMatches(resp.matches ?? []);
      setAnswerMode(resp.answer_mode ?? "rag");
    } catch (e: unknown) {
      setErr(asErrorMessage(e, "Ask failed"));
      setSourceMatches([]);
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
          Upload a PDF, CSV, or XLSX file, ask a question, and verify every answer with scored source citations.
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
            Choose a PDF, CSV, or XLSX file. We apply stable indexing defaults automatically, with no setup required.
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

        <section className="surface-card">
          <div className="section-heading">
            <div>
              <p className="step-kicker">Step 2</p>
              <h2>Ask and review answer</h2>
            </div>
            <span className={statusClassName(askStatus.tone)}>{askStatus.label}</span>
          </div>

          <p className="section-copy">Ask a question grounded in the uploaded document data.</p>

          <div className="ask-row">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Ask a question about your PDF, CSV, or XLSX..."
              className="query-input"
            />
            <button onClick={ask} disabled={!canAsk} className="primary-btn ask-btn">
              {busy === "ask" ? "Asking..." : "Ask"}
            </button>
          </div>

          {!indexed && <p className="helper-note">Upload and index a PDF, CSV, or XLSX file to enable Ask.</p>}

          <div className="answer-panel">
            <div className="response-module-header">
              <h3>Response</h3>
              <span className={`response-state ${hasAssistantAnswer ? "ready" : "idle"}`}>
                {hasAssistantAnswer ? "Grounded answer" : "Waiting for question"}
              </span>
            </div>
            <p className="response-copy">
              Answers are generated from indexed chunks and paired with scored source references.
            </p>

            {latestQuestion === "" ? (
              <div className="empty-state">
                Your conversation appears here after you ask a question.
              </div>
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

                  return (
                    <details key={citation.source_id} className="source-card">
                      <summary>
                        <span className="source-rank">[{index + 1}]</span>
                        <div className="source-summary-main">
                          <span className="source-id">
                            {citation.doc_id.slice(0, 8)}...:{citation.chunk_id}
                          </span>
                          <span className={`source-confidence ${confidence.tone}`}>{confidence.label}</span>
                        </div>
                        <span className="source-range">
                          score {citation.score.toFixed(3)} • chars {citation.start}-{citation.end}
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

      {err && (
        <div className="error-banner">
          <strong>Problem:</strong> {err}
        </div>
      )}
    </main>
  );
}
