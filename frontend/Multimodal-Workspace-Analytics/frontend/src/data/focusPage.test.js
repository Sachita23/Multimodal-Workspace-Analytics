// Unit tests for what the Focus page shows. Run with `npm test`.

import assert from "node:assert/strict";
import { test } from "node:test";

import { FocusApiError, focusPageState } from "./focus.js";


const loading = { value: null, error: null, loading: true };
const loaded = (value) => ({ value, error: null, loading: false });
const failed = (error, value = null) => ({ value, error, loading: false });

const notFound = new FocusApiError("This API server has no Focus endpoints yet.", 404);
const offline = new FocusApiError("Can't reach the dashboard server.", 0);


test("a 404 from any Focus request means the server has no Focus API", () => {
  const page = focusPageState({ live: failed(notFound), summary: loading, sessions: loading });

  assert.equal(page.state, "not-installed");
  assert.equal(page.error, notFound);

  assert.equal(focusPageState({ live: loaded({}), summary: loaded({}), sessions: failed(notFound) }).state, "not-installed");
});


test("loading until every request has answered once", () => {
  assert.equal(focusPageState({ live: loaded({}), summary: loading, sessions: loaded([]) }).state, "loading");
});


test("an error page only when no history ever loaded", () => {
  const none = focusPageState({ live: loaded({}), summary: failed(offline), sessions: failed(offline) });
  assert.deepEqual(none, { state: "error", error: offline });

  // A later failure keeps showing the last good history.
  const kept = focusPageState({ live: failed(offline), summary: failed(offline, { sessions: 2 }), sessions: loaded([]) });
  assert.equal(kept.state, "ready");

  assert.equal(focusPageState({ live: loaded({}), summary: loaded({}), sessions: loaded([]) }).state, "ready");
});
