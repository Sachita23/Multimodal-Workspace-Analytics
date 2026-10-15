import DashboardLayout from "../layouts/DashboardLayout";
import { AnalyticsGate, CollectingState } from "../components/PageState";
import { MIN_ROWS_FOR_INSIGHTS } from "../data/analytics";
import { formatDuration, formatMinutes, isBehaviorLabel } from "../utils/format";

function InsightsContent({ data }) {
  const { metrics, applicationUsage, behaviorAnalytics, peakHour, quietHour, hourlyMode } = data;

  if (metrics.totalRecords < MIN_ROWS_FOR_INSIGHTS) {
    return (
      <>
        <div className="page-header"><div><h1>Insights</h1><p>Patterns and practical recommendations derived from your workspace activity.</p></div></div>
        <CollectingState records={metrics.totalRecords} />
      </>
    );
  }

  const perHour = hourlyMode === "timeline" ? "" : " (avg per day)";
  const topApp = applicationUsage[0];
  const totalAppTime = applicationUsage.reduce((sum, item) => sum + item.duration, 0);
  const topThreeTime = applicationUsage.slice(0, 3).reduce((sum, item) => sum + item.duration, 0);
  const topThreeShare = totalAppTime ? (topThreeTime / totalAppTime) * 100 : 0;
  const behaviorPatterns = behaviorAnalytics.filter((item) => isBehaviorLabel(item.behavior));
  // Null until the pipeline has clustered at least one finished session.
  const mostIdleBehavior = behaviorPatterns.length
    ? behaviorPatterns.reduce((best, item) => (item.avgIdleRatio > best.avgIdleRatio ? item : best))
    : null;
  const averageSwitchesPerSession = metrics.totalSessions ? metrics.applicationSwitches / metrics.totalSessions : 0;
  const anomalyRate = metrics.totalRecords ? (metrics.anomalousRecords / metrics.totalRecords) * 100 : 0;
  const scope = data.rangeLabel.toLowerCase();
  const focusMessage = metrics.focusScore >= 70
    ? `Strong sustained activity was recorded in the ${scope}.`
    : metrics.focusScore >= 45
      ? "Activity is balanced, with meaningful room to reduce idle time."
      : `Idle time is outweighing active work time in the ${scope}.`;

  const insightCards = [
    { category: "Focus health", title: `${metrics.focusScore}% focus score`, text: `${focusMessage} You logged ${formatDuration(metrics.totalActiveSeconds)} active and ${formatDuration(metrics.totalIdleSeconds)} idle.`, tone: "blue" },
    { category: "Tool concentration", title: `${topThreeShare.toFixed(1)}% in top 3 apps`, text: topApp ? `${topApp.application} led usage with ${formatDuration(topApp.duration)}, indicating your primary work environment.` : "No application activity was recorded.", tone: "green" },
    peakHour
      ? { category: "Best work window", title: peakHour.label, text: `${formatMinutes(peakHour.active)} active${perHour} were captured during your peak hour. Protect this time for deep work.`, tone: "purple" }
      : { category: "Best work window", title: "Not yet", text: "Your peak hour appears once a full hour of activity has been recorded.", tone: "purple" },
    quietHour
      ? { category: "Low-activity window", title: quietHour.label, text: `Only ${formatMinutes(quietHour.active)} active${perHour} were recorded. This is a natural slot for breaks or routine work.`, tone: "orange" }
      : { category: "Low-activity window", title: "Not yet", text: "A quiet window appears once at least two full hours have been recorded.", tone: "orange" },
    { category: "Context switching", title: `${averageSwitchesPerSession.toFixed(1)} per session`, text: `${metrics.applicationSwitches.toLocaleString()} application switches occurred across ${metrics.totalSessions.toLocaleString()} sessions.`, tone: "blue" },
    mostIdleBehavior
      ? { category: "Idle behavior", title: mostIdleBehavior.behavior, text: `This pattern has the highest idle ratio at ${mostIdleBehavior.avgIdleRatio.toFixed(1)}% of its tracked time.`, tone: "orange" }
      : { category: "Idle behavior", title: "Not enough sessions yet", text: `Behavior patterns appear once a few finished sessions have been classified. Overall, ${metrics.avgIdleRatio.toFixed(1)}% of tracked time was idle.`, tone: "orange" },
    metrics.hasBattery
      ? { category: "Resource readiness", title: `${metrics.minBattery.toFixed(0)}% lowest battery`, text: `Average CPU was ${metrics.avgCPU}% and memory was ${metrics.avgMemory}%, suggesting the observed workload was ${metrics.avgCPU > 70 ? "resource-intensive" : "within a moderate range"}.`, tone: "green" }
      : { category: "Resource readiness", title: `${metrics.avgCPU}% average CPU`, text: `Memory averaged ${metrics.avgMemory}%, suggesting the observed workload was ${metrics.avgCPU > 70 ? "resource-intensive" : "within a moderate range"}. No battery was detected on this machine.`, tone: "green" },
    { category: "Unusual activity", title: `${metrics.anomalyCount} unusual ${metrics.anomalyCount === 1 ? "episode" : "episodes"}`, text: metrics.anomalyCount ? `${anomalyRate.toFixed(1)}% of samples were flagged. The Anomalies page lists each episode, newest first.` : `No unusual activity was flagged in the ${scope}.`, tone: "red" },
  ];
  const recommendations = [
    peakHour
      ? { title: "Schedule around your peak", text: `Reserve ${peakHour.label} for demanding work; it is your strongest observed activity window.` }
      : { title: "Keep collecting", text: "Recommendations about your best hours appear after a full hour of activity." },
    { title: averageSwitchesPerSession > 10 ? "Reduce task fragmentation" : "Maintain task continuity", text: averageSwitchesPerSession > 10 ? "Group related work and silence nonessential notifications to limit context switching." : "Your switching pattern is relatively contained; continue grouping similar tasks together." },
    { title: metrics.anomalyCount ? "Review flagged episodes" : "Keep monitoring anomalies", text: metrics.anomalyCount ? "Inspect the unusual episodes to determine whether they reflect interruptions or expected work." : "Nothing is flagged right now, but periodic review can reveal early changes in work patterns." },
  ];

  return (
    <>
      <div className="page-header"><div><h1>Insights</h1><p>Rule-based observations and recommendations derived from your workspace activity.</p></div></div>
      <section className="insight-hero"><div><span>Workspace summary · {data.rangeLabel}</span><h2>{focusMessage}</h2><p>Across {metrics.totalRecords.toLocaleString()} activity samples{peakHour ? `, your strongest work period was ${peakHour.label}` : ""}.</p></div><div className="insight-hero-score"><strong>{metrics.focusScore}%</strong><span>focus score</span></div></section>
      <div className="insights-grid">{insightCards.map((insight) => <article className={`insight-card ${insight.tone}`} key={insight.category}><span>{insight.category}</span><h2>{insight.title}</h2><p>{insight.text}</p></article>)}</div>
      <section className="recommendations-card"><div className="chart-header"><div><h3>Recommended next steps</h3><p>Small changes suggested by the observed patterns.</p></div></div><div className="recommendations-grid">{recommendations.map((recommendation, index) => <div className="recommendation" key={recommendation.title}><span>{index + 1}</span><div><h4>{recommendation.title}</h4><p>{recommendation.text}</p></div></div>)}</div></section>
    </>
  );
}

function Insights() {
  return (
    <DashboardLayout>
      <AnalyticsGate loadingMessage="Generating workspace insights..." errorTitle="Unable to generate insights">
        {(data) => <InsightsContent data={data} />}
      </AnalyticsGate>
    </DashboardLayout>
  );
}

export default Insights;
