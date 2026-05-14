// apps/web/app/lib/api.ts

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/+$/, "") || "http://localhost:8000";

async function handleResponse(res: Response) {
  if (res.ok) {
    // try JSON first; if not JSON, return text
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) return await res.json();
    return await res.text();
  }

  // error path: prefer body text for debugging
  const text = await res.text();
  throw new Error(text || `API error ${res.status}`);
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

