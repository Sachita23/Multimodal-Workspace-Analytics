// Unit tests for fetchWithTimeout. Run with `npm test`.

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { RequestTimeoutError, fetchWithTimeout } from "./http.js";


const realFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = realFetch;
});


// A fetch that never answers (a suspended API process, a half-open socket).
// Like the real fetch it rejects when its signal aborts.
function hangingFetch(calls = []) {
  return (url, init = {}) => {
    calls.push({ url, init });

    return new Promise((_, reject) => {
      init.signal?.addEventListener("abort", () => {
        reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
      });
    });
  };
}


// A body that sends the given chunks `gapMs` apart, then optionally stalls.
function slowBody(chunks, gapMs, { stallAfter = false } = {}) {
  const encoder = new TextEncoder();
  let index = 0;

  return new ReadableStream({
    async pull(controller) {
      await new Promise((resolve) => setTimeout(resolve, gapMs));

      if (index < chunks.length) {
        controller.enqueue(encoder.encode(chunks[index]));
        index += 1;
        return;
      }

      if (stallAfter) {
        await new Promise(() => {});
      }

      controller.close();
    },
  });
}


test("returns the response and its text", async () => {
  globalThis.fetch = async () => new Response("hello", { status: 200 });

  const { response, text } = await fetchWithTimeout("/x", { timeoutMs: 200 });

  assert.equal(response.status, 200);
  assert.equal(text, "hello");
});


test("does not read the body of a 304 or an error status by default", async () => {
  globalThis.fetch = async () => new Response(null, { status: 304 });
  assert.equal((await fetchWithTimeout("/x", { timeoutMs: 200 })).text, null);

  globalThis.fetch = async () => new Response("nope", { status: 404 });
  assert.equal((await fetchWithTimeout("/x", { timeoutMs: 200 })).text, null);
});


test("a request that never answers fails with a timeout instead of hanging", async () => {
  const calls = [];
  globalThis.fetch = hangingFetch(calls);

  const started = Date.now();

  await assert.rejects(
    fetchWithTimeout("/api/analytics.csv", { timeoutMs: 60 }),
    (error) => error instanceof RequestTimeoutError && error.code === "timeout"
  );

  assert.ok(Date.now() - started < 1000);
  // The underlying request was aborted, not left open.
  assert.equal(calls[0].init.signal.aborted, true);
});


test("a fetch stub that ignores the abort signal still times out", async () => {
  globalThis.fetch = () => new Promise(() => {});

  await assert.rejects(
    fetchWithTimeout("/x", { timeoutMs: 40 }),
    (error) => error.code === "timeout"
  );
});


test("a body that stops arriving times out", async () => {
  globalThis.fetch = async () => new Response(slowBody(["a,b\n"], 5, { stallAfter: true }), { status: 200 });

  await assert.rejects(
    fetchWithTimeout("/x", { timeoutMs: 80 }),
    (error) => error.code === "timeout"
  );
});


test("a large body that keeps arriving is not cut off by the deadline", async () => {
  // 6 chunks 30 ms apart: 180 ms in total, longer than the 80 ms deadline,
  // but never quiet for 80 ms.
  const chunks = ["1\n", "2\n", "3\n", "4\n", "5\n", "6\n"];
  globalThis.fetch = async () => new Response(slowBody(chunks, 30), { status: 200 });

  const { text } = await fetchWithTimeout("/x", { timeoutMs: 80 });

  assert.equal(text, chunks.join(""));
});


test("the caller's signal still cancels the request", async () => {
  globalThis.fetch = hangingFetch();
  const controller = new AbortController();

  const pending = fetchWithTimeout("/x", { signal: controller.signal, timeoutMs: 5000 });
  setTimeout(() => controller.abort(), 10);

  await assert.rejects(pending, (error) => error.name === "AbortError");

  const aborted = new AbortController();
  aborted.abort();
  await assert.rejects(
    fetchWithTimeout("/x", { signal: aborted.signal }),
    (error) => error.name === "AbortError"
  );
});
