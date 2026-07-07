"""
T224: read-only operational dashboard (founder direct instruction, 2026-07-07).
Auth: Bearer ADMIN_SECRET (mirrors admin.py). No mutation endpoints here.
"""
import logging

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.services.dashboard import get_dashboard_snapshot

logger = logging.getLogger("artimagehub.dashboard_api")
router = APIRouter()


def _require_admin(authorization: str | None) -> None:
    settings = get_settings()
    if not settings.admin_secret:
        raise HTTPException(status_code=404, detail="Not found")
    expected = f"Bearer {settings.admin_secret}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/admin/dashboard/data")
async def dashboard_data(
    days: int = Query(default=30, ge=1, le=365),
    granularity: str = Query(default="day"),
    authorization: str | None = Header(default=None),
):
    _require_admin(authorization)
    return get_dashboard_snapshot(days=days, granularity=granularity)


_DASHBOARD_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>artimagehub ops dashboard</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 960px; margin: 40px auto; padding: 0 16px; color: #1a1a1a; }
  h1 { font-size: 20px; }
  h2 { font-size: 15px; margin-top: 32px; color: #444; }
  table { border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 13px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #eee; }
  th { color: #888; font-weight: 600; }
  .stat-row { display: flex; gap: 24px; margin: 12px 0; }
  .stat { background: #f7f7f7; border-radius: 8px; padding: 12px 16px; min-width: 120px; }
  .stat .n { font-size: 22px; font-weight: 700; }
  .stat .l { font-size: 11px; color: #888; }
  .err { color: #b00; font-size: 13px; }
  .bad { color: #b00; font-weight: 600; }
  .ok { color: #2a7a2a; }
  input, select, button { font-size: 13px; padding: 4px 8px; margin-right: 8px; }
  #key { width: 260px; }
</style>
</head>
<body>
<h1>artimagehub ops dashboard</h1>
<div>
  <input id="key" type="password" placeholder="ADMIN_SECRET">
  <select id="days">
    <option value="7">7d</option>
    <option value="30" selected>30d</option>
    <option value="90">90d</option>
  </select>
  <select id="gran">
    <option value="day" selected>day</option>
    <option value="week">week</option>
    <option value="month">month</option>
  </select>
  <button onclick="load()">Load</button>
</div>
<div id="out">Enter ADMIN_SECRET and click Load.</div>

<script>
async function load() {
  const key = document.getElementById('key').value;
  const days = document.getElementById('days').value;
  const gran = document.getElementById('gran').value;
  const out = document.getElementById('out');
  out.innerHTML = 'Loading...';
  try {
    const res = await fetch(`/api/admin/dashboard/data?days=${days}&granularity=${gran}`, {
      headers: { 'Authorization': 'Bearer ' + key }
    });
    if (!res.ok) { out.innerHTML = `<div class="err">HTTP ${res.status}: ${await res.text()}</div>`; return; }
    const d = await res.json();
    render(d);
  } catch (e) {
    out.innerHTML = `<div class="err">${e}</div>`;
  }
}

function render(d) {
  const out = document.getElementById('out');
  let html = `<div style="color:#888;font-size:11px">generated_at ${d.generated_at}</div>`;

  const o = d.orders;
  html += '<h2>Orders (Dodo live truth)</h2>';
  if (o.error) {
    html += `<div class="err">${o.error}</div>`;
  } else {
    html += `<div class="stat-row">
      <div class="stat"><div class="n">${o.totals.orders}</div><div class="l">orders (${o.days}d)</div></div>
      <div class="stat"><div class="n">$${o.totals.revenue_usd}</div><div class="l">revenue</div></div>
      <div class="stat"><div class="n">${o.excluded_self_test}</div><div class="l">self-test excluded</div></div>
      <div class="stat"><div class="n">${o.excluded_other_product}</div><div class="l">other-product excluded</div></div>
    </div>`;
    html += '<table><tr><th>period</th><th>orders</th><th>revenue</th></tr>';
    for (const row of o.series) {
      html += `<tr><td>${row.period}</td><td>${row.orders}</td><td>$${row.revenue_usd}</td></tr>`;
    }
    html += '</table>';
  }

  const c = d.customers;
  html += '<h2>Customers</h2>';
  if (c.error) {
    html += `<div class="err">${c.error}</div>`;
  } else {
    html += `<div class="stat-row">
      <div class="stat"><div class="n">${c.unique_customers}</div><div class="l">unique customers</div></div>
      <div class="stat"><div class="n">${c.repeat_customers}</div><div class="l">repeat customers</div></div>
      <div class="stat"><div class="n">${(c.repeat_rate*100).toFixed(1)}%</div><div class="l">repeat rate</div></div>
    </div>`;
  }

  const th = d.task_health;
  html += '<h2>Task health (all tasks incl. self-test, system-reliability signal)</h2>';
  if (th.error) {
    html += `<div class="err">${th.error}</div>`;
  } else {
    html += '<table><tr><th>feature</th><th>total</th><th>completed</th><th>failed</th><th>success rate</th><th>fallback rate</th></tr>';
    for (const [fk, s] of Object.entries(th.features)) {
      const sr = s.success_rate;
      const srClass = sr === null ? '' : (sr < 0.7 ? 'bad' : 'ok');
      html += `<tr><td>${fk}</td><td>${s.total}</td><td>${s.completed}</td><td>${s.failed}</td>` +
        `<td class="${srClass}">${sr === null ? '—' : (sr*100).toFixed(1)+'%'}</td>` +
        `<td>${s.fallback_rate === null ? '—' : (s.fallback_rate*100).toFixed(1)+'%'}</td></tr>`;
    }
    html += '</table>';
  }

  out.innerHTML = html;
}
</script>
</body>
</html>"""


@router.get("/admin/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    """Static shell -- fetches /admin/dashboard/data client-side with the
    ADMIN_SECRET typed into the page, never baked into the HTML itself."""
    return _DASHBOARD_HTML
