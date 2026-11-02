import type { ApiError } from "./errors";

/**
 * How requests are authenticated. The client never knows which one is in use, so
 * the same UI runs inside LessonForge (cookie session from the Edboox launch) or
 * inside Edboox (its own bearer tokens).
 */
export interface AuthStrategy {
  /** Add credentials to an outgoing request. */
  prepare(init: RequestInit & { headers: Headers }): Promise<void> | void;
  /** Called once when the server answers 401. */
  onUnauthorized?(err: ApiError): void;
}

const CSRF_HEADER = "X-LF-Request";

/**
 * Same-origin session cookie set by `/auth/launch`. Cookie-authenticated writes
 * must carry `X-LF-Request: 1` (a cross-site form can't set custom headers). On a
 * 401 the user is sent to Edboox to sign in — the URL comes from the server.
 */
export function cookieSession(opts: { redirect?: (url: string) => void } = {}): AuthStrategy {
  let redirecting = false;
  const redirect = opts.redirect ?? ((url: string) => window.location.assign(url));
  return {
    prepare(init) {
      init.credentials = "include";
      init.headers.set(CSRF_HEADER, "1");
    },
    onUnauthorized(err) {
      const url = err.loginUrl;
      if (url && !redirecting) {
        redirecting = true;
        redirect(url);
      }
    },
  };
}

/** A bearer token from the host app (e.g. Edboox calling the API with its own token). */
export function bearerToken(getToken: () => string | null | Promise<string | null>,
                            onUnauthorized?: (err: ApiError) => void): AuthStrategy {
  return {
    async prepare(init) {
      const token = await getToken();
      if (token) init.headers.set("Authorization", `Bearer ${token}`);
    },
    onUnauthorized,
  };
}
