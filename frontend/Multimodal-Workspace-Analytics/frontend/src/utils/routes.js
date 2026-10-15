// --------------------------------------------------
// Which pages show the single-user analytics
// --------------------------------------------------


/**
 * What the shared toolbar above a page shows: the live status pill (`live`)
 * and the time range picker (`range`). Team and Focus have their own data
 * and status; Settings shows the status but has nothing to filter.
 */
export function toolbarFor(pathname) {
  if (pathname.startsWith("/team") || pathname.startsWith("/focus")) {
    return { live: false, range: false };
  }

  return { live: true, range: !pathname.startsWith("/settings") };
}


/**
 * Pages that show no single-user analytics: there the analytics CSV only
 * feeds the navbar's anomaly alert, so it is refreshed far less often.
 */
export function isBackgroundRoute(pathname) {
  return !toolbarFor(pathname).live;
}
