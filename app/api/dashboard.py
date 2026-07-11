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


# T228 (2026-07-08): tables -> line charts (founder feedback: too much table,
# wants curves). Hand-rolled SVG, no chart-library dependency -- this is an
# internal admin tool with a handful of small daily series, a library would
# be pure overhead. Palette/marks/interaction follow the dataviz skill:
# 2px lines, r>=4 end dots with a 2px surface ring, hairline solid gridlines,
# legend only for >=2 series, single hover crosshair+tooltip reading every
# series at that x, tables kept below every chart as the accessible twin.
# Different-scale measures (orders vs revenue; sessions vs the two small
# funnel counts) are never combined on one dual-axis chart -- split into
# separate single-axis charts instead.
_DASHBOARD_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>artimagehub ops dashboard</title>
<style>
  :root {
    --surface-1:      #fcfcfb;
    --page:           #f9f9f7;
    --text-primary:   #0b0b0b;
    --text-secondary: #52514e;
    --text-muted:     #898781;
    --grid:           #e1e0d9;
    --axis:           #c3c2b7;
    --border:         rgba(11,11,11,0.10);
    --series-1:       #2a78d6; /* blue */
    --series-2:       #1baf7a; /* aqua */
    --series-3:       #eda100; /* yellow */
    --series-4:       #008300; /* green */
    --series-5:       #4a3aa7; /* violet */
    --series-6:       #e34948; /* red */
    --series-7:       #e87ba4; /* magenta */
    --series-8:       #eb6834; /* orange */
    --series-other:   #898781; /* neutral gray, not a categorical identity */
    --good:           #006300;
    --bad:            #d03b3b;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --surface-1:      #1a1a19;
      --page:           #0d0d0d;
      --text-primary:   #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted:     #898781;
      --grid:           #2c2c2a;
      --axis:           #383835;
      --border:         rgba(255,255,255,0.10);
      --series-1:       #3987e5;
      --series-2:       #199e70;
      --series-3:       #c98500;
      --series-4:       #008300;
      --series-5:       #9085e9;
      --series-6:       #e66767;
      --series-7:       #d55181;
      --series-8:       #d95926;
      --series-other:   #898781;
      --good:           #0ca30c;
      --bad:            #e66767;
    }
  }
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; max-width: 960px; margin: 40px auto; padding: 0 16px;
         color: var(--text-primary); background: var(--page); }
  h1 { font-size: 20px; }
  h2 { font-size: 15px; margin-top: 32px; color: var(--text-secondary); }
  table { border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 13px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); font-variant-numeric: tabular-nums; }
  th { color: var(--text-muted); font-weight: 600; font-variant-numeric: normal; }
  details.tbl { margin-top: 6px; }
  details.tbl summary { font-size: 12px; color: var(--text-muted); cursor: pointer; }
  .stat-row { display: flex; gap: 24px; margin: 12px 0; flex-wrap: wrap; }
  .stat { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; min-width: 120px; }
  .stat .n { font-size: 22px; font-weight: 700; }
  .stat .l { font-size: 11px; color: var(--text-muted); }
  .err { color: var(--bad); font-size: 13px; }
  .bad { color: var(--bad); font-weight: 600; }
  .ok { color: var(--good); }
  .note { font-size: 11px; color: var(--text-muted); margin-top: 6px; line-height: 1.5; }
  .charts-row { display: flex; gap: 16px; flex-wrap: wrap; margin-top: 8px; }
  .chart-card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px 8px; flex: 1 1 380px; min-width: 300px; }
  .chart-title { font-size: 12px; color: var(--text-secondary); font-weight: 600; margin-bottom: 4px; }
  .legend { display: flex; gap: 14px; font-size: 11px; color: var(--text-secondary); margin-bottom: 4px; }
  .legend-item { display: flex; align-items: center; gap: 5px; }
  .legend-key { width: 12px; height: 2px; display: inline-block; }
  .chart-svg { width: 100%; height: auto; display: block; overflow: visible; }
  .chart-tooltip { position: absolute; background: var(--surface-1); border: 1px solid var(--border); border-radius: 6px;
                   padding: 6px 8px; font-size: 11px; pointer-events: none; box-shadow: 0 2px 8px rgba(0,0,0,0.12); white-space: nowrap; }
  .chart-tooltip .row { display: flex; align-items: center; gap: 6px; }
  .chart-tooltip .key { width: 8px; height: 2px; display: inline-block; }
  .chart-tooltip .val { font-weight: 700; font-variant-numeric: tabular-nums; }
  .chart-tooltip .lbl { color: var(--text-secondary); }
  .chart-wrap { position: relative; }
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

// ---- minimal SVG line-chart renderer (no dependency) ----------------------
// series: [{ name, color, points: [{x: <label string>, y: <number>}] }]
function lineChartSVG(series, opts) {
  const w = opts.w || 560, h = opts.h || 160;
  const padL = 34, padR = 12, padT = 10, padB = 22;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  const allY = series.flatMap(s => s.points.map(p => p.y));
  const yMax = Math.max(1, ...allY);
  const n = series[0] ? series[0].points.length : 0;
  const xAt = i => padL + (n <= 1 ? 0 : (plotW * i) / (n - 1));
  const yAt = v => padT + plotH - (plotH * v) / yMax;

  // clean y ticks: 0, mid, max -- niceMax is always even (or a multiple of a
  // clean unit) so the midpoint is never an ugly value like 1.5
  const niceMax = (() => {
    if (yMax <= 5) {
      let m = Math.ceil(yMax);
      if (m % 2 !== 0) m += 1;
      return Math.max(m, 2);
    }
    const mag = Math.pow(10, Math.floor(Math.log10(yMax)));
    let m = Math.ceil(yMax / mag) * mag;
    if ((m / mag) % 2 !== 0) m += mag;
    return m;
  })();
  const ticks = [0, niceMax / 2, niceMax];

  let svg = `<svg class="chart-svg" viewBox="0 0 ${w} ${h}" data-w="${w}" data-h="${h}" data-padl="${padL}" data-padt="${padT}" data-plotw="${plotW}" data-ploth="${plotH}" data-ymax="${niceMax}" data-n="${n}">`;

  // gridlines + y labels
  for (const t of ticks) {
    const y = yAt(t);
    svg += `<line x1="${padL}" y1="${y}" x2="${w-padR}" y2="${y}" stroke="var(--grid)" stroke-width="1"/>`;
    svg += `<text x="${padL-6}" y="${y+3}" font-size="9" fill="var(--text-muted)" text-anchor="end">${fmtNum(t)}</text>`;
  }
  // baseline
  svg += `<line x1="${padL}" y1="${padT+plotH}" x2="${w-padR}" y2="${padT+plotH}" stroke="var(--axis)" stroke-width="1"/>`;

  // x labels: first, middle, last only (avoid clutter)
  if (n > 0) {
    const idxs = n === 1 ? [0] : [0, Math.floor((n-1)/2), n-1];
    for (const i of new Set(idxs)) {
      svg += `<text x="${xAt(i)}" y="${h-4}" font-size="9" fill="var(--text-muted)" text-anchor="${i===0?'start':i===n-1?'end':'middle'}">${series[0].points[i].x}</text>`;
    }
  }

  // lines + end dots + end value label. Past ~4 series, direct end-labels
  // collide and read as noise (dataviz guidance) -- rely on the legend +
  // tooltip + detail table instead for those, still keep the dot.
  const skipDirectLabels = series.length > 4;
  series.forEach((s, si) => {
    if (s.points.length === 0) return;
    const d = s.points.map((p, i) => `${i===0?'M':'L'}${xAt(i).toFixed(1)},${yAt(p.y).toFixed(1)}`).join(' ');
    svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    const last = s.points[s.points.length - 1];
    const lx = xAt(s.points.length - 1), ly = yAt(last.y);
    svg += `<circle cx="${lx}" cy="${ly}" r="4" fill="${s.color}" stroke="var(--surface-1)" stroke-width="2"/>`;
    if (!skipDirectLabels) {
      // stagger end labels vertically a touch if multiple series to reduce collision
      const dy = series.length > 1 ? (si - (series.length-1)/2) * 11 : 0;
      const anchor = lx > w - padR - 26 ? 'end' : 'start';
      const lxLabel = anchor === 'end' ? lx - 6 : lx + 6;
      svg += `<text x="${lxLabel}" y="${ly + 3 + dy}" font-size="10" font-weight="700" fill="var(--text-primary)" text-anchor="${anchor}">${fmtNum(last.y)}</text>`;
    }
  });

  // hover hit layer (crosshair, added by attachHover)
  svg += `<line class="crosshair" x1="0" y1="${padT}" x2="0" y2="${padT+plotH}" stroke="var(--axis)" stroke-width="1" style="display:none"/>`;
  svg += `<rect class="hit-rect" x="${padL}" y="${padT}" width="${plotW}" height="${plotH}" fill="transparent"/>`;
  svg += `</svg>`;
  return svg;
}

function fmtNum(v) {
  if (Math.abs(v) >= 1000) return (v/1000).toFixed(v % 1000 === 0 ? 0 : 1) + 'K';
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

function renderChartCard(container, title, series, valueFmt) {
  const card = document.createElement('div');
  card.className = 'chart-card';
  let html = `<div class="chart-title">${escapeHtml(title)}</div>`;
  if (series.length > 1) {
    html += '<div class="legend">' + series.map(s =>
      `<span class="legend-item"><span class="legend-key" style="background:${s.color}"></span>${escapeHtml(s.name)}</span>`
    ).join('') + '</div>';
  }
  const n = series[0] ? series[0].points.length : 0;
  if (n === 0) {
    card.innerHTML = html + '<div class="note">no data in this window</div>';
    container.appendChild(card);
    return;
  }
  html += `<div class="chart-wrap">${lineChartSVG(series, {})}</div>`;
  card.innerHTML = html;
  container.appendChild(card);
  attachHover(card, series, valueFmt);
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function attachHover(card, series, valueFmt) {
  const svg = card.querySelector('svg');
  const hitRect = svg.querySelector('.hit-rect');
  const crosshair = svg.querySelector('.crosshair');
  const padL = parseFloat(svg.dataset.padl), padT = parseFloat(svg.dataset.padt);
  const plotW = parseFloat(svg.dataset.plotw), plotH = parseFloat(svg.dataset.ploth);
  const n = parseInt(svg.dataset.n, 10);
  let tip = card.querySelector('.chart-tooltip');
  if (!tip) {
    tip = document.createElement('div');
    tip.className = 'chart-tooltip';
    tip.style.display = 'none';
    card.style.position = 'relative';
    card.appendChild(tip);
  }

  function handle(evt) {
    const rect = svg.getBoundingClientRect();
    const scaleX = rect.width / parseFloat(svg.getAttribute('viewBox').split(' ')[2]);
    const localX = (evt.clientX - rect.left) / scaleX;
    if (n <= 1) return;
    let idx = Math.round(((localX - padL) / plotW) * (n - 1));
    idx = Math.max(0, Math.min(n - 1, idx));
    const cx = padL + (plotW * idx) / (n - 1);
    crosshair.setAttribute('x1', cx); crosshair.setAttribute('x2', cx);
    crosshair.style.display = '';

    const label = series[0].points[idx].x;
    let html = `<div style="font-weight:700;margin-bottom:3px">${escapeHtml(label)}</div>`;
    for (const s of series) {
      const v = s.points[idx] ? s.points[idx].y : null;
      html += `<div class="row"><span class="key" style="background:${s.color}"></span>` +
        `<span class="lbl">${escapeHtml(s.name)}</span><span class="val">${v === null ? '—' : (valueFmt ? valueFmt(v) : v)}</span></div>`;
    }
    tip.innerHTML = html;
    tip.style.display = '';
    const tipX = Math.min(rect.width - 140, Math.max(0, (cx / parseFloat(svg.getAttribute('viewBox').split(' ')[2])) * rect.width + 10));
    tip.style.left = tipX + 'px';
    tip.style.top = '6px';
  }
  hitRect.addEventListener('pointermove', handle);
  hitRect.addEventListener('pointerenter', handle);
  hitRect.addEventListener('pointerleave', () => { tip.style.display = 'none'; crosshair.style.display = 'none'; });
}

// T238: fixed channel -> color-slot lookup, one UNIQUE slot per name (no
// modulo wraparound) so two channels that co-occur (at most CHANNEL_MAX_SERIES
// = 6 at once) never collide on the same hue -- color follows entity, not
// rank. Covers the 8 channel groups most likely to actually appear for this
// site; anything else falls back to a cycling assignment (rare, since at
// most 6 real channels are ever shown together and these 8 already cover
// everything observed in production).
const CHANNEL_COLOR_ORDER = [
  'Organic Search', 'Direct', 'AI Assistant', 'Organic Social',
  'Referral', 'Paid Search', 'Unassigned', 'Cross-network',
];
function channelColor(name) {
  const idx = CHANNEL_COLOR_ORDER.indexOf(name);
  const slot = (idx === -1 ? name.length : idx) % 8 + 1;
  return `var(--series-${slot})`;
}

function shortDate(s) {
  // "2026-07-04" -> "07-04"; leave week/month periods as-is
  const m = /^\\d{4}-(\\d{2}-\\d{2})$/.exec(s);
  return m ? m[1] : s;
}

function render(d) {
  const out = document.getElementById('out');
  out.innerHTML = '';
  const gen = document.createElement('div');
  gen.style.cssText = 'color:var(--text-muted);font-size:11px';
  gen.textContent = `generated_at ${d.generated_at}`;
  out.appendChild(gen);

  const f = d.funnel;
  appendH2(out, 'Daily top-of-funnel');
  if (f.error) {
    appendErr(out, f.error);
  } else {
    appendStatRow(out, [
      [f.totals.sessions_external, `sessions (external, ${f.days}d)`],
      [f.totals.payment_attempts, 'payment attempts'],
      [f.totals.funnel_start_users_external, '"registration" proxy'],
    ]);
    if (f.errors && (f.errors.ga4 || f.errors.dodo)) {
      appendErr(out, [f.errors.ga4 && ('GA4: '+f.errors.ga4), f.errors.dodo && ('Dodo: '+f.errors.dodo)].filter(Boolean).join(' | '));
    }
    const chartsRow = document.createElement('div');
    chartsRow.className = 'charts-row';
    out.appendChild(chartsRow);
    renderChartCard(chartsRow, 'External sessions & unique visitors', [
      { name: 'sessions', color: 'var(--series-1)', points: f.series.map(r => ({ x: shortDate(r.date), y: r.sessions_external })) },
      { name: 'unique visitors (UV)', color: 'var(--series-2)', points: f.series.map(r => ({ x: shortDate(r.date), y: r.users_external })) },
    ]);
    renderChartCard(chartsRow, 'Payment attempts vs "registration" proxy', [
      { name: 'payment attempts', color: 'var(--series-1)', points: f.series.map(r => ({ x: shortDate(r.date), y: r.payment_attempts })) },
      { name: '"registration" proxy', color: 'var(--series-2)', points: f.series.map(r => ({ x: shortDate(r.date), y: r.funnel_start_users_external })) },
    ]);
    const tbl = document.createElement('details');
    tbl.className = 'tbl';
    tbl.innerHTML = '<summary>daily detail table</summary>';
    const table = document.createElement('table');
    table.innerHTML = '<tr><th>date</th><th>sessions (ext)</th><th>users (ext)</th><th>payment attempts</th><th>"registration" proxy</th></tr>' +
      f.series.map(row => `<tr><td>${row.date}</td><td>${row.sessions_external}</td><td>${row.users_external}</td>` +
        `<td>${row.payment_attempts}</td><td>${row.funnel_start_users_external}</td></tr>`).join('');
    tbl.appendChild(table);
    out.appendChild(tbl);
    const note = document.createElement('div');
    note.className = 'note';
    note.innerHTML = `traffic: ${escapeHtml(f.notes.traffic_filter)}<br>` +
      `payment attempts: ${escapeHtml(f.notes.payment_attempts_definition)}<br>` +
      `"registration": ${escapeHtml(f.notes.registration_caveat)}`;
    out.appendChild(note);
  }

  const ch = d.channels;
  appendH2(out, 'Traffic channel mix (external, daily)');
  if (ch.error) {
    appendErr(out, ch.error);
  } else if (!ch.channels || ch.channels.length === 0) {
    appendErr(out, 'no channel data in this window');
  } else {
    const chartsRow2 = document.createElement('div');
    chartsRow2.className = 'charts-row';
    out.appendChild(chartsRow2);
    const channelSeries = ch.channels.map(name => ({
      name,
      color: name === 'Other' ? 'var(--series-other)' : channelColor(name),
      points: ch.series.map(r => ({ x: shortDate(r.date), y: r[name] || 0 })),
    }));
    renderChartCard(chartsRow2, 'Sessions by channel group', channelSeries);
    const tbl2 = document.createElement('details');
    tbl2.className = 'tbl';
    tbl2.innerHTML = '<summary>daily detail table</summary>';
    const table2 = document.createElement('table');
    table2.innerHTML = '<tr><th>date</th>' + ch.channels.map(c => `<th>${escapeHtml(c)}</th>`).join('') + '</tr>' +
      ch.series.map(row => `<tr><td>${row.date}</td>` + ch.channels.map(c => `<td>${row[c] || 0}</td>`).join('') + '</tr>').join('');
    tbl2.appendChild(table2);
    out.appendChild(tbl2);
    const note2 = document.createElement('div');
    note2.className = 'note';
    let noteText = escapeHtml(ch.notes.channel_dimension);
    if (ch.notes.folded_into_other && ch.notes.folded_into_other.length > 0) {
      noteText += `<br>folded into "Other": ${escapeHtml(ch.notes.folded_into_other.join(', '))}`;
    }
    note2.innerHTML = noteText;
    out.appendChild(note2);
  }

  const bq = d.bing_ctr;
  appendH2(out, 'Bing query CTR (keyword-mining input)');
  if (bq.error) {
    appendErr(out, bq.error);
  } else if (!bq.rows || bq.rows.length === 0) {
    appendErr(out, 'no query data');
  } else {
    appendStatRow(out, [
      [bq.totals.queries, 'queries'],
      [bq.totals.impressions, 'impressions'],
      [bq.totals.clicks, 'clicks'],
      [bq.totals.zero_click_queries, 'zero-click queries'],
    ]);
    const bqTbl = document.createElement('table');
    bqTbl.innerHTML = '<tr><th>query</th><th>impressions</th><th>clicks</th><th>CTR</th></tr>' +
      bq.rows.map(r => {
        const ctrClass = r.clicks === 0 && r.impressions > 0 ? 'bad' : '';
        return `<tr><td>${escapeHtml(r.query)}</td><td>${r.impressions}</td><td>${r.clicks}</td>` +
          `<td class="${ctrClass}">${(r.ctr*100).toFixed(1)}%</td></tr>`;
      }).join('');
    out.appendChild(bqTbl);
    const bqNote = document.createElement('div');
    bqNote.className = 'note';
    let bqNoteText = `${escapeHtml(bq.notes.source)}<br>window: ${bq.window.observed_dates.join(', ') || 'n/a'} — ${escapeHtml(bq.notes.window_caveat)}`;
    if (bq.notes.truncated_to) {
      bqNoteText += `<br>showing top ${bq.notes.truncated_to} of ${bq.totals.queries} queries by impressions`;
    }
    bqNote.innerHTML = bqNoteText;
    out.appendChild(bqNote);
  }

  const o = d.orders;
  appendH2(out, 'Orders (Dodo live truth)');
  if (o.error) {
    appendErr(out, o.error);
  } else {
    appendStatRow(out, [
      [o.totals.orders, `orders (${o.days}d)`],
      ['$' + o.totals.revenue_usd, 'revenue'],
      [o.excluded_self_test, 'self-test excluded'],
      [o.excluded_other_product, 'other-product excluded'],
    ]);
    const chartsRow = document.createElement('div');
    chartsRow.className = 'charts-row';
    out.appendChild(chartsRow);
    renderChartCard(chartsRow, 'Orders', [
      { name: 'orders', color: 'var(--series-1)', points: o.series.map(r => ({ x: shortDate(r.period), y: r.orders })) },
    ]);
    renderChartCard(chartsRow, 'Revenue (USD)', [
      { name: 'revenue', color: 'var(--series-2)', points: o.series.map(r => ({ x: shortDate(r.period), y: r.revenue_usd })) },
    ], v => '$' + v.toFixed(2));
    const tbl = document.createElement('details');
    tbl.className = 'tbl';
    tbl.innerHTML = '<summary>detail table</summary>';
    const table = document.createElement('table');
    table.innerHTML = '<tr><th>period</th><th>orders</th><th>revenue</th></tr>' +
      o.series.map(row => `<tr><td>${row.period}</td><td>${row.orders}</td><td>$${row.revenue_usd}</td></tr>`).join('');
    tbl.appendChild(table);
    out.appendChild(tbl);

    // T231 (founder direct instruction, 2026-07-09): per-order channel
    // attribution so a specific order can be traced to a channel without a
    // manual DB query. Older orders that predate the attribution columns
    // show "no attribution data" rather than blank cells or an error.
    if (o.recent_orders) {
      const ord = document.createElement('details');
      ord.className = 'tbl';
      ord.open = true;
      ord.innerHTML = `<summary>recent orders — channel attribution (${o.recent_orders.length})</summary>`;
      const ordTable = document.createElement('table');
      const rowsHtml = o.recent_orders.map(row => {
        if (!row.attribution_available) {
          return `<tr><td>${escapeHtml(row.created_at)}</td><td>${escapeHtml(row.email)}</td>` +
            `<td>$${row.revenue_usd}</td><td colspan="4" class="note" style="padding:6px 10px">no attribution data</td></tr>`;
        }
        return `<tr><td>${escapeHtml(row.created_at)}</td><td>${escapeHtml(row.email)}</td><td>$${row.revenue_usd}</td>` +
          `<td>${escapeHtml(row.landing_page || '—')}</td><td>${escapeHtml(row.cta_slot || '—')}</td>` +
          `<td>${escapeHtml(row.entry_variant || '—')}</td><td>${escapeHtml(row.checkout_source || '—')}</td></tr>`;
      }).join('');
      ordTable.innerHTML = '<tr><th>time</th><th>email</th><th>revenue</th><th>landing_page</th><th>cta_slot</th><th>entry_variant</th><th>checkout_source</th></tr>' + rowsHtml;
      ord.appendChild(ordTable);
      out.appendChild(ord);
    }
  }

  const c = d.customers;
  appendH2(out, 'Customers');
  if (c.error) {
    appendErr(out, c.error);
  } else {
    appendStatRow(out, [
      [c.unique_customers, 'unique customers'],
      [c.repeat_customers, 'repeat customers'],
      [(c.repeat_rate*100).toFixed(1) + '%', 'repeat rate'],
    ]);
  }

  const th = d.task_health;
  appendH2(out, 'Task health (all tasks incl. self-test, system-reliability signal)');
  if (th.error) {
    appendErr(out, th.error);
  } else {
    const table = document.createElement('table');
    table.innerHTML = '<tr><th>feature</th><th>total</th><th>completed</th><th>failed</th><th>success rate</th><th>fallback rate</th></tr>' +
      Object.entries(th.features).map(([fk, s]) => {
        const sr = s.success_rate;
        const srClass = sr === null ? '' : (sr < 0.7 ? 'bad' : 'ok');
        return `<tr><td>${fk}</td><td>${s.total}</td><td>${s.completed}</td><td>${s.failed}</td>` +
          `<td class="${srClass}">${sr === null ? '—' : (sr*100).toFixed(1)+'%'}</td>` +
          `<td>${s.fallback_rate === null ? '—' : (s.fallback_rate*100).toFixed(1)+'%'}</td></tr>`;
      }).join('');
    out.appendChild(table);
  }
}

function appendH2(out, text) {
  const h = document.createElement('h2');
  h.textContent = text;
  out.appendChild(h);
}
function appendErr(out, text) {
  const e = document.createElement('div');
  e.className = 'err';
  e.textContent = text;
  out.appendChild(e);
}
function appendStatRow(out, pairs) {
  const row = document.createElement('div');
  row.className = 'stat-row';
  row.innerHTML = pairs.map(([n, l]) => `<div class="stat"><div class="n">${n}</div><div class="l">${escapeHtml(l)}</div></div>`).join('');
  out.appendChild(row);
}
</script>
</body>
</html>"""


@router.get("/admin/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    """Static shell -- fetches /admin/dashboard/data client-side with the
    ADMIN_SECRET typed into the page, never baked into the HTML itself."""
    return _DASHBOARD_HTML
