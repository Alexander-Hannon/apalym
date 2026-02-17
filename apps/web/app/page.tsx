"use client";

import { useState } from "react";
import { postJson } from "./lib/api";

type DemoResponse = {
  ok: boolean;
  service: string;
  sent_to_ai: { message: string };
  ai_response: any;
};

export default function Home() {
  const [message, setMessage] = useState("Hello from web → api → ai");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<DemoResponse | null>(null);

  async function runDemo() {
    setLoading(true);
    setError(null);
    setData(null);

    try {
      const json = await postJson<DemoResponse>("/v1/demo", { message });
      setData(json);
    } catch (e: any) {
      setError(e?.message ?? "Unknown error");
    } finally {
      setLoading(false);
    }
  } // ✅ closes runDemo

  return (
    <main
      style={{
        maxWidth: 800,
        margin: "40px auto",
        padding: 16,
        fontFamily: "system-ui, sans-serif",
      }}
    >
      <h1 style={{ fontSize: 28, marginBottom: 8 }}>Apalym Demo</h1>
      <p style={{ marginTop: 0, color: "#555" }}>
        Click the button to run a web → api → ai call and display the response.
      </p>

      <div style={{ display: "flex", gap: 12, marginTop: 20 }}>
        <input
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          style={{ flex: 1, padding: 10, borderRadius: 8, border: "1px solid #ccc" }}
          placeholder="Type a message…"
        />
        <button
          onClick={runDemo}
          disabled={loading}
          style={{
            padding: "10px 14px",
            borderRadius: 8,
            border: "1px solid #111",
            background: loading ? "#ddd" : "#111",
            color: "#fff",
            cursor: loading ? "not-allowed" : "pointer",
          }}
        >
          {loading ? "Running..." : "Run Demo"}
        </button>
      </div>

      {error && (
        <div
          style={{
            marginTop: 16,
            padding: 12,
            borderRadius: 8,
            background: "#ffe6e6",
            border: "1px solid #ffb3b3",
          }}
        >
          <strong>Error:</strong> {error}
        </div>
      )}

      {data && (
        <div style={{ marginTop: 16 }}>
          <h2 style={{ fontSize: 18 }}>Response</h2>
          <pre style={{ padding: 12, borderRadius: 8, background: "#f6f6f6", overflowX: "auto" }}>
            {JSON.stringify(data, null, 2)}
          </pre>
        </div>
      )}
    </main>
  );
}

