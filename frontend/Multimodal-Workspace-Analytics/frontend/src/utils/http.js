// --------------------------------------------------
// fetch() with a deadline
// --------------------------------------------------
//
// A browser fetch has no timeout of its own, and neither does the Vite /api
// proxy. When the API process is suspended, or a socket goes half-open after
// the laptop sleeps, a request can stay pending for minutes. Pollers that skip
// a tick while a request is in flight would then stop for good, so every
// dashboard request goes through fetchWithTimeout.


// The server must start answering, and keep sending, within this long.
// Twice the 10 s refresh interval: generous for a local API, short enough
// that a stuck request is noticed and retried.
export const REQUEST_TIMEOUT_SECONDS = 20;


export class RequestTimeoutError extends Error {
  constructor(seconds) {
    super(
      `The dashboard server stopped responding (no answer for ${Math.round(seconds)} s). ` +
      "Retrying automatically."
    );
    this.name = "TimeoutError";
    this.code = "timeout";
  }
}


function abortError(signal) {
  const reason = signal?.reason;

  if (reason instanceof Error && reason.name === "AbortError") return reason;

  const error = new Error("The request was aborted.");
  error.name = "AbortError";
  return error;
}


async function readText(response, guarded, arm) {
  const body = response.body;

  if (!body || typeof body.getReader !== "function") {
    arm();
    return guarded(response.text());
  }

  const reader = body.getReader();
  const decoder = new TextDecoder();
  let text = "";

  try {
    for (;;) {
      // Re-armed for every chunk: a large file that keeps arriving is fine,
      // a transfer that stalls is not.
      arm();
      const { done, value } = await guarded(reader.read());

      if (done) break;

      text += decoder.decode(value, { stream: true });
    }
  } catch (error) {
    reader.cancel().catch(() => {});
    throw error;
  }

  return text + decoder.decode();
}


/**
 * fetch(url, init) that gives up when the server goes quiet: no response
 * headers, or no new body bytes, for `timeoutMs`. It then rejects with a
 * RequestTimeoutError (error.code === "timeout"). The caller's `signal`
 * still cancels it, rejecting with an AbortError.
 *
 * Resolves to {response, text}. The body is read (as text) only when
 * readBody(response) is true; otherwise text is null.
 */
export async function fetchWithTimeout(url, {
  signal,
  timeoutMs = REQUEST_TIMEOUT_SECONDS * 1000,
  readBody = (response) => response.ok,
  ...init
} = {}) {
  const controller = new AbortController();
  let timer = null;
  let fail = null;

  // Rejects when the deadline passes or the caller aborts, even if the
  // underlying fetch (or a test stub) ignores the abort signal.
  const interrupted = new Promise((_, reject) => {
    fail = reject;
  });
  interrupted.catch(() => {});

  const guarded = (promise) => Promise.race([promise, interrupted]);

  const arm = () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      fail(new RequestTimeoutError(timeoutMs / 1000));
      controller.abort();
    }, timeoutMs);
  };

  const onAbort = () => {
    fail(abortError(signal));
    controller.abort(signal.reason);
  };

  if (signal?.aborted) {
    throw abortError(signal);
  }

  signal?.addEventListener("abort", onAbort, { once: true });

  try {
    arm();

    const response = await guarded(fetch(url, { ...init, signal: controller.signal }));
    const text = readBody(response) ? await readText(response, guarded, arm) : null;

    return { response, text };
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
  }
}
