/* Runs before every other setup file — and so before MSW wraps `fetch`.
 *
 * jsdom replaces AbortController, and Node's fetch/Request reject its signals
 * ("Expected signal to be an instance of AbortSignal"). Under test we drop
 * signals from requests; no test relies on cancelling an HTTP request (stream
 * cancellation is exercised against the pure turn reducer instead). */
const strip = (init?: RequestInit) => (init?.signal ? { ...init, signal: undefined } : init);

const NativeRequest = globalThis.Request;
globalThis.Request = class extends NativeRequest {
  constructor(input: RequestInfo | URL, init?: RequestInit) { super(input, strip(init)); }
} as typeof Request;

const nativeFetch = globalThis.fetch;
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => nativeFetch(input, strip(init))) as typeof fetch;

export {};
