// Unit tests for the Focus page's poller. Run with `npm test`.

import assert from "node:assert/strict";
import { test } from "node:test";

import { pollEvery } from "./polling.js";


function fakeDocument() {
  const listeners = new Set();

  return {
    hidden: false,
    listeners,
    addEventListener: (type, listener) => listeners.add(listener),
    removeEventListener: (type, listener) => listeners.delete(listener),
    setHidden(hidden) {
      this.hidden = hidden;
      listeners.forEach((listener) => listener());
    },
  };
}


function manualTimer() {
  const timer = { callback: null, ms: null, cleared: false };

  return {
    timer,
    setTimer: (callback, ms) => {
      Object.assign(timer, { callback, ms });
      return 1;
    },
    clearTimer: () => {
      timer.cleared = true;
    },
  };
}


// A loader whose calls stay pending until resolved or rejected by hand.
function manualLoader() {
  const calls = [];

  const load = ({ signal }) => new Promise((resolve, reject) => {
    calls.push({ signal, resolve, reject });
  });

  return { load, calls };
}


const flush = () => new Promise((resolve) => setTimeout(resolve, 0));


test("loads now, then on every tick, never two calls at a time", async () => {
  const doc = fakeDocument();
  const { timer, setTimer, clearTimer } = manualTimer();
  const { load, calls } = manualLoader();
  const values = [];

  const stop = pollEvery(load, 5, { doc, setTimer, clearTimer, onValue: (value) => values.push(value) });

  assert.equal(calls.length, 1);
  assert.equal(timer.ms, 5000);

  // Still running: the tick is skipped.
  timer.callback();
  assert.equal(calls.length, 1);

  calls[0].resolve("first");
  await flush();
  assert.deepEqual(values, ["first"]);

  timer.callback();
  assert.equal(calls.length, 2);

  stop();
});


test("a failed call reports the error; the next tick tries again", async () => {
  const doc = fakeDocument();
  const { timer, setTimer, clearTimer } = manualTimer();
  const { load, calls } = manualLoader();
  const errors = [];

  const stop = pollEvery(load, 5, { doc, setTimer, clearTimer, onError: (error) => errors.push(error.status) });

  calls[0].reject(Object.assign(new Error("Not Found"), { status: 404 }));
  await flush();
  assert.deepEqual(errors, [404]);

  timer.callback();
  assert.equal(calls.length, 2);

  stop();
});


test("nothing is loaded while hidden; it catches up when shown", async () => {
  const doc = fakeDocument();
  const { timer, setTimer, clearTimer } = manualTimer();
  const { load, calls } = manualLoader();

  const stop = pollEvery(load, 5, { doc, setTimer, clearTimer });
  calls[0].resolve(1);
  await flush();

  doc.setHidden(true);
  timer.callback();
  timer.callback();
  assert.equal(calls.length, 1);

  doc.setHidden(false);
  assert.equal(calls.length, 2);

  stop();
});


test("stop() aborts the running call, drops its answer and clears everything", async () => {
  const doc = fakeDocument();
  const { timer, setTimer, clearTimer } = manualTimer();
  const { load, calls } = manualLoader();
  const values = [];

  const stop = pollEvery(load, 5, { doc, setTimer, clearTimer, onValue: (value) => values.push(value) });

  stop();
  assert.equal(calls[0].signal.aborted, true);
  assert.equal(timer.cleared, true);
  assert.equal(doc.listeners.size, 0);

  calls[0].resolve("late");
  await flush();
  assert.deepEqual(values, []);
});


test("a call left over from a stopped poller does not block the next one (StrictMode)", async () => {
  const doc = fakeDocument();
  const { setTimer, clearTimer } = manualTimer();
  const { load, calls } = manualLoader();
  const values = [];
  const options = { doc, setTimer, clearTimer, onValue: (value) => values.push(value) };

  const stopFirst = pollEvery(load, 5, options);
  stopFirst();

  const stopSecond = pollEvery(load, 5, options);
  assert.equal(calls.length, 2);

  // The first call settling late neither reports nor resets the second.
  calls[0].resolve("stale");
  calls[1].resolve("fresh");
  await flush();
  assert.deepEqual(values, ["fresh"]);

  stopSecond();
});
