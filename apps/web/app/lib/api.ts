// apps/web/app/lib/api.ts

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/+$/, "") || "http://localhost:8000";

function safeApiErrorMessage(status: number, bodyText: string): string {
  const trimmed = bodyText.trim();
  let detail: string | undefined;
  if (trimmed.startsWith("{")) {
    try {
      const parsed = JSON.parse(trimmed) as { detail?: unknown };
      if (typeof parsed.detail === "string") {
        detail = parsed.detail;
      } else if (Array.isArray(parsed.detail)) {
        const parts = parsed.detail
          .map((item: { msg?: string } | string) => (typeof item === "string" ? item : item?.msg))
          .filter((x): x is string => Boolean(x));
        if (parts.length) detail = parts.join(" ");
      }
    } catch {
      /* ignore parse errors */
    }
  }

  if (status === 401 || status === 403) {
    return detail || "Authentication failed.";
  }
  if (status === 404) {
    return detail || "The requested resource was not found.";
  }
  if (status === 422) {
    return detail || "The request could not be processed.";
  }
  if (status >= 500) {
    return "The service is temporarily unavailable. Please try again.";
  }
  return detail || "Something went wrong. Please try again.";
}

async function handleResponse(res: Response) {
  if (res.ok) {
    // try JSON first; if not JSON, return text
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) return await res.json();
    return await res.text();
  }

  const text = await res.text();
  throw new Error(safeApiErrorMessage(res.status, text));
}

export async function getJson(path: string, headers: Record<string, string> = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "GET",
    headers: {
      ...headers,
    },
  });
  return await handleResponse(res);
}

export async function postJson(path: string, body: unknown, headers: Record<string, string> = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...headers,
    },
    body: JSON.stringify(body),
  });

  return await handleResponse(res);
}

export async function postForm(path: string, form: FormData, headers: Record<string, string> = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      ...headers,
      // DO NOT set Content-Type here; browser sets multipart boundary automatically.
    },
    body: form,
  });

  return await handleResponse(res);
}
