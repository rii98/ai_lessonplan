import type { AuthStrategy } from "./auth";
import { filenameFrom, saveBlob } from "./download";
import { ApiError } from "./errors";
import { readSse, type SseEvent } from "./sse";

export type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export type Query = Record<string, string | number | boolean | null | undefined>;

export interface RequestOptions {
  body?: unknown;
  query?: Query;
  signal?: AbortSignal;
}

export interface ApiClientConfig {
  /** Where the versioned API lives — `/api/v1` same-origin, or an absolute URL. */
  baseUrl: string;
  auth: AuthStrategy;
}

export interface ApiClient {
  request<T>(method: Method, path: string, opts?: RequestOptions): Promise<T>;
  get<T>(path: string, query?: Query, signal?: AbortSignal): Promise<T>;
  post<T>(path: string, body?: unknown, opts?: Omit<RequestOptions, "body">): Promise<T>;
  /** POST/GET a file and save it; resolves with the saved filename. */
  download(method: Method, path: string, opts: RequestOptions & { fallbackName: string }): Promise<string>;
  /** POST and iterate the Server-Sent Events of the response. */
  stream(path: string, body: unknown, signal?: AbortSignal): AsyncGenerator<SseEvent>;
}

function buildUrl(base: string, path: string, query?: Query): string {
  const url = base.replace(/\/$/, "") + (path.startsWith("/") ? path : `/${path}`);
  if (!query) return url;
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  const s = qs.toString();
  return s ? `${url}?${s}` : url;
}

/**
 * The one place that talks HTTP. Everything above it deals in typed values and
 * `ApiError`s — never `Response`s — so swapping the transport (or the auth) never
 * touches a screen.
 */
export function createApiClient({ baseUrl, auth }: ApiClientConfig): ApiClient {
  async function send(method: Method, path: string, { body, query, signal }: RequestOptions = {}): Promise<Response> {
    const headers = new Headers({ Accept: "application/json" });
    const init: RequestInit & { headers: Headers } = { method, headers, signal };
    if (body !== undefined) {
      headers.set("Content-Type", "application/json");
      init.body = JSON.stringify(body);
    }
    await auth.prepare(init);
    let res: Response;
    try {
      res = await fetch(buildUrl(baseUrl, path, query), init);
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") throw err;
      throw new ApiError(0, "network", "network error");
    }
    if (!res.ok) {
      const err = await ApiError.fromResponse(res);
      if (res.status === 401) auth.onUnauthorized?.(err);
      throw err;
    }
    return res;
  }

  const client: ApiClient = {
    async request<T>(method: Method, path: string, opts?: RequestOptions): Promise<T> {
      const res = await send(method, path, opts);
      if (res.status === 204) return undefined as T;
      const text = await res.text();
      return (text ? JSON.parse(text) : undefined) as T;
    },
    get: (path, query, signal) => client.request("GET", path, { query, signal }),
    post: (path, body, opts) => client.request("POST", path, { ...opts, body }),
    async download(method, path, { fallbackName, ...opts }) {
      const res = await send(method, path, opts);
      const name = filenameFrom(res, fallbackName);
      saveBlob(await res.blob(), name);
      return name;
    },
    async *stream(path, body, signal) {
      const res = await send("POST", path, { body, signal });
      if (!res.body) throw new ApiError(res.status, "stream", "the server sent no stream");
      yield* readSse(res.body);
    },
  };
  return client;
}
