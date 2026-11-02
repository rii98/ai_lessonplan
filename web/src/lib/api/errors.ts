/** The server's uniform error envelope: `{error: {code, message, details, request_id}}`. */
export interface ErrorEnvelope {
  error?: { code?: string; message?: string; details?: unknown; request_id?: string };
  detail?: unknown;
}

export interface FieldIssue {
  loc: string;
  msg: string;
}

/** A failed API call. `code` is the stable machine-readable reason (`auth.missing`, `validation`, …). */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;
  readonly requestId: string | undefined;

  constructor(status: number, code: string, message: string, details?: unknown, requestId?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }

  /** Field-level issues when the server rejected a payload (422 `validation`). */
  get fieldIssues(): FieldIssue[] {
    return Array.isArray(this.details)
      ? this.details.filter((d): d is FieldIssue => !!d && typeof d === "object" && "msg" in d)
      : [];
  }

  /** Where to sign in, when the server says the caller is not authenticated. */
  get loginUrl(): string | undefined {
    const d = this.details as { login_url?: unknown } | null;
    return d && typeof d.login_url === "string" ? d.login_url : undefined;
  }

  static async fromResponse(res: Response): Promise<ApiError> {
    let body: ErrorEnvelope | null = null;
    try {
      body = (await res.clone().json()) as ErrorEnvelope;
    } catch {
      /* non-JSON body */
    }
    const e = body?.error;
    const fallback = res.statusText || `Request failed (${res.status})`;
    const message = e?.message ?? (typeof body?.detail === "string" ? body.detail : fallback);
    return new ApiError(res.status, e?.code ?? `http.${res.status}`, message, e?.details,
      e?.request_id ?? res.headers.get("x-request-id") ?? undefined);
  }
}

/** A human sentence for any thrown value — what the UI shows in a banner or toast. */
export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 0) return "Can't reach LessonForge. Check your connection and try again.";
    const issues = err.fieldIssues;
    if (issues.length) return `${err.message}: ${issues.slice(0, 3).map((i) => `${i.loc} — ${i.msg}`).join("; ")}`;
    if (err.status === 403) return err.message || "You don't have access to this.";
    if (err.status >= 500) return "Something went wrong on our side. Please try again in a moment.";
    return err.message;
  }
  if (err instanceof DOMException && err.name === "AbortError") return "Cancelled.";
  if (err instanceof Error) return err.message;
  return "Something went wrong.";
}

export const isAbort = (err: unknown): boolean => err instanceof DOMException && err.name === "AbortError";
