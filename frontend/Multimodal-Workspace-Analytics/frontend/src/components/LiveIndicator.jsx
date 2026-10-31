import { useEffect, useState } from "react";

import { useAnalytics } from "../context/AnalyticsProvider";
import { RANGES } from "../data/analytics";
import { describeLiveStatus } from "../utils/liveStatus";


function useNow(intervalMs) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);

  return now;
}


/**
 * "Live · updated 6 s ago", amber when the collector or pipeline stalls.
 */
function LiveIndicator() {
  const analytics = useAnalytics();
  const now = useNow(1000);
  const { tone, label, detail, title } = describeLiveStatus(analytics, now);

  return (
    <div className={`live-indicator live-${tone}`} title={title}>
      <span className="live-indicator-dot" aria-hidden="true" />
      <strong role="status">{label}</strong>
      {detail && <span className="live-indicator-detail">· {detail}</span>}
    </div>
  );
}


export function RangeSelect() {
  const { range, setRange } = useAnalytics();

  return (
    <label className="range-select">
      <span className="sr-only">Time range</span>
      <select
        className="range-selector"
        value={range}
        onChange={(event) => setRange(event.target.value)}
      >
        {RANGES.map((item) => (
          <option key={item.value} value={item.value}>{item.label}</option>
        ))}
      </select>
    </label>
  );
}


export default LiveIndicator;
