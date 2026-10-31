// --------------------------------------------------
// A small visibility-aware poller (used by the Focus page)
// --------------------------------------------------


/**
 * Calls load({signal}) now, then every `seconds` while the page is visible,
 * and once as soon as it becomes visible again. A call is skipped while the
 * previous one is still running (every dashboard request has a deadline, so
 * that never lasts long). onValue(value) and onError(error) are only called
 * until stop().
 *
 * Returns stop(), which aborts the running call and clears the timer and
 * listener. Each start has its own state, so a call left over from an earlier
 * start (React StrictMode mounts effects twice) can neither report a value
 * nor block the new start's calls.
 */
export function pollEvery(load, seconds, {
  onValue = () => {},
  onError = () => {},
  doc = globalThis.document,
  setTimer = setInterval,
  clearTimer = clearInterval,
} = {}) {
  const controller = new AbortController();
  const { signal } = controller;
  let running = false;

  async function run() {
    if (running || signal.aborted) return;

    running = true;

    try {
      const value = await load({ signal });
      if (!signal.aborted) onValue(value);
    } catch (error) {
      if (!signal.aborted) onError(error);
    } finally {
      running = false;
    }
  }

  function runIfVisible() {
    if (!doc.hidden) run();
  }

  run();

  const timer = setTimer(runIfVisible, seconds * 1000);
  doc.addEventListener("visibilitychange", runIfVisible);

  return () => {
    controller.abort();
    clearTimer(timer);
    doc.removeEventListener("visibilitychange", runIfVisible);
  };
}
