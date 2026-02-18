"use client";

import { useMemo, useState } from "react";
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
  error?: string;
  message?: string;
};

function makeRequestId() {
  return `WEB-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export default function Home() {
  // demo tenant is fixed for non-technical demos
  const tenantId = "demo-tenant";

  const [requestId, setRequestId] = useState(makeRequestId());
  const [file, setFile] = useState<File | null>(null);

  const [doc, setDoc] = useState<UploadResp | null>(null);
  const [indexed, setIndexed] = useState<IndexResp | null>(null);

  const [query, setQuery] = useState("What does being dialed in mean?");
  const [chat, setChat] = useState<{ role: "user" | "assistant"; text: string }[]>([]);
  const [citations, setCitations] = useState<Citation[]>([]);
  const [busy, setBusy] = useState<null | "upload" | "ask">(null);
  const [err, setErr] = useState<string | null>(null);

  const headers = useMemo(
    () => ({
      "x-request-id": requestId,
      "x-tenant-id": tenantId,
    }),
    [requestId, tenantId]
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
    } catch (e: any) {
      setErr(e?.message ?? "Upload/index failed");
    } finally {
      setBusy(null);
    }
  }

  async function ask() {
    setErr(null);
    setBusy("ask");

    // Show the user question immediately
    setChat((c) => [...c, { role: "user", text: query }]);

    try {
      const resp = (await postJson("/v1/ask", { query, top_k: 5 }, headers)) as AskResp;

      if (!resp.ok) {
        const msg = resp.message || "Ask failed";
        setChat((c) => [...c, { role: "assistant", text: `⚠️ ${msg}` }]);
        setCitations(resp.citations ?? []);
        return;
      }

      setChat((c) => [...c, { role: "assistant", text: resp.answer ?? "" }]);
      setCitations(resp.citations ?? []);
    } catch (e: any) {
      setErr(e?.message ?? "Ask failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <main style={{ maxWidth: 980, margin: "0 auto", padding: 24 }}>
      <header style={{ padding: "28px 0 18px" }}>
        <h1 style={{ fontSize: 34, letterSpacing: -0.5, margin: 0 }}>Apalym</h1>
        <p style={{ margin: "10px 0 0", color: "var(--muted)", maxWidth: 760 }}>
          Upload a PDF, then ask questions. Answers come back with citations so you can trust what you’re seeing.
        </p>

        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 14 }}>
          <span style={pillStyle}>tenant: {tenantId}</span>
          <span style={pillStyle}>request_id: {requestId}</span>
          <button
            onClick={() => setRequestId(makeRequestId())}
            style={{ ...btnStyle, marginLeft: "auto" }}
          >
            New request id
          </button>
        </div>
      </header>

      <div style={{ display: "grid", gridTemplateColumns: "380px 1fr", gap: 18 }}>
        {/* Left card: Upload */}
        <section style={cardStyle}>
          <h2 style={h2Style}>Upload</h2>
          <p style={mutedStyle}>Choose a PDF. We’ll index it automatically (no settings required).</p>

          <input
            type="file"
            accept="application/pdf"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />

          <button
            onClick={uploadAndIndex}
            disabled={!file || busy !== null}
            style={{
              ...primaryBtnStyle,
              opacity: !file || busy ? 0.6 : 1,
              marginTop: 12,
            }}
          >
            {busy === "upload" ? "Uploading & indexing…" : "Upload PDF"}
          </button>

          <div style={{ marginTop: 14, borderTop: "1px solid var(--border)", paddingTop: 14 }}>
            <div style={metaRowStyle}>
              <span style={metaKeyStyle}>Document</span>
              <span style={metaValStyle}>{doc?.filename ?? "—"}</span>
            </div>
            <div style={metaRowStyle}>
              <span style={metaKeyStyle}>doc_id</span>
              <span style={metaValStyle}>{doc?.doc_id ?? "—"}</span>
            </div>
            <div style={metaRowStyle}>
              <span style={metaKeyStyle}>Indexed</span>
              <span style={metaValStyle}>{indexed ? `${indexed.upserted_count} chunks` : "—"}</span>
            </div>
          </div>

          {err && (
            <div style={errorStyle}>
              <strong>Problem:</strong> {err}
            </div>
          )}
        </section>

        {/* Right card: Ask + chat */}
        <section style={cardStyle}>
          <h2 style={h2Style}>Ask</h2>

          <div style={{ display: "flex", gap: 10, marginTop: 10 }}>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Ask a question about your PDF…"
              style={inputStyle}
            />
            <button
              onClick={ask}
              disabled={busy !== null}
              style={{ ...primaryBtnStyle, width: 120, opacity: busy ? 0.6 : 1 }}
            >
              {busy === "ask" ? "Asking…" : "Ask"}
            </button>
          </div>

          <div style={{ marginTop: 16 }}>
            {chat.length === 0 ? (
              <div style={{ ...mutedStyle, padding: "14px 2px" }}>
                Upload a PDF, then ask a question to see an answer with sources.
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {chat.map((m, i) => (
                  <div
                    key={i}
                    style={{
                      alignSelf: m.role === "user" ? "flex-end" : "flex-start",
                      maxWidth: "92%",
                      padding: "12px 14px",
                      borderRadius: 14,
                      background: m.role === "user" ? "rgba(109,94,252,.12)" : "rgba(15,18,34,.04)",
                      border: "1px solid var(--border)",
                      lineHeight: 1.45,
                      whiteSpace: "pre-wrap",
                    }}
                  >
                    {m.text}
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Citations */}
          {citations.length > 0 && (
            <div style={{ marginTop: 18 }}>
              <h3 style={{ margin: "0 0 10px", fontSize: 14, color: "var(--muted)" }}>Sources</h3>

              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {citations.map((c) => (
                  <details key={c.source_id} style={sourceCardStyle}>
                    <summary
                      style={{
                        cursor: "pointer",
                        display: "flex",
                        gap: 10,
                        alignItems: "center",
                      }}
                    >
                      <span
                        style={{
                          fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                          fontSize: 12,
                        }}
                      >
                        {c.doc_id.slice(0, 8)}…:{c.chunk_id}
                      </span>
                      <span style={{ color: "var(--muted)", fontSize: 12 }}>
                        score {c.score.toFixed(3)} • chars {c.start}-{c.end}
                      </span>
                    </summary>
                    <div style={{ marginTop: 10, color: "var(--muted)", fontSize: 13 }}>
                      (Next enhancement: “View excerpt” by fetching doc text and slicing start/end.)
                    </div>
                  </details>
                ))}
              </div>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}

const cardStyle: React.CSSProperties = {
  background: "var(--card)",
  border: "1px solid var(--border)",
  borderRadius: 18,
  padding: 18,
  boxShadow: "var(--shadow)",
  minWidth: 0,
};

const h2Style: React.CSSProperties = { margin: 0, fontSize: 16, letterSpacing: -0.2 };

const mutedStyle: React.CSSProperties = { margin: "10px 0 0", color: "var(--muted)", fontSize: 13 };

const pillStyle: React.CSSProperties = {
  padding: "7px 10px",
  borderRadius: 999,
  border: "1px solid var(--border)",
  background: "rgba(255,255,255,.7)",
  fontSize: 12,
  color: "var(--muted)",
};

const btnStyle: React.CSSProperties = {
  padding: "9px 12px",
  borderRadius: 12,
  border: "1px solid var(--border)",
  background: "rgba(255,255,255,.9)",
  cursor: "pointer",
  fontSize: 13,
};

const primaryBtnStyle: React.CSSProperties = {
  padding: "10px 12px",
  borderRadius: 12,
  border: "1px solid rgba(109,94,252,.35)",
  background: "linear-gradient(180deg, rgba(109,94,252,.95), rgba(109,94,252,.82))",
  color: "white",
  cursor: "pointer",
  fontWeight: 600,
};

const inputStyle: React.CSSProperties = {
  flex: 1,
  padding: "12px 14px",
  borderRadius: 14,
  border: "1px solid var(--border)",
  outline: "none",
  minWidth: 0,
  fontSize: 16,
  fontWeight: 600,
  background: "rgba(255,255,255,.95)",
  boxShadow: "0 10px 30px rgba(17, 12, 46, 0.06)",
};


const metaRowStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  gap: 12,
  padding: "6px 0",
};

const metaKeyStyle: React.CSSProperties = { color: "var(--muted)", fontSize: 12 };

const metaValStyle: React.CSSProperties = {
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
  fontSize: 12,
  color: "var(--text)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  maxWidth: 240,
};

const errorStyle: React.CSSProperties = {
  marginTop: 14,
  padding: 12,
  borderRadius: 14,
  border: "1px solid rgba(255, 90, 90, .25)",
  background: "rgba(255, 90, 90, .07)",
  color: "#7b1b1b",
  fontSize: 13,
};

const sourceCardStyle: React.CSSProperties = {
  border: "1px solid var(--border)",
  borderRadius: 14,
  padding: "10px 12px",
  background: "rgba(15,18,34,.02)",
};

