import Papa from "papaparse";
const ANALYTICS_API = "/api/analytics.csv";


function toNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}


function toBoolean(value) {
  if (typeof value === "boolean") {
    return value;
  }

  const normalized = String(value).toLowerCase().trim();

  return (
    normalized === "true" ||
    normalized === "1" ||
    normalized === "yes"
  );
}


function round(value, decimals = 2) {
  const factor = 10 ** decimals;
  return Math.round(value * factor) / factor;
}


export async function loadAnalyticsData() {

  const response = await fetch(`${ANALYTICS_API}?t=${Date.now()}`, {
    cache: "no-store",
  });

  if (!response.ok) {
    throw new Error("Unable to load CSV dataset");
  }

  const csvText = await response.text();

  return new Promise((resolve, reject) => {

    Papa.parse(csvText, {

      header: true,

      skipEmptyLines: true,

      complete: (results) => {

        try {

          const rows = results.data.map((row) => ({

            ...row,

            idle_seconds: toNumber(row.idle_seconds),

            cpu_percent: toNumber(row.cpu_percent),

            memory_percent: toNumber(row.memory_percent),

            battery_percent: toNumber(row.battery_percent),

            duration_seconds: toNumber(row.duration_seconds),

            active_duration_seconds:
              toNumber(row.active_duration_seconds),

            idle_duration_seconds:
              toNumber(row.idle_duration_seconds),

            session_id: String(row.session_id || ""),

            application_switch:
              toBoolean(row.application_switch),

            window_switch:
              toBoolean(row.window_switch),

            idle_ratio:
              toNumber(row.idle_ratio),

            switch_frequency:
              toNumber(row.switch_frequency),

            unique_app_count:
              toNumber(row.unique_app_count),

            avg_cpu:
              toNumber(row.avg_cpu),

            avg_memory:
              toNumber(row.avg_memory),

            hour:
              toNumber(row.hour),

            cluster_id:
              toNumber(row.cluster_id),

            anomaly_score:
              toNumber(row.anomaly_score),

            is_anomaly:
              toBoolean(row.is_anomaly),

            behavior_label:
              row.behavior_label || "Unknown",

            application:
              row.application || "Unknown",

            activity_status:
              row.activity_status || "Unknown",

            timestamp:
              row.timestamp || ""

          }));


          // =========================
          // BASIC METRICS
          // =========================

          const totalActiveSeconds = rows.reduce(
            (sum, row) => sum + row.active_duration_seconds,
            0
          );


          const totalIdleSeconds = rows.reduce(
            (sum, row) => sum + row.idle_duration_seconds,
            0
          );


          const sessions = new Set(
            rows
              .map((row) => row.session_id)
              .filter(Boolean)
          );


          const anomalyCount = rows.filter(
            (row) => row.is_anomaly
          ).length;


          // =========================
          // FOCUS SCORE
          // =========================
          // Derived from active vs idle behavior.
          // This is NOT directly stored in the CSV.

          const totalTime =
            totalActiveSeconds + totalIdleSeconds;

          const focusScore =
            totalTime > 0
              ? (totalActiveSeconds / totalTime) * 100
              : 0;


          // =========================
          // APPLICATION USAGE
          // =========================

          const applicationMap = {};

          rows.forEach((row) => {

            const app = row.application;

            if (!applicationMap[app]) {
              applicationMap[app] = {
                application: app,
                duration: 0,
                records: 0,
                switches: 0
              };
            }

            applicationMap[app].duration +=
              row.active_duration_seconds;

            applicationMap[app].records += 1;

            if (row.application_switch) {
              applicationMap[app].switches += 1;
            }

          });


          const applicationUsage = Object.values(
            applicationMap
          )
            .map((item) => ({
              ...item,
              duration: round(item.duration)
            }))
            .sort(
              (a, b) => b.duration - a.duration
            );


          // =========================
          // BEHAVIOR DISTRIBUTION
          // =========================

          const behaviorMap = {};

          rows.forEach((row) => {

            const behavior = row.behavior_label;

            if (!behaviorMap[behavior]) {
              behaviorMap[behavior] = 0;
            }

            behaviorMap[behavior]++;

          });


          const behaviorDistribution =
            Object.entries(behaviorMap)
              .map(([behavior, count]) => ({
                behavior,
                count
              }))
              .sort(
                (a, b) => b.count - a.count
              );


          // =========================
          // HOURLY ACTIVITY
          // =========================

          const hourlyMap = {};

          rows.forEach((row) => {

            const hour = row.hour;

            if (!hourlyMap[hour]) {
              hourlyMap[hour] = {
                hour,
                active: 0,
                idle: 0
              };
            }

            hourlyMap[hour].active +=
              row.active_duration_seconds;

            hourlyMap[hour].idle +=
              row.idle_duration_seconds;

          });


          const hourlyActivity =
            Object.values(hourlyMap)
              .sort((a, b) => a.hour - b.hour)
              .map((item) => ({
                hour: item.hour,
                active: round(item.active / 60),
                idle: round(item.idle / 60)
              }));


          // =========================
          // RESOURCE USAGE
          // =========================

          const avgCPU =
            rows.length > 0
              ? rows.reduce(
                  (sum, row) => sum + row.cpu_percent,
                  0
                ) / rows.length
              : 0;


          const avgMemory =
            rows.length > 0
              ? rows.reduce(
                  (sum, row) => sum + row.memory_percent,
                  0
                ) / rows.length
              : 0;


          const avgBattery =
            rows.length > 0
              ? rows.reduce(
                  (sum, row) => sum + row.battery_percent,
                  0
                ) / rows.length
              : 0;


          // =========================
          // SESSION ANALYTICS
          // =========================

          const sessionMap = {};

          rows.forEach((row) => {

            const id = row.session_id;

            if (!id) {
              return;
            }

            if (!sessionMap[id]) {
              sessionMap[id] = {
                session_id: id,
                duration: 0,
                active: 0,
                idle: 0,
                applications: 0,
                switches: 0,
                switchFrequency: 0,
                idleRatio: 0,
                cpu: 0,
                memory: 0,
                cluster: row.cluster_id,
                behavior: row.behavior_label,
                records: 0
              };
            }

            sessionMap[id].duration += row.duration_seconds;

            sessionMap[id].active +=
              row.active_duration_seconds;

            sessionMap[id].idle +=
              row.idle_duration_seconds;

            sessionMap[id].applications =
              Math.max(
                sessionMap[id].applications,
                row.unique_app_count
              );

            sessionMap[id].switches +=
              row.application_switch ? 1 : 0;

            sessionMap[id].switchFrequency +=
              row.switch_frequency;

            sessionMap[id].cpu += row.cpu_percent;

            sessionMap[id].memory += row.memory_percent;

            sessionMap[id].records += 1;

          });


          const sessionAnalytics =
            Object.values(sessionMap)
              .map((session) => ({

                session_id:
                  session.session_id,

                duration:
                  session.duration,

                active:
                  session.active,

                idle:
                  session.idle,

                applications:
                  session.applications,

                switches:
                  session.switches,

                switchFrequency:
                  session.records > 0
                    ? round(
                        session.switchFrequency /
                        session.records
                      )
                    : 0,

                idleRatio:
                  session.active + session.idle > 0
                    ? round(
                        session.idle /
                        (session.active + session.idle) *
                        100
                      )
                    : 0,

                avgCPU:
                  session.records > 0
                    ? round(
                        session.cpu /
                        session.records
                      )
                    : 0,

                avgMemory:
                  session.records > 0
                    ? round(
                        session.memory /
                        session.records
                      )
                    : 0,

                cluster:
                  session.cluster,

                behavior:
                  session.behavior

              }))
              .sort(
                (a, b) =>
                  b.duration - a.duration
              );


          // =========================
          // BEHAVIOR ANALYTICS
          // =========================

          const behaviorMapDetailed = {};

          rows.forEach((row) => {

            const behavior = row.behavior_label || "Unknown";

            if (!behaviorMapDetailed[behavior]) {

              behaviorMapDetailed[behavior] = {
                behavior,
                records: 0,
                active: 0,
                idle: 0,
                switches: 0,
                applications: 0,
                idleRatio: 0
              };

            }

            const item = behaviorMapDetailed[behavior];

            item.records += 1;

            item.active +=
              row.active_duration_seconds;

            item.idle +=
              row.idle_duration_seconds;

            item.switches +=
              row.application_switch ? 1 : 0;

            item.applications +=
              row.unique_app_count;

            item.idleRatio +=
              row.idle_ratio;

          });


          const behaviorAnalytics =
            Object.values(behaviorMapDetailed)
              .map((item) => ({

                behavior: item.behavior,

                records: item.records,

                active: item.active,

                idle: item.idle,

                switches: item.switches,

                avgApplications:
                  item.records > 0
                    ? round(
                        item.applications /
                        item.records
                      )
                    : 0,

                avgIdleRatio:
                  item.records > 0
                    ? round(
                        item.idleRatio /
                        item.records
                      )
                    : 0

              }))
              .sort(
                (a, b) =>
                  b.records - a.records
              );


          // =========================
          // RESOURCE USAGE BY HOUR
          // =========================

          const resourceMap = {};

          rows.forEach((row) => {

            const hour = row.hour;

            if (!resourceMap[hour]) {
              resourceMap[hour] = {
                hour,
                cpu: 0,
                memory: 0,
                battery: 0,
                count: 0
              };
            }

            resourceMap[hour].cpu += row.cpu_percent;
            resourceMap[hour].memory += row.memory_percent;
            resourceMap[hour].battery += row.battery_percent;
            resourceMap[hour].count += 1;

          });


          const resourceByHour =
            Object.values(resourceMap)
              .sort((a, b) => a.hour - b.hour)
              .map((item) => ({
                hour: item.hour,
                cpu: round(item.cpu / item.count),
                memory: round(item.memory / item.count),
                battery: round(item.battery / item.count)
              }));


          // =========================
          // SWITCHING
          // =========================

          const applicationSwitches =
            rows.filter(
              (row) => row.application_switch
            ).length;


          const windowSwitches =
            rows.filter(
              (row) => row.window_switch
            ).length;


          // =========================
          // ANOMALIES
          // =========================

          const anomalyRows =
            rows
              .filter((row) => row.is_anomaly)
              .sort(
                (a, b) =>
                  b.anomaly_score -
                  a.anomaly_score
              );


          // =========================
          // RESULT
          // =========================

          resolve({

            rows,

            metrics: {

              totalRecords: rows.length,

              totalSessions: sessions.size,

              totalActiveSeconds,

              totalIdleSeconds,

              focusScore: round(focusScore),

              anomalyCount,

              applicationSwitches,

              windowSwitches,

              avgCPU: round(avgCPU),

              avgMemory: round(avgMemory),

              avgBattery: round(avgBattery)

            },

            applicationUsage,

            behaviorDistribution,

            hourlyActivity,

            resourceByHour,

            sessionAnalytics,

            behaviorAnalytics,

            anomalies: anomalyRows

          });

        } catch (error) {

          reject(error);

        }

      },

      error: (error) => {
        reject(error);
      }

    });

  });

}
