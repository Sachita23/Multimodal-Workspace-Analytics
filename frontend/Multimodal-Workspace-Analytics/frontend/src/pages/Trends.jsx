import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import DashboardLayout from "../layouts/DashboardLayout";
import { AnalyticsGate } from "../components/PageState";
import { BRAND_COLOR, CHART_COLORS, NEUTRAL_COLOR, formatMinutes } from "../utils/format";

function TrendsContent({ data }) {
  const { hourlyActivity, resourceByHour, metrics, peakHour, hourlyMode, hourlyDays } = data;
  const timeline = hourlyMode === "timeline";
  const unitNote = timeline ? "in the last 24 hours" : `avg per day over ${hourlyDays} ${hourlyDays === 1 ? "day" : "days"}`;
  const minutesTooltip = (value, name) => [formatMinutes(value), name];
  const percentTooltip = (value, name) => [value === null ? "--" : `${value}%`, name];

  return (
    <>
      <div className="page-header"><div><h1>Trends</h1><p>Explore how workspace activity and device resources change over time.</p></div></div>
      <div className="stats-grid stats-grid-3">
        <div className="stat-card"><span>Peak Activity</span><h2>{peakHour ? peakHour.label : "--"}</h2><p>{peakHour ? `${formatMinutes(peakHour.active)} active (${timeline ? "that hour" : "avg per day"})` : "Needs a full hour of data"}</p></div>
        <div className="stat-card"><span>Avg CPU</span><h2>{metrics.avgCPU}%</h2><p>{data.rangeLabel}</p></div>
        <div className="stat-card"><span>Avg Memory</span><h2>{metrics.avgMemory}%</h2><p>{data.rangeLabel}</p></div>
      </div>
      <div className="dashboard-grid">
        <div className="chart-card large"><div className="chart-header"><div><h3>Activity Trend</h3><p>Active and idle minutes by hour, {unitNote}</p></div></div><div className="chart-container"><ResponsiveContainer width="100%" height="100%"><LineChart data={hourlyActivity}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="label" minTickGap={16} /><YAxis unit=" min" width={56} /><Tooltip formatter={minutesTooltip} /><Legend /><Line type="monotone" dataKey="active" name="Active time" stroke={CHART_COLORS[0]} strokeWidth={2} dot={false} isAnimationActive={false} /><Line type="monotone" dataKey="idle" name="Idle time" stroke={NEUTRAL_COLOR} strokeWidth={2} dot={false} isAnimationActive={false} /></LineChart></ResponsiveContainer></div></div>
        <div className="chart-card"><div className="chart-header"><div><h3>Hourly Activity</h3><p>Active minutes at each hour, {unitNote}</p></div></div><div className="chart-container"><ResponsiveContainer width="100%" height="100%"><BarChart data={hourlyActivity}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="label" minTickGap={16} /><YAxis unit=" min" width={56} /><Tooltip formatter={minutesTooltip} /><Bar dataKey="active" name="Active time" fill={BRAND_COLOR} radius={[4, 4, 0, 0]} isAnimationActive={false} /></BarChart></ResponsiveContainer></div></div>
      </div>
      <div className="chart-card trends-resource-card"><div className="chart-header"><div><h3>Resource Trend</h3><p>Average CPU, memory{metrics.hasBattery ? ", and battery" : ""} percentage by hour</p></div></div><div className="chart-container"><ResponsiveContainer width="100%" height="100%"><LineChart data={resourceByHour}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="label" minTickGap={16} /><YAxis unit="%" domain={[0, 100]} /><Tooltip formatter={percentTooltip} /><Legend /><Line type="monotone" dataKey="cpu" stroke={CHART_COLORS[0]} name="CPU" strokeWidth={2} dot={false} isAnimationActive={false} /><Line type="monotone" dataKey="memory" stroke={CHART_COLORS[1]} name="Memory" strokeWidth={2} dot={false} isAnimationActive={false} />{metrics.hasBattery && <Line type="monotone" dataKey="battery" stroke={CHART_COLORS[2]} name="Battery" strokeWidth={2} dot={false} isAnimationActive={false} />}</LineChart></ResponsiveContainer></div></div>
    </>
  );
}

function Trends() {
  return (
    <DashboardLayout>
      <AnalyticsGate loadingMessage="Loading trend analytics..." errorTitle="Unable to load trends">
        {(data) => <TrendsContent data={data} />}
      </AnalyticsGate>
    </DashboardLayout>
  );
}

export default Trends;
