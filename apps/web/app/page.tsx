"use client";

import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getJson, postForm, postJson } from "./lib/api";
import {
  clearClientSession,
  persistClientSession,
  readStoredAccessKey,
  readStoredDisplayLabel,
} from "./lib/authSession";

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

type CitedText = {
  text: string;
  citation_ids: number[];
};

type SourceEvidence = {
  citation_id: number;
  title: string;
  page?: number | null;
  excerpt: string;
  document_name: string;
};

type EvidenceLimitations = {
  message: string | null;
  validation_questions: string[];
  has_limitations?: boolean;
};

type AskResp = {
  ok: boolean;
  tenant_id: string;
  request_id: string;
  query: string;
  answer?: CitedText | string;
  answer_mode?: "rag" | "extractive" | "advisor";
  source_backed_findings?: CitedText[];
  advisor_recommendations?: CitedText[];
  sources?: SourceEvidence[];
  brief?: {
    direct_answer: string;
    key_findings: string[];
    recommended_next_actions: string[];
    confidence?: string | null;
    evidence_note?: string | null;
  };
  confidence?: string | null;
  evidence_note?: string | null;
  error?: string;
  message?: string;
};

type InsightModuleStatus = "ready" | "limited_evidence" | "ok" | "grounded" | "fallback" | "failed" | "insufficient_evidence" | "error";

type MetricValue = {
  label: string;
  actual: number;
  target: number;
  unit: string;
  direction: "lower_is_better" | "higher_is_better";
};

type InsightModule = {
  module_id: string;
  title: string;
  status: InsightModuleStatus;
  confidence?: "high" | "medium" | "low";
  summary: CitedText | string;
  direct_answer: CitedText | string;
  key_findings: (CitedText | string)[];
  recommended_actions: (CitedText | string)[];
  sources: SourceEvidence[];
  evidence_limitations?: EvidenceLimitations;
  validation_questions?: string[];
  metrics?: MetricValue[];
  module_key?: string;
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

type InsightTab = {
  moduleId: string;
  navLabel: string;
  sidebarLabel: string;
  icon: string;
  tone: "losses" | "costs" | "risks" | "margins" | "trends";
};

const INSIGHT_TABS: InsightTab[] = [
  { moduleId: "loss-leakage", navLabel: "Losses", sidebarLabel: "Losses", icon: "📉", tone: "losses" },
  { moduleId: "top-cost-drivers", navLabel: "Costs", sidebarLabel: "Costs", icon: "📊", tone: "costs" },
  { moduleId: "key-risks", navLabel: "Risks", sidebarLabel: "Risks", icon: "⚠️", tone: "risks" },
  { moduleId: "margin-opportunities", navLabel: "Margins", sidebarLabel: "Margins", icon: "📈", tone: "margins" },
  { moduleId: "trends-anomalies", navLabel: "Trends", sidebarLabel: "Trends", icon: "🪄", tone: "trends" },
];

function makeRequestId() {
  return `WEB-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function asErrorMessage(error: unknown, fallback: string) {
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

function moduleValidationQuestions(insight: InsightModule): string[] {
  if (insight.validation_questions && insight.validation_questions.length > 0) {
    return insight.validation_questions;
  }
  return insight.evidence_limitations?.validation_questions ?? [];
}

function citedTextStr(val: CitedText | string | undefined): string {
  if (!val) return "";
  if (typeof val === "string") return val;
  return val.text || "";
}

function parseAskAnswer(value: unknown): CitedText {
  if (value && typeof value === "object" && "text" in value) {
    const cited = value as CitedText;
    return {
      text: typeof cited.text === "string" ? cited.text : "",
      citation_ids: Array.isArray(cited.citation_ids) ? cited.citation_ids : [],
    };
  }
  if (typeof value !== "string") {
    return { text: "", citation_ids: [] };
  }
  const trimmed = value.trim();
  if (trimmed.startsWith("{")) {
    try {
      const parsed = JSON.parse(trimmed) as {
        direct_answer?: { text?: string; citation_ids?: number[] };
        text?: string;
        citation_ids?: number[];
      };
      if (parsed.direct_answer && typeof parsed.direct_answer.text === "string") {
        return {
          text: parsed.direct_answer.text,
          citation_ids: parsed.direct_answer.citation_ids ?? [],
        };
      }
      if (typeof parsed.text === "string") {
        return {
          text: parsed.text,
          citation_ids: parsed.citation_ids ?? [],
        };
      }
    } catch {
      // Fall through to plain-text handling.
    }
  }
  return { text: value, citation_ids: [] };
}

function citedTextIds(val: CitedText | string | undefined): number[] {
  if (!val || typeof val === "string") return [];
  return val.citation_ids || [];
}

function normalizeModuleStatus(status: InsightModuleStatus): InsightModuleStatus {
  if (status === "ok") return "ready";
  if (status === "grounded" || status === "fallback" || status === "insufficient_evidence") {
    return "limited_evidence";
  }
  return status;
}

function confidenceLabel(confidence?: string | null): string {
  if (!confidence) return "Medium";
  const normalized = confidence.toLowerCase();
  if (normalized === "high") return "High";
  if (normalized === "low") return "Low";
  return "Medium";
}

function renderMetricsChart(metrics: MetricValue[]) {
  if (metrics.length === 0) return null;
  return (
    <div className="metrics-chart">
      {metrics.map((metric) => {
        const maxValue = Math.max(metric.actual, metric.target, 1);
        const actualPct = Math.min(100, (metric.actual / maxValue) * 100);
        const targetPct = Math.min(100, (metric.target / maxValue) * 100);
        return (
          <div key={metric.label} className="metric-row">
            <div className="metric-head">
              <span className="metric-label">{metric.label}</span>
              <span className="metric-values">
                {metric.actual}
                {metric.unit} vs {metric.target}
                {metric.unit}
              </span>
            </div>
            <div className="metric-bars" aria-hidden>
              <div className="metric-bar actual" style={{ width: `${actualPct}%` }} />
              <div className="metric-bar target" style={{ width: `${targetPct}%` }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function renderEvidenceQualityCard(
  insight: InsightModule,
  sources: SourceEvidence[],
  onViewEvidence: () => void
) {
  const limited = normalizeModuleStatus(insight.status) === "limited_evidence";
  const metrics = insight.metrics ?? [];

  return (
    <aside className="evidence-quality-card" aria-label="Evidence quality">
      <p className="evidence-quality-title">Evidence quality</p>
      {limited ? (
        <>
          <p className="evidence-quality-status">Limited category detail</p>
          <p className="evidence-quality-note">Needs validation before decision</p>
        </>
      ) : (
        <p className="evidence-quality-status">Source-backed diagnosis</p>
      )}
      <dl className="evidence-quality-meta">
        <div>
          <dt>Sources</dt>
          <dd>{sources.length}</dd>
        </div>
        <div>
          <dt>Confidence</dt>
          <dd>{confidenceLabel(insight.confidence)}</dd>
        </div>
      </dl>
      {sources.length > 0 && (
        <button type="button" className="ghost-btn evidence-quality-btn" onClick={onViewEvidence}>
          View supporting evidence
        </button>
      )}
      {renderMetricsChart(metrics)}
    </aside>
  );
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

function renderCitationBadges(
  citationIds: number[],
  sources: SourceEvidence[],
  onSelect: (source: SourceEvidence) => void
): ReactNode {
  if (citationIds.length === 0) return null;
  return (
    <span className="citation-pills">
      {citationIds.map((id) => {
        const source = sources.find((item) => item.citation_id === id);
        return (
          <button
            key={id}
            type="button"
            className="citation-chip citation-chip-btn"
            title={source ? `View source ${id}` : `Source ${id}`}
            onClick={() => source && onSelect(source)}
          >
            {id}
          </button>
        );
      })}
    </span>
  );
}

function renderCitedContent(
  value: CitedText | string | undefined,
  sources: SourceEvidence[],
  onSelect: (source: SourceEvidence) => void
): ReactNode {
  const text = citedTextStr(value);
  const ids = citedTextIds(value);
  return (
    <>
      {renderInlineMarkdown(text, "cited")}
      {renderCitationBadges(ids, sources, onSelect)}
    </>
  );
}

function statusHintForInsight(status: InsightModuleStatus, limitations?: EvidenceLimitations): string {
  const normalized = normalizeModuleStatus(status);
  if (normalized === "ready") return "";
  if (limitations?.message) return limitations.message;
  if (normalized === "limited_evidence") {
    return "This diagnosis is based on limited source evidence and should be validated with supporting data.";
  }
  return "This module could not be fully assembled for this run. Try regenerating or ask a follow-up question.";
}

function statusBadgeLabel(status: InsightModuleStatus): string | null {
  const normalized = normalizeModuleStatus(status);
  if (normalized === "ready") return null;
  if (normalized === "limited_evidence") return "Needs validation";
  if (normalized === "failed" || normalized === "error") return "Unavailable";
  return null;
}

function deriveFileType(filename?: string) {
  const ext = filename?.split(".").pop()?.toUpperCase();
  if (!ext) return "DOC";
  if (["PDF", "CSV", "XLSX"].includes(ext)) return ext;
  return ext.slice(0, 4);
}

function deriveGreeting() {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

export default function Home() {
  const [requestId] = useState(makeRequestId());
  const [authRequired, setAuthRequired] = useState(false);
  const [authReady, setAuthReady] = useState(false);
  const [accessKey, setAccessKey] = useState<string | null>(null);
  const [displayLabel, setDisplayLabel] = useState("Demo workspace");
  const [loginField, setLoginField] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  const [debugOpen, setDebugOpen] = useState(false);
  const [docsOpen, setDocsOpen] = useState(false);
  const [lastRequestId, setLastRequestId] = useState<string | null>(null);
  const [lastTenantId, setLastTenantId] = useState<string | null>(null);

  const [file, setFile] = useState<File | null>(null);
  const [doc, setDoc] = useState<UploadResp | null>(null);
  const [indexed, setIndexed] = useState<IndexResp | null>(null);
  const [insights, setInsights] = useState<InsightModule[]>([]);
  const [activeInsightId, setActiveInsightId] = useState<string>(INSIGHT_TABS[0].moduleId);

  const [query, setQuery] = useState("");
  const [chat, setChat] = useState<ChatMessage[]>([]);
  const [askSources, setAskSources] = useState<SourceEvidence[]>([]);
  const [askFindings, setAskFindings] = useState<CitedText[]>([]);
  const [askRecommendations, setAskRecommendations] = useState<CitedText[]>([]);
  const [askAnswer, setAskAnswer] = useState<CitedText | null>(null);
  const [answerMode, setAnswerMode] = useState<AskResp["answer_mode"] | null>(null);
  const [evidenceNote, setEvidenceNote] = useState<string | null>(null);
  const [selectedEvidence, setSelectedEvidence] = useState<SourceEvidence | null>(null);
  const [busy, setBusy] = useState<null | "upload" | "insights" | "ask">(null);
  const [err, setErr] = useState<string | null>(null);

  const uploadSectionRef = useRef<HTMLElement | null>(null);
  const briefSectionRef = useRef<HTMLElement | null>(null);
  const askSectionRef = useRef<HTMLElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const askTextareaRef = useRef<HTMLTextAreaElement | null>(null);

  const resizeAskTextarea = useCallback(() => {
    const el = askTextareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, []);

  function recordApiResponseMeta(payload: unknown) {
    if (!payload || typeof payload !== "object") return;
    const obj = payload as { request_id?: string; tenant_id?: string };
    if (obj.request_id) setLastRequestId(obj.request_id);
    if (obj.tenant_id) setLastTenantId(obj.tenant_id);
  }

  useEffect(() => {
    let cancelled = false;

    async function initAuth() {
      try {
        const cfg = (await getJson("/v1/auth/config")) as { client_test_auth_enabled?: boolean };
        if (cancelled) return;
        const required = Boolean(cfg.client_test_auth_enabled);
        setAuthRequired(required);
        if (!required) {
          setDisplayLabel("Demo workspace");
          setAuthReady(true);
          return;
        }

        const stored = readStoredAccessKey();
        if (!stored) {
          setAuthReady(true);
          return;
        }

        try {
          const sess = (await getJson("/v1/auth/session", {
            Authorization: `Bearer ${stored}`,
            "x-request-id": requestId,
          })) as { display_label?: string };
          if (cancelled) return;
          setAccessKey(stored);
          setDisplayLabel(sess.display_label ?? readStoredDisplayLabel() ?? "Workspace");
        } catch {
          clearClientSession();
          setAccessKey(null);
        } finally {
          if (!cancelled) setAuthReady(true);
        }
      } catch {
        if (cancelled) return;
        setAuthRequired(false);
        setDisplayLabel("Demo workspace");
        setAuthReady(true);
      }
    }

    void initAuth();
    return () => {
      cancelled = true;
    };
  }, [requestId]);

  const headers = useMemo(() => {
    const h: Record<string, string> = {
      "x-request-id": requestId,
    };
    if (accessKey) {
      h.Authorization = `Bearer ${accessKey}`;
    }
    return h;
  }, [requestId, accessKey]);

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
  const selectedTab = INSIGHT_TABS.find((tab) => tab.moduleId === selectedInsightId) ?? INSIGHT_TABS[0];

  const selectedSources = selectedInsight?.sources ?? [];

  const openEvidence = useCallback((source: SourceEvidence) => {
    setSelectedEvidence(source);
  }, []);

  async function fetchInsights(docId: string, clearOldInsights = false) {
    if (clearOldInsights) setInsights([]);
    setBusy("insights");
    try {
      const resp = (await postJson("/v1/insights", { doc_id: docId, top_k: 3 }, headers)) as InsightsResp;
      recordApiResponseMeta(resp);
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
      recordApiResponseMeta(up);
      setDoc(up);

      const CHUNK_SIZE = 700;
      const OVERLAP = 80;
      const idx = (await postJson(
        `/v1/docs/${up.doc_id}/index?chunk_size=${CHUNK_SIZE}&overlap=${OVERLAP}`,
        {},
        headers
      )) as IndexResp;
      recordApiResponseMeta(idx);
      setIndexed(idx);
      setActiveInsightId(INSIGHT_TABS[0].moduleId);
      setInsights([]);
      setQuery("");
      setChat([]);
      setAskSources([]);
      setAskFindings([]);
      setAskRecommendations([]);
      setAskAnswer(null);
      setAnswerMode(null);
      setEvidenceNote(null);
      setSelectedEvidence(null);

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
    setAskSources([]);
    setAskFindings([]);
    setAskRecommendations([]);
    setAskAnswer(null);
    setEvidenceNote(null);
    setChat((c) => [...c, { role: "user", text: trimmedQuery }]);

    try {
      const resp = (await postJson(
        "/v1/ask",
        { query: trimmedQuery, top_k: 3, doc_id: activeDocId },
        headers
      )) as AskResp;

      if (!resp.ok) {
        const msg = resp.message || "Ask failed";
        setChat((c) => [...c, { role: "assistant", text: msg }]);
        recordApiResponseMeta(resp);
        setAskSources(resp.sources ?? []);
        setAskFindings(resp.source_backed_findings ?? []);
        setAskRecommendations(resp.advisor_recommendations ?? []);
        setAskAnswer(parseAskAnswer(resp.answer ?? msg));
        setEvidenceNote(resp.evidence_note ?? resp.brief?.evidence_note ?? null);
        setAnswerMode(resp.answer_mode ?? null);
        return;
      }

      const answerObj = parseAskAnswer(resp.answer);

      setChat((c) => [...c, { role: "assistant", text: answerObj.text }]);
      recordApiResponseMeta(resp);
      setAskSources(resp.sources ?? []);
      setAskFindings(resp.source_backed_findings ?? []);
      setAskRecommendations(resp.advisor_recommendations ?? []);
      setAskAnswer(answerObj);
      setEvidenceNote(resp.evidence_note ?? resp.brief?.evidence_note ?? null);
      setAnswerMode(resp.answer_mode ?? "rag");
    } catch (e: unknown) {
      setErr(asErrorMessage(e, "Ask failed"));
      setAskSources([]);
      setAskFindings([]);
      setAskRecommendations([]);
      setAskAnswer(null);
      setEvidenceNote(null);
    } finally {
      setBusy(null);
    }
  }

  async function signIn() {
    const trimmed = loginField.trim();
    if (!trimmed) {
      setErr("Enter the access key you were given for this test.");
      return;
    }
    setErr(null);
    setAuthBusy(true);
    try {
      const resp = (await postJson(
        "/v1/auth/login",
        { access_key: trimmed },
        { "x-request-id": requestId }
      )) as { display_label: string; tenant_id: string };
      persistClientSession(trimmed, resp.display_label);
      setAccessKey(trimmed);
      setDisplayLabel(resp.display_label);
      setLoginField("");
    } catch (e: unknown) {
      setErr(asErrorMessage(e, "Sign-in failed"));
    } finally {
      setAuthBusy(false);
    }
  }

  function signOut() {
    clearClientSession();
    setAccessKey(null);
    setDisplayLabel("Demo workspace");
    setDebugOpen(false);
    setFile(null);
    setDoc(null);
    setIndexed(null);
    setInsights([]);
    setQuery("");
    setChat([]);
    setAskSources([]);
    setAskFindings([]);
    setAskRecommendations([]);
    setAskAnswer(null);
    setAnswerMode(null);
    setEvidenceNote(null);
    setSelectedEvidence(null);
    setLastRequestId(null);
    setLastTenantId(null);
  }

  function resetForNewUpload() {
    setFile(null);
    setDoc(null);
    setIndexed(null);
    setInsights([]);
    setActiveInsightId(INSIGHT_TABS[0].moduleId);
    setQuery("");
    setChat([]);
    setAskSources([]);
    setAskFindings([]);
    setAskRecommendations([]);
    setAskAnswer(null);
    setAnswerMode(null);
    setEvidenceNote(null);
    setSelectedEvidence(null);
    setErr(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  function openFilePicker() {
    if (fileInputRef.current) fileInputRef.current.value = "";
    fileInputRef.current?.click();
  }

  function jumpToSection(target: "brief" | "ask" | "upload") {
    if (target === "brief") {
      briefSectionRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    if (target === "ask") {
      askSectionRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    uploadSectionRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function activateInsight(moduleId: string) {
    setActiveInsightId(moduleId);
    jumpToSection("brief");
  }

  const greeting = deriveGreeting();
  const fileTypeLabel = deriveFileType(doc?.filename ?? file?.name);
  const uploadStatusLabel =
    busy === "upload"
      ? "Indexing"
      : busy === "insights"
        ? "Generating insights"
        : indexed
          ? "Ready"
          : file
            ? "Selected"
            : "No document";
  const headerSubtitle = doc?.filename
    ? `Here’s your executive brief for ${doc.filename}`
    : "Upload a business document to generate your executive diagnosis.";

  if (!authReady) {
    return (
      <main className="login-gate-screen">
        <p className="login-gate-status">Preparing your workspace…</p>
      </main>
    );
  }

  if (authRequired && !accessKey) {
    return (
      <main className="login-gate-screen">
        <section className="glass-card login-gate-card">
          <div className="login-gate-brand">
            <div className="brand-logo" aria-hidden>
              ◈
            </div>
            <div>
              <p className="brand-title">Apalym</p>
              <p className="brand-subtitle">Client testing sign-in</p>
            </div>
          </div>
          <p className="login-gate-lead">
            Use the access key you were given. Your uploads and answers stay scoped to the workspace tied to that key.
          </p>
          <label className="login-gate-label">
            Access key
            <input
              className="login-gate-input"
              type="password"
              autoComplete="off"
              value={loginField}
              onChange={(e) => setLoginField(e.target.value)}
              placeholder="Paste your access key"
            />
          </label>
          <button type="button" className="primary-btn login-gate-btn" onClick={() => void signIn()} disabled={authBusy}>
            {authBusy ? "Signing in…" : "Sign in"}
          </button>
          {err && (
            <div className="error-banner login-gate-error">
              <strong>Problem:</strong> {err}
            </div>
          )}
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand-block">
          <div className="brand-logo" aria-hidden>
            ◈
          </div>
          <div>
            <p className="brand-title">Apalym</p>
            <p className="brand-subtitle">Executive AI</p>
          </div>
        </div>

        <nav className="sidebar-nav">
          <button className="nav-item active" type="button" onClick={() => jumpToSection("brief")}>
            <span className="nav-icon">✦</span>
            Executive Brief
          </button>

          {INSIGHT_TABS.map((tab) => (
            <button
              key={tab.moduleId}
              type="button"
              className={`nav-item tone-${tab.tone} ${selectedInsightId === tab.moduleId ? "active-soft" : ""}`}
              onClick={() => activateInsight(tab.moduleId)}
            >
              <span className="nav-icon">{tab.icon}</span>
              {tab.sidebarLabel}
            </button>
          ))}

          <button className="nav-item" type="button" onClick={() => jumpToSection("ask")}>
            <span className="nav-icon">💬</span>
            Ask Follow-up
          </button>
          <button className={`nav-item ${docsOpen ? "active-soft" : ""}`} type="button" onClick={() => setDocsOpen((o) => !o)}>
            <span className="nav-icon">📄</span>
            Documents
          </button>
          <button
            className={`nav-item ${debugOpen ? "active-soft" : ""}`}
            type="button"
            onClick={() => setDebugOpen((open) => !open)}
          >
            <span className="nav-icon">⚙️</span>
            Diagnostics
          </button>
        </nav>
        {accessKey && (
          <div className="sidebar-footer">
            <button className="nav-item sign-out-btn" type="button" onClick={signOut}>
              <span className="nav-icon">⎋</span>
              Sign out
            </button>
          </div>
        )}
      </aside>

      <section className="main-pane">
        <header className="top-header">
          <div>
            <h1>
              {greeting}, {displayLabel} <span aria-hidden>👋</span>
            </h1>
            <p>{headerSubtitle}</p>
          </div>
          <button type="button" className="header-upload-btn" onClick={() => { resetForNewUpload(); openFilePicker(); }}>
            ⬆ Upload New Document
          </button>
        </header>

        <section className="glass-card doc-card" ref={uploadSectionRef}>
          <div className="doc-card-left">
            <div className="file-badge">{fileTypeLabel}</div>
            <div>
              <p className="doc-name">{doc?.filename ?? file?.name ?? "No document selected"}</p>
              <p className="doc-meta">
                {indexed ? "Indexed and diagnosis-ready" : file ? "Ready to index" : "Upload PDF, CSV, or XLSX"}
              </p>
            </div>
          </div>
          <div className="doc-card-actions">
            <span className={`status-pill ${indexed ? "ready" : busy === "upload" ? "working" : "idle"}`}>
              {uploadStatusLabel}
            </span>
            <button type="button" className="ghost-btn" onClick={openFilePicker}>
              Select Document
            </button>
            <button type="button" className="primary-btn" onClick={uploadAndIndex} disabled={!canUpload}>
              {busy === "upload" ? "Uploading..." : "Generate Brief"}
            </button>
          </div>

          <input
            ref={fileInputRef}
            className="hidden-file-input"
            type="file"
            accept="application/pdf,text/csv,.csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,.xlsx"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </section>

        <section className="glass-card brief-card" ref={briefSectionRef}>
          <div className="brief-nav-shell" role="tablist" aria-label="Insight modules">
            {INSIGHT_TABS.map((tab) => (
              <button
                key={tab.moduleId}
                type="button"
                className={`brief-tab tone-${tab.tone} ${selectedInsightId === tab.moduleId ? "active" : ""}`}
                onClick={() => setActiveInsightId(tab.moduleId)}
              >
                <span>{tab.icon}</span>
                {tab.navLabel}
              </button>
            ))}
          </div>

          {!indexed && <div className="empty-panel">Upload and index a document to unlock the executive brief.</div>}

          {busy === "insights" && (
            <div className="empty-panel">
              Building your diagnosis modules with document-grounded evidence and citations.
            </div>
          )}

          {indexed && !busy && !selectedInsight && (
            <div className="empty-panel">No insights were returned yet. Try regenerating the diagnosis.</div>
          )}

          {selectedInsight && (
            <>
              <article className={`insight-hero tone-${selectedTab.tone}`}>
                <div className="insight-hero-main">
                  <div className="hero-tag-row">
                    <p className="hero-tag">Primary Insight</p>
                    {statusBadgeLabel(selectedInsight.status) && (
                      <span className="limited-evidence-badge">{statusBadgeLabel(selectedInsight.status)}</span>
                    )}
                  </div>
                  <h2>{selectedInsight.title}</h2>
                  <p>
                    {renderCitedContent(
                      selectedInsight.direct_answer || selectedInsight.summary,
                      selectedSources,
                      openEvidence
                    )}
                  </p>
                  {statusHintForInsight(selectedInsight.status, selectedInsight.evidence_limitations) && (
                    <p className="insight-note">
                      {statusHintForInsight(selectedInsight.status, selectedInsight.evidence_limitations)}
                    </p>
                  )}
                  {normalizeModuleStatus(selectedInsight.status) === "limited_evidence" &&
                    moduleValidationQuestions(selectedInsight).length > 0 && (
                      <div className="validation-questions">
                        <h4>What to validate next</h4>
                        <ul>
                          {moduleValidationQuestions(selectedInsight).map((question, index) => (
                            <li key={index}>{question}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                </div>
                {renderEvidenceQualityCard(selectedInsight, selectedSources, () => {
                  if (selectedSources[0]) openEvidence(selectedSources[0]);
                })}
              </article>

              <div className="details-grid">
                <section className="detail-panel findings-panel">
                  <h3>Key Findings</h3>
                  <ul>
                    {selectedInsight.key_findings.map((finding, index) => (
                      <li key={index}>{renderCitedContent(finding, selectedSources, openEvidence)}</li>
                    ))}
                  </ul>
                </section>

                <section className="detail-panel actions-panel">
                  <h3>Recommended Actions</h3>
                  <ul>
                    {selectedInsight.recommended_actions.map((action, index) => (
                      <li key={index}>{renderCitedContent(action, selectedSources, openEvidence)}</li>
                    ))}
                  </ul>
                </section>

                <section className="detail-panel sources-panel">
                  <div className="sources-head">
                    <h3>Top Sources</h3>
                    <p className="sources-subtitle">View the source evidence used for this diagnosis.</p>
                    {indexed && (
                      <button type="button" className="ghost-btn" disabled={!canRegenerateInsights} onClick={() => fetchInsights(indexed.doc_id)}>
                        Regenerate
                      </button>
                    )}
                  </div>
                  {selectedSources.length === 0 ? (
                    <p className="sources-empty">No source excerpts were available for this insight.</p>
                  ) : (
                    <div className="sources-list">
                      {selectedSources.map((source) => (
                        <details key={`${selectedInsight.module_id}-src-${source.citation_id}`} className="source-row">
                          <summary>
                            <span className="source-number">{source.citation_id}</span>
                            <div>
                              <p className="source-title">Source {source.citation_id}</p>
                              <p className="source-confidence">{source.document_name}</p>
                            </div>
                          </summary>
                          {source.excerpt && (
                            <div className="source-evidence-drawer">
                              <p className="evidence-label">Source evidence — not AI-generated analysis</p>
                              {source.page && <p className="source-page">Page {source.page}</p>}
                              <p className="source-preview">{source.excerpt}</p>
                            </div>
                          )}
                        </details>
                      ))}
                    </div>
                  )}
                </section>
              </div>

              {normalizeModuleStatus(selectedInsight.status) === "limited_evidence" && selectedSources.length > 0 && (
                <div className="view-sources-row">
                  <button type="button" className="ghost-btn" onClick={() => openEvidence(selectedSources[0])}>
                    View source evidence
                  </button>
                </div>
              )}
            </>
          )}
        </section>

        <section className="glass-card ask-card" ref={askSectionRef}>
          <div className="ask-head">
            <h2>Follow-up Q&A</h2>
            <p>Ask Apalym to explain, prioritize, or turn recommendations into next steps.</p>
          </div>

          <div className="ask-row">
            <textarea
              ref={askTextareaRef}
              value={query}
              rows={1}
              onChange={(e) => { setQuery(e.target.value); resizeAskTextarea(); }}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); } }}
              placeholder="Ask a follow-up question..."
              className="ask-input ask-textarea"
            />
            <button onClick={ask} disabled={!canAsk} className="primary-btn ask-btn">
              {busy === "ask" ? "Asking..." : "Ask"}
            </button>
          </div>

          {!indexed && <p className="helper-note">Upload and index a document to enable follow-up Q&A.</p>}

          {latestQuestion === "" ? (
            <div className="empty-panel ask-empty">Your follow-up responses appear here.</div>
          ) : (
            <div className="ask-response">
              <div className="ask-question-block">
                <span>Question</span>
                <p>{latestQuestion}</p>
              </div>

              {latestAssistantMessage && (
                <article className="ask-answer-block">
                  <div className="ask-answer-head">
                    <h3>Answer</h3>
                    {answerMode && (
                      <span className={`answer-mode ${answerMode}`}>
                        {answerMode === "advisor" ? "Document-informed advisor" : answerMode === "rag" ? "AI-grounded" : "Source-grounded"}
                      </span>
                    )}
                  </div>
                  <p className="ask-direct-answer">
                    {askAnswer
                      ? renderCitedContent(askAnswer, askSources, openEvidence)
                      : renderInlineMarkdown(latestAssistantMessage, "ask-answer")}
                  </p>

                  {evidenceNote && <p className="evidence-note">{evidenceNote}</p>}

                  <div className="ask-details-grid">
                    <section>
                      <h4>Source-backed Findings</h4>
                      {askFindings.length > 0 ? (
                        <ul>
                          {askFindings.map((finding, index) => (
                            <li key={index}>{renderCitedContent(finding, askSources, openEvidence)}</li>
                          ))}
                        </ul>
                      ) : (
                        <p className="helper-note">No source-backed findings were returned.</p>
                      )}
                    </section>

                    <section>
                      <h4>Advisor Recommendations</h4>
                      {askRecommendations.length > 0 ? (
                        <ul>
                          {askRecommendations.map((action, index) => (
                            <li key={index}>{renderCitedContent(action, askSources, openEvidence)}</li>
                          ))}
                        </ul>
                      ) : (
                        <p className="helper-note">No advisor recommendations were returned.</p>
                      )}
                    </section>
                  </div>
                </article>
              )}
            </div>
          )}

          {askSources.length > 0 && (
            <div className="ask-sources">
              <h3>Supporting Sources</h3>
              <div className="sources-list">
                {askSources.map((source) => (
                  <details key={`ask-src-${source.citation_id}`} className="source-row">
                    <summary>
                      <span className="source-number">{source.citation_id}</span>
                      <div>
                        <p className="source-title">Source {source.citation_id}</p>
                        <p className="source-confidence">{source.document_name}</p>
                      </div>
                    </summary>
                    {source.excerpt && (
                      <div className="source-evidence-drawer">
                        {source.page && <p className="source-page">Page {source.page}</p>}
                        <p className="source-preview">{source.excerpt}</p>
                      </div>
                    )}
                  </details>
                ))}
              </div>
            </div>
          )}
        </section>

        {selectedEvidence && (
          <div className="evidence-modal-backdrop" onClick={() => setSelectedEvidence(null)}>
            <div className="evidence-modal" onClick={(event) => event.stopPropagation()}>
              <div className="evidence-modal-head">
                <h3>Source {selectedEvidence.citation_id}</h3>
                <button type="button" className="ghost-btn" onClick={() => setSelectedEvidence(null)}>
                  Close
                </button>
              </div>
              <p className="source-confidence">{selectedEvidence.document_name}</p>
              {selectedEvidence.page && <p className="source-page">Page {selectedEvidence.page}</p>}
              <p className="source-preview">{selectedEvidence.excerpt}</p>
            </div>
          </div>
        )}

        {docsOpen && (
          <section className="glass-card docs-panel" aria-label="Documents">
            <h3>Current Document</h3>
            {!doc ? (
              <p className="docs-empty">No document uploaded yet. Upload a PDF, CSV, or XLSX to get started.</p>
            ) : (
              <div className="docs-info">
                <dl className="debug-dl">
                  <div><dt>Filename</dt><dd>{doc.filename}</dd></div>
                  <div><dt>Type</dt><dd>{deriveFileType(doc.filename)}</dd></div>
                  <div><dt>Document ID</dt><dd>{doc.doc_id.slice(0, 12)}…</dd></div>
                  <div><dt>Indexed chunks</dt><dd>{indexed?.num_chunks ?? "—"}</dd></div>
                  <div><dt>Status</dt><dd>{indexed ? "Indexed & brief-ready" : "Uploaded"}</dd></div>
                </dl>
                <div className="docs-actions">
                  <button type="button" className="ghost-btn" onClick={() => { resetForNewUpload(); openFilePicker(); setDocsOpen(false); }}>
                    Replace Document
                  </button>
                  {indexed && (
                    <button type="button" className="primary-btn" disabled={busy !== null} onClick={() => { fetchInsights(indexed.doc_id); setDocsOpen(false); }}>
                      Regenerate Brief
                    </button>
                  )}
                </div>
              </div>
            )}
            <p className="docs-note">Full document library coming in a future release.</p>
          </section>
        )}

        {debugOpen && (
          <section className="glass-card debug-panel" aria-label="Diagnostics">
            <h3>Diagnostics</h3>
            <p className="debug-panel-note">For support only. Do not share access keys from this screen.</p>
            <dl className="debug-dl">
              <div>
                <dt>Last request id</dt>
                <dd>{lastRequestId ?? "—"}</dd>
              </div>
              <div>
                <dt>Workspace tenant id</dt>
                <dd>{lastTenantId ?? "—"}</dd>
              </div>
            </dl>
          </section>
        )}

        {err && (
          <div className="error-banner">
            <strong>Problem:</strong> {err}
          </div>
        )}
      </section>
    </main>
  );
}
