import { useEffect, useState } from "react";
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
import { loadAnalyticsData } from "../data/analytics";

function Trends() {
  const [analytics, setAnalytics] = useState(null);

  useEffect(() => {
    loadAnalyticsData().then(setAnalytics).catch(console.error);
  }, []);

  if (!analytics) {
    return <DashboardLayout><div className="loading-state">Loading trend analytics...</div></DashboardLayout>;
  }

  const { hourlyActivity, resourceByHour, metrics } = analytics;
  const busiestHour = hourlyActivity.reduce(
    (busiest, item) => item.active > busiest.active ? item : busiest,
    { hour: 0, active: 0 }
  );
  const mostIdleHour = hourlyActivity.reduce(
    (mostIdle, item) => item.idle > mostIdle.idle ? item : mostIdle,
    { hour: 0, idle: 0 }
  );

  return (
    <DashboardLayout>
      <div className="page-header"><div><h1>Trends</h1><p>Explore how workspace activity and device resources change over time.</p></div></div>
      <div className="stats-grid">
        <div className="stat-card"><span>Peak Activity</span><h2>{busiestHour.hour}:00</h2><p>{Math.round(busiestHour.active)} active minutes</p></div>
        <div className="stat-card"><span>Most Idle</span><h2>{mostIdleHour.hour}:00</h2><p>{Math.round(mostIdleHour.idle)} idle minutes</p></div>
        <div className="stat-card"><span>Avg CPU</span><h2>{metrics.avgCPU}%</h2><p>Across all recorded activity</p></div>
        <div className="stat-card"><span>Avg Memory</span><h2>{metrics.avgMemory}%</h2><p>Across all recorded activity</p></div>
      </div>
      <div className="dashboard-grid">
        <div className="chart-card large"><div className="chart-header"><div><h3>Activity Trend</h3><p>Active and idle minutes by hour</p></div></div><div className="chart-container"><ResponsiveContainer width="100%" height="100%"><LineChart data={hourlyActivity}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="hour" tickFormatter={(hour) => `${hour}:00`} /><YAxis unit=" min" /><Tooltip labelFormatter={(hour) => `${hour}:00`} /><Legend /><Line type="monotone" dataKey="active" name="Active time" stroke="#2563eb" strokeWidth={3} dot={false} /><Line type="monotone" dataKey="idle" name="Idle time" stroke="#9ca3af" strokeWidth={2} dot={false} /></LineChart></ResponsiveContainer></div></div>
        <div className="chart-card"><div className="chart-header"><div><h3>Hourly Activity</h3><p>Active minutes at each hour</p></div></div><div className="chart-container"><ResponsiveContainer width="100%" height="100%"><BarChart data={hourlyActivity}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="hour" tickFormatter={(hour) => `${hour}:00`} /><YAxis /><Tooltip /><Bar dataKey="active" name="Active time" fill="#10b981" radius={[5, 5, 0, 0]} /></BarChart></ResponsiveContainer></div></div>
      </div>
      <div className="chart-card trends-resource-card"><div className="chart-header"><div><h3>Resource Trend</h3><p>Average CPU, memory, and battery percentage by hour</p></div></div><div className="chart-container"><ResponsiveContainer width="100%" height="100%"><LineChart data={resourceByHour}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="hour" tickFormatter={(hour) => `${hour}:00`} /><YAxis unit="%" /><Tooltip labelFormatter={(hour) => `${hour}:00`} /><Legend /><Line type="monotone" dataKey="cpu" stroke="#2563eb" name="CPU" dot={false} /><Line type="monotone" dataKey="memory" stroke="#8b5cf6" name="Memory" dot={false} /><Line type="monotone" dataKey="battery" stroke="#f59e0b" name="Battery" dot={false} /></LineChart></ResponsiveContainer></div></div>
    </DashboardLayout>
  );
}

export default Trends;
