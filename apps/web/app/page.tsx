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
  answer_mode?: "rag" | "extractive" | "advisor";
  brief?: {
    direct_answer: string;
    key_findings: string[];
    recommended_next_actions: string[];
    confidence?: string | null;
    evidence_note?: string | null;
  };
  citations?: Citation[];
  matches?: RetrievalMatch[];
  error?: string;
  message?: string;
};

type InsightModuleStatus = "ok" | "grounded" | "fallback" | "failed" | "insufficient_evidence";

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
  return text
    .replace(/\[[0-9a-f]{8}-[0-9a-f-]{27}(?:\s*,\s*[0-9a-f]{8}-[0-9a-f-]{27})*\]/gi, "")
    .replace(/\s{2,}/g, " ")
    .trim();
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
        <span key={`tag-${index}-${citationIdx}`} className="citation-chip">
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
  if (status === "insufficient_evidence") {
    return "Limited evidence — this diagnosis is based on limited source evidence and should be validated with supporting data.";
  }
  if (status === "grounded" || status === "fallback") {
    return "Needs validation — this diagnosis is based on limited source evidence and should be validated with supporting data.";
  }
  return "This module could not be fully assembled for this run. Try regenerating or ask a follow-up question.";
}

function statusBadgeLabel(status: InsightModuleStatus): string | null {
  if (status === "ok") return null;
  if (status === "insufficient_evidence") return "Limited evidence";
  if (status === "grounded" || status === "fallback") return "Needs validation";
  if (status === "failed") return "Unavailable";
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

function confidenceLabel(score?: number | null) {
  if (typeof score !== "number") return "Evidence";
  if (score >= 0.75) return "High confidence";
  if (score >= 0.55) return "Medium confidence";
  return "Low confidence";
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
  const [citations, setCitations] = useState<Citation[]>([]);
  const [sourceMatches, setSourceMatches] = useState<RetrievalMatch[]>([]);
  const [briefFromServer, setBriefFromServer] = useState<AskResp["brief"]>();
  const [answerMode, setAnswerMode] = useState<AskResp["answer_mode"] | null>(null);
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
        setChat((c) => [...c, { role: "assistant", text: msg }]);
        recordApiResponseMeta(resp);
        setCitations(resp.citations ?? []);
        setSourceMatches(resp.matches ?? []);
        setBriefFromServer(resp.brief);
        setAnswerMode(resp.answer_mode ?? null);
        return;
      }

      setChat((c) => [...c, { role: "assistant", text: resp.answer ?? "" }]);
      recordApiResponseMeta(resp);
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
    setCitations([]);
    setSourceMatches([]);
    setBriefFromServer(undefined);
    setAnswerMode(null);
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
    setCitations([]);
    setSourceMatches([]);
    setBriefFromServer(undefined);
    setAnswerMode(null);
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
                  <p>{renderInlineMarkdown(selectedInsight.direct_answer, "insight-direct-answer")}</p>
                  {statusHintForInsight(selectedInsight.status) && (
                    <p className="insight-note">{statusHintForInsight(selectedInsight.status)}</p>
                  )}
                </div>
                <div className="hero-visual" aria-hidden>
                  <span />
                  <span />
                  <span />
                </div>
              </article>

              <div className="details-grid">
                <section className="detail-panel findings-panel">
                  <h3>Key Findings</h3>
                  {selectedInsight.key_findings.length === 0 ? (
                    <p className="sources-empty">No findings returned for this module.</p>
                  ) : (
                    <ul>
                      {selectedInsight.key_findings.map((finding, index) => (
                        <li key={index}>{renderInlineMarkdown(finding, `finding-${index}`)}</li>
                      ))}
                    </ul>
                  )}
                </section>

                <section className="detail-panel actions-panel">
                  <h3>Recommended Actions</h3>
                  {selectedInsight.recommended_actions.length === 0 ? (
                    <p className="sources-empty">No actions returned for this module.</p>
                  ) : (
                    <ul>
                      {selectedInsight.recommended_actions.map((action, index) => (
                        <li key={index}>{renderInlineMarkdown(action, `action-${index}`)}</li>
                      ))}
                    </ul>
                  )}
                </section>

                <section className="detail-panel sources-panel">
                  <div className="sources-head">
                    <h3>Top Sources</h3>
                    {indexed && (
                      <button type="button" className="ghost-btn" disabled={!canRegenerateInsights} onClick={() => fetchInsights(indexed.doc_id)}>
                        Regenerate
                      </button>
                    )}
                  </div>
                  {selectedInsight.citations.length === 0 ? (
                    <p className="sources-empty">No source excerpts were available for this insight.</p>
                  ) : (
                    <div className="sources-list">
                      {selectedInsight.citations.map((citation, index) => {
                        const match = selectedMatchesBySource.get(citation.source_id);
                        return (
                          <details key={`${selectedInsight.module_id}-${citation.source_id}`} className="source-row">
                            <summary>
                              <span className="source-number">{index + 1}</span>
                              <div>
                                <p className="source-title">Source excerpt {index + 1}</p>
                                <p className="source-confidence">{confidenceLabel(citation.score)}</p>
                              </div>
                            </summary>
                            {match?.text && <p className="source-preview">{match.text}</p>}
                          </details>
                        );
                      })}
                    </div>
                  )}
                </section>
              </div>
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
                    {renderTextWithCitationChips(insightBrief.directAnswer, citationOrder)}
                  </p>

                  {briefFromServer?.evidence_note && (
                    <p className="evidence-note">{briefFromServer.evidence_note}</p>
                  )}

                  <div className="ask-details-grid">
                    <section>
                      <h4>Source-backed Findings</h4>
                      <ul>
                        {insightBrief.keyFindings.map((finding, index) => (
                          <li key={index}>{renderTextWithCitationChips(finding, citationOrder)}</li>
                        ))}
                      </ul>
                    </section>

                    <section>
                      <h4>Advisor Recommendations</h4>
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

          {citations.length > 0 && (
            <div className="ask-sources">
              <h3>Supporting Sources</h3>
              <div className="sources-list">
                {citations.map((citation, index) => {
                  const match = matchesBySource.get(citation.source_id);
                  return (
                    <details key={citation.source_id} className="source-row">
                      <summary>
                        <span className="source-number">{index + 1}</span>
                        <div>
                          <p className="source-title">Source excerpt {index + 1}</p>
                          <p className="source-confidence">{confidenceLabel(citation.score)}</p>
                        </div>
                      </summary>
                      {match?.text && <p className="source-preview">{match.text}</p>}
                    </details>
                  );
                })}
              </div>
            </div>
          )}
        </section>

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
