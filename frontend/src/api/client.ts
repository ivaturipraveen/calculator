import type {
  CalculateRequest,
  CalculateResponse,
  CalculatorSchema,
  CalculatorSummary,
  CatalogMeta,
} from "./types";

// `GET /api/calculators/{name}` returns the raw spec. The UI never needs it --
// it renders from /schema -- so there is no client method for it; reach for
// curl or the docs page when you want to read a calculator's formulas.

const BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { signal });
  if (!res.ok) {
    throw new ApiError(res.status, await errorText(res));
  }
  return res.json() as Promise<T>;
}

async function post<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    throw new ApiError(res.status, await errorText(res));
  }
  return res.json() as Promise<T>;
}

async function errorText(res: Response): Promise<string> {
  try {
    const body = await res.json();
    return typeof body?.detail === "string" ? body.detail : res.statusText;
  } catch {
    return res.statusText;
  }
}

export const api = {
  catalog: (signal?: AbortSignal) => get<CatalogMeta>("/api/catalog", signal),

  list: (
    params: { q?: string; category?: string; renderer?: string } = {},
    signal?: AbortSignal,
  ) => {
    const search = new URLSearchParams({ limit: "500" });
    if (params.q) search.set("q", params.q);
    if (params.category) search.set("category", params.category);
    if (params.renderer) search.set("renderer", params.renderer);
    return get<{ count: number; calculators: CalculatorSummary[] }>(
      `/api/calculators?${search}`,
      signal,
    );
  },

  schema: (slug: string, signal?: AbortSignal) =>
    get<CalculatorSchema>(`/api/calculators/${slug}/schema`, signal),

  calculate: (slug: string, body: CalculateRequest, signal?: AbortSignal) =>
    post<CalculateResponse>(`/api/calculators/${slug}/calculate`, body, signal),
};

export { BASE as API_BASE };
