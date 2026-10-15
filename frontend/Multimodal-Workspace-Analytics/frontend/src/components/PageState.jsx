import { useAnalytics } from "../context/AnalyticsProvider";
import { MIN_ROWS_FOR_INSIGHTS } from "../data/analytics";


export function LoadingState({ message = "Loading…" }) {
  return (
    <div className="loading-state" role="status" aria-live="polite">
      {message}
    </div>
  );
}


export function ErrorState({ title = "Unable to load analytics", error, onRetry }) {
  const message = typeof error === "string" ? error : error?.message;

  return (
    <div className="error-state" role="alert">
      <h2>{title}</h2>
      {message && <p>{message}</p>}
      {onRetry && (
        <button type="button" className="range-selector state-action" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}


export function EmptyState({ title, children, action }) {
  return (
    <div className="empty-state">
      <h2>{title}</h2>
      {children && <div className="empty-state-body">{children}</div>}
      {action}
    </div>
  );
}


// "Collecting data…" placeholder shown while there are too few samples.
export function CollectingState({ records }) {
  const progress = Math.min(100, Math.round((records / MIN_ROWS_FOR_INSIGHTS) * 100));

  return (
    <EmptyState title="Collecting data…">
      Insights appear after a few minutes of activity ({records} of {MIN_ROWS_FOR_INSIGHTS} samples so far, {progress}%).
    </EmptyState>
  );
}


/**
 * Renders loading, error and empty states for the single-user pages, and
 * calls children(data) once analytics for the selected range are available.
 */
// The live pipeline's last failure, when /api/status reports one.
function PipelineError({ status }) {
  const message = status?.pipeline?.last_error;

  if (!message) return null;

  return (
    <p className="pipeline-error" role="alert">
      The last pipeline run failed: <code>{message}</code>
    </p>
  );
}


export function AnalyticsGate({ loadingMessage, errorTitle, allowEmptyRange = false, children }) {
  const { data, loading, error, refresh, setRange, status } = useAnalytics();

  if (!data) {
    if (loading || !error) {
      return <LoadingState message={loadingMessage} />;
    }

    // 404: the server is up but has no processed file (fresh start, or the
    // pipeline keeps failing). Say so, with the pipeline's own error.
    if (error.code === "no-data") {
      return (
        <EmptyState
          title="No processed data yet"
          action={(
            <button type="button" className="range-selector state-action" onClick={() => refresh().catch(() => {})}>
              Check again
            </button>
          )}
        >
          {error.message}
          <PipelineError status={status} />
        </EmptyState>
      );
    }

    return (
      <ErrorState
        title={errorTitle}
        error={error}
        onRetry={() => refresh().catch(() => {})}
      />
    );
  }

  if (data.totalRowsAll === 0) {
    return (
      <EmptyState title="No activity recorded yet">
        The collector writes a sample every 5 seconds and the live pipeline
        processes it every 10 seconds. Keep `python start_project.py` running
        and this page fills in by itself.
        <PipelineError status={status} />
      </EmptyState>
    );
  }

  if (!allowEmptyRange && data.metrics.totalRecords === 0) {
    return (
      <EmptyState
        title={`No activity in the ${data.rangeLabel.toLowerCase()}`}
        action={(
          <button type="button" className="range-selector state-action" onClick={() => setRange("all")}>
            Show all time
          </button>
        )}
      >
        {data.totalRowsAll.toLocaleString()} older samples are available.
      </EmptyState>
    );
  }

  return children(data);
}
