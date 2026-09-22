import { useEffect, useState } from "react";
import DashboardLayout from "../layouts/DashboardLayout";
import { loadAnalyticsData } from "../data/analytics";

function formatDuration(seconds) {
  const minutes = Math.round(seconds / 60);
  const hours = Math.floor(minutes / 60);
  return hours > 0 ? `${hours}h ${minutes % 60}m` : `${minutes} min`;
}

function formatHour(hour) {
  return `${String(hour).padStart(2, "0")}:00`;
}

function Insights() {
  const [analytics, setAnalytics] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    loadAnalyticsData().then(setAnalytics).catch(() => setError("Unable to generate insights."));
  }, []);

  if (error) return <DashboardLayout><div className="error-state">{error}</div></DashboardLayout>;
  if (!analytics) return <DashboardLayout><div className="loading-state">Generating workspace insights...</div></DashboardLayout>;

  const { metrics, applicationUsage, behaviorAnalytics, hourlyActivity, resourceByHour, anomalies } = analytics;
  const topApp = applicationUsage[0];
  const totalAppTime = applicationUsage.reduce((sum, item) => sum + item.duration, 0);
  const topThreeTime = applicationUsage.slice(0, 3).reduce((sum, item) => sum + item.duration, 0);
  const topThreeShare = totalAppTime ? (topThreeTime / totalAppTime) * 100 : 0;
  const busiestHour = hourlyActivity.reduce((best, item) => item.active > best.active ? item : best, { hour: 0, active: 0 });
  const quietestHour = hourlyActivity.reduce((best, item) => item.active < best.active ? item : best, hourlyActivity[0] || { hour: 0, active: 0 });
  const mostIdleBehavior = behaviorAnalytics.reduce(
    (best, item) => Number(item.avgIdleRatio) > Number(best.avgIdleRatio) ? item : best,
    behaviorAnalytics[0] || { behavior: "Unknown", avgIdleRatio: 0 }
  );
  const averageSwitchesPerSession = metrics.totalSessions ? metrics.applicationSwitches / metrics.totalSessions : 0;
  const anomalyRate = metrics.totalRecords ? (anomalies.length / metrics.totalRecords) * 100 : 0;
  const batteryValues = resourceByHour.map((item) => item.battery).filter(Number.isFinite);
  const lowestBattery = batteryValues.length ? Math.min(...batteryValues) : 0;
  const focusMessage = metrics.focusScore >= 70
    ? "Strong sustained activity was recorded across the dataset."
    : metrics.focusScore >= 45
      ? "Activity is balanced, with meaningful room to reduce idle time."
      : "Idle time is outweighing active work time in this dataset.";

  const insightCards = [
    { category: "Focus health", title: `${metrics.focusScore}% focus score`, text: `${focusMessage} You logged ${formatDuration(metrics.totalActiveSeconds)} active and ${formatDuration(metrics.totalIdleSeconds)} idle.`, tone: "blue" },
    { category: "Tool concentration", title: `${topThreeShare.toFixed(1)}% in top 3 apps`, text: topApp ? `${topApp.application} led usage with ${formatDuration(topApp.duration)}, indicating your primary work environment.` : "No application activity was recorded.", tone: "green" },
    { category: "Best work window", title: formatHour(busiestHour.hour), text: `${Math.round(busiestHour.active)} active minutes were captured during your peak hour. Protect this time for deep work.`, tone: "purple" },
    { category: "Low-activity window", title: formatHour(quietestHour.hour), text: `Only ${Math.round(quietestHour.active)} active minutes were recorded. This is a natural slot for breaks or routine work.`, tone: "orange" },
    { category: "Context switching", title: `${averageSwitchesPerSession.toFixed(1)} per session`, text: `${metrics.applicationSwitches.toLocaleString()} application switches occurred across ${metrics.totalSessions.toLocaleString()} sessions.`, tone: "blue" },
    { category: "Idle behavior", title: mostIdleBehavior.behavior || "Unknown", text: `This pattern has the highest average idle ratio at ${(Number(mostIdleBehavior.avgIdleRatio) * 100).toFixed(1)}%.`, tone: "orange" },
    { category: "Resource readiness", title: `${lowestBattery.toFixed(1)}% lowest battery`, text: `Average CPU was ${metrics.avgCPU}% and memory was ${metrics.avgMemory}%, suggesting the observed workload was ${metrics.avgCPU > 70 ? "resource-intensive" : "within a moderate range"}.`, tone: "green" },
    { category: "Unusual activity", title: `${anomalyRate.toFixed(2)}% flagged`, text: anomalies.length ? `${anomalies.length.toLocaleString()} records need review. The Anomalies page ranks them by detection score.` : "No unusual records were flagged in this dataset.", tone: "red" },
  ];
  const recommendations = [
    { title: "Schedule around your peak", text: `Reserve ${formatHour(busiestHour.hour)} for demanding work; it is your strongest observed activity window.` },
    { title: averageSwitchesPerSession > 10 ? "Reduce task fragmentation" : "Maintain task continuity", text: averageSwitchesPerSession > 10 ? "Group related work and silence nonessential notifications to limit context switching." : "Your switching pattern is relatively contained; continue grouping similar tasks together." },
    { title: anomalyRate > 1 ? "Review flagged events" : "Keep monitoring anomalies", text: anomalyRate > 1 ? "Inspect high-scoring records to determine whether they reflect interruptions or expected work." : "The anomaly rate is low, but periodic review can reveal early changes in work patterns." },
  ];

  return (
    <DashboardLayout>
      <div className="page-header"><div><h1>Insights</h1><p>Patterns and practical recommendations derived from your workspace activity.</p></div></div>
      <section className="insight-hero"><div><span>Workspace summary</span><h2>{focusMessage}</h2><p>Across {metrics.totalRecords.toLocaleString()} activity records, your strongest work period was {formatHour(busiestHour.hour)}.</p></div><div className="insight-hero-score"><strong>{metrics.focusScore}%</strong><span>focus score</span></div></section>
      <div className="insights-grid">{insightCards.map((insight) => <article className={`insight-card ${insight.tone}`} key={insight.category}><span>{insight.category}</span><h2>{insight.title}</h2><p>{insight.text}</p></article>)}</div>
      <section className="recommendations-card"><div className="chart-header"><div><h3>Recommended next steps</h3><p>Small changes suggested by the observed patterns.</p></div></div><div className="recommendations-grid">{recommendations.map((recommendation, index) => <div className="recommendation" key={recommendation.title}><span>{index + 1}</span><div><h4>{recommendation.title}</h4><p>{recommendation.text}</p></div></div>)}</div></section>
    </DashboardLayout>
  );
}

export default Insights;
