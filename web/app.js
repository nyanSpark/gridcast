/* gridcast frontend.
 *
 * Two things here are worth knowing before reading the rest:
 *
 * 1. It works against EITHER backend. On load it probes `api/meta`; if a
 *    FastAPI server answers, ranges are arbitrary and zooming refetches at a
 *    finer bucket. If not, it falls back to the static JSON in `data/`, which
 *    is what a zero-backend host (Vercel, GitHub Pages) serves. Same UI.
 *
 * 2. All timestamps arrive as UTC and are converted to Pacific *wall clock*
 *    here rather than handed to Plotly as instants. Plotly renders naive date
 *    strings in the viewer's local zone, which would silently relabel the whole
 *    chart for anyone outside California. Converting explicitly is the same
 *    discipline the backend applies in timeutil.py.
 */
'use strict';

const TABS = {
  timeline: { view: 'series',         chart: 'chart-timeline', render: renderTimeline },
  response: { view: 'response-curve', chart: 'chart-response', render: renderResponse },
  accuracy: { view: 'forecast-error', chart: 'chart-accuracy', render: renderAccuracy },
  fuel:     { view: 'fuel-mix',       chart: 'chart-fuel',     render: renderFuel },
};

const FUEL_COLORS = {
  solar: '#f2c14e', wind: '#62b6cb', natural_gas: '#a4826b', large_hydro: '#4b8fd0',
  nuclear: '#9b7fc7', geothermal: '#c4726b', small_hydro: '#6da8c9', biomass: '#8fa36b',
  biogas: '#a8b06b', coal: '#5a5a5a', imports: '#8a8f98', batteries: '#6bbf8a',
};
const STACKED_FUELS = ['solar', 'wind', 'geothermal', 'biomass', 'biogas', 'small_hydro',
                       'coal', 'nuclear', 'natural_gas', 'large_hydro'];
/* Imports and batteries go on their own lines: both can be negative (exporting,
 * charging) and a stacked area with negative members reads as a bug. */
const LINE_FUELS = ['imports', 'batteries'];

/* Hour of day is cyclical, so the scale has to wrap: midnight at both ends. */
const HOUR_SCALE = [
  [0.00, '#3b4a6b'], [0.17, '#4a7fa8'], [0.33, '#7fc0e8'],
  [0.50, '#f2c14e'], [0.67, '#e8833a'], [0.83, '#a8556b'], [1.00, '#3b4a6b'],
];

const state = {
  tab: 'timeline', location: null, preset: '14d', unit: 'F',
  meta: null, window: null, cache: new Map(),
};

/* ---------- Pacific time ---------------------------------------------------- */
const PT_PARTS = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/Los_Angeles', hour12: false,
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
});

function toPacific(iso) {
  if (!iso) return null;
  const parts = {};
  for (const { type, value } of PT_PARTS.formatToParts(new Date(iso))) parts[type] = value;
  const hour = parts.hour === '24' ? '00' : parts.hour;
  return `${parts.year}-${parts.month}-${parts.day} ${hour}:${parts.minute}:${parts.second}`;
}
const toPacificAll = (list) => (list || []).map(toPacific);

/* Inverse of toPacific: a Pacific wall-clock string back to a UTC instant.
 * Solved by iteration because the offset depends on the answer -- two passes
 * converge everywhere except inside the one-hour DST fold, which is close
 * enough for interpreting a zoom gesture. */
function pacificToUtcIso(wall) {
  const naive = Date.parse(wall.replace(' ', 'T') + 'Z');
  if (Number.isNaN(naive)) return null;
  let guess = naive;
  for (let i = 0; i < 2; i++) {
    const shown = Date.parse(toPacific(new Date(guess).toISOString()).replace(' ', 'T') + 'Z');
    guess += naive - shown;
  }
  return new Date(guess).toISOString();
}

/* ---------- units ----------------------------------------------------------- */
const tempFrom = (celsius) => (celsius === null || celsius === undefined ? null
  : state.unit === 'F' ? celsius * 9 / 5 + 32 : celsius);
const tempAll = (list) => (list || []).map(tempFrom);
const tempLabel = () => (state.unit === 'F' ? '°F' : '°C');
/* A *difference* in °C scales by 9/5 but carries no +32 offset. */
const deltaFrom = (celsius) => (celsius === null || celsius === undefined ? null
  : state.unit === 'F' ? celsius * 9 / 5 : celsius);

/* ---------- theme ----------------------------------------------------------- */
function theme() {
  const styles = getComputedStyle(document.documentElement);
  const read = (name) => styles.getPropertyValue(name).trim();
  return {
    text: read('--text'), muted: read('--muted'), faint: read('--faint'),
    grid: read('--grid'), border: read('--border'), accent: read('--accent'),
    accentSoft: read('--accent-soft'), cool: read('--cool'), coolSoft: read('--cool-soft'),
    tooltipBg: read('--tooltip-bg'), tooltipFg: read('--tooltip-fg'),
    tooltipBorder: read('--tooltip-border'),
  };
}

function baseLayout(t, extra) {
  return Object.assign({
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    font: { color: t.text, family: 'ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif', size: 12 },
    margin: { l: 64, r: 64, t: 34, b: 44 },
    hovermode: 'x unified',
    legend: { orientation: 'h', y: 1.08, x: 0, bgcolor: 'rgba(0,0,0,0)', font: { size: 11 } },
    /* Plotly's default hover label is near-white with dark text, which is
     * unreadable on the dark theme. Drive it from the same tokens as the rest
     * of the page so it follows light/dark automatically. Set at the layout
     * level so it overrides the per-trace colouring Plotly applies in
     * 'closest' hovermode as well as the single box in 'x unified'. */
    hoverlabel: {
      bgcolor: t.tooltipBg,
      bordercolor: t.tooltipBorder,
      font: { color: t.tooltipFg, size: 12,
              family: 'ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif' },
      align: 'left',
    },
    xaxis: { gridcolor: t.grid, linecolor: t.border, zeroline: false },
    yaxis: { gridcolor: t.grid, zeroline: false },
  }, extra || {});
}

const PLOT_CONFIG = {
  responsive: true,
  displaylogo: false,
  modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'],
  toImageButtonOptions: { filename: 'gridcast', scale: 2 },
};

/* ---------- data access ----------------------------------------------------- */
const Data = {
  mode: null,

  async init() {
    try {
      const response = await fetch('api/meta', { cache: 'no-store' });
      if (response.ok) {
        const meta = await response.json();
        if (meta && meta.mode === 'api') { this.mode = 'api'; return meta; }
      }
    } catch (error) { /* no server here; that is a supported deployment */ }

    const response = await fetch('data/meta.json', { cache: 'no-store' });
    if (!response.ok) throw new Error('no data found — run `gridcast export`');
    this.mode = 'static';
    return response.json();
  },

  async view(name, { location, preset, start, end }) {
    if (this.mode === 'api') {
      const query = new URLSearchParams({ location });
      if (start && end) { query.set('start', start); query.set('end', end); }
      else { query.set('range', preset); }
      const response = await fetch(`api/${name}?${query}`);
      if (!response.ok) throw new Error(`${name}: ${response.status}`);
      return response.json();
    }
    const response = await fetch(`data/${name}-${location}-${preset}.json`);
    if (!response.ok) throw new Error(`missing data/${name}-${location}-${preset}.json`);
    return response.json();
  },
};

/* ---------- renderers ------------------------------------------------------- */
function renderTimeline(node, payload) {
  const t = theme();
  const grid = payload.grid || {};
  const weather = payload.weather || {};
  const nowPacific = toPacific(new Date().toISOString());

  const traces = [];
  const line = (x, y, name, color, dash, axis, width) => {
    if (!x || !x.length) return;
    traces.push({
      type: 'scatter', mode: 'lines', name, x, y,
      line: { color, width: width || 1.8, dash: dash || 'solid', shape: 'spline', smoothing: 0.3 },
      yaxis: axis, hovertemplate: '%{y:,.0f}<extra>' + name + '</extra>',
    });
  };

  const gx = toPacificAll(grid.t);
  line(gx, grid.demand, 'Demand (actual)', t.accent, 'solid', 'y', 2.2);
  line(gx, grid.day_ahead_forecast, 'Day-ahead forecast', t.muted, 'dash', 'y');
  line(gx, grid.hour_ahead_forecast, 'Hour-ahead forecast', t.faint, 'dot', 'y');
  line(toPacificAll((payload.load_forecast || {}).t), (payload.load_forecast || {}).forecast_mw,
       'Load forecast (7-day)', t.accentSoft, 'dash', 'y');

  const wx = toPacificAll(weather.t);
  if (wx.length) {
    traces.push({
      type: 'scatter', mode: 'lines', name: `Temperature (${tempLabel()})`,
      x: wx, y: tempAll(weather.temperature_2m), yaxis: 'y2',
      line: { color: t.cool, width: 2 },
      hovertemplate: '%{y:.1f}' + tempLabel() + '<extra>Temperature</extra>',
    });
  }
  const forecast = payload.weather_forecast || {};
  if (forecast.t && forecast.t.length) {
    traces.push({
      type: 'scatter', mode: 'lines', name: 'Temperature forecast',
      x: toPacificAll(forecast.t), y: tempAll(forecast.temperature_2m), yaxis: 'y2',
      line: { color: t.cool, width: 1.8, dash: 'dash' },
      hovertemplate: '%{y:.1f}' + tempLabel() + '<extra>Temp forecast</extra>',
    });
  }

  const layout = baseLayout(t, {
    xaxis: {
      gridcolor: t.grid, linecolor: t.border, zeroline: false,
      rangeslider: { visible: true, thickness: 0.08, bgcolor: 'rgba(0,0,0,0)',
                     bordercolor: t.border, borderwidth: 1 },
      title: { text: 'Pacific time', font: { size: 11, color: t.faint } },
    },
    yaxis: { title: { text: 'Megawatts', font: { size: 11, color: t.faint } },
             gridcolor: t.grid, zeroline: false, rangemode: 'tozero' },
    yaxis2: { title: { text: `Temperature ${tempLabel()}`, font: { size: 11, color: t.faint } },
              overlaying: 'y', side: 'right', showgrid: false, zeroline: false },
    shapes: [
      { type: 'rect', xref: 'x', yref: 'paper', x0: nowPacific, x1: gx[gx.length - 1] || nowPacific,
        y0: 0, y1: 1, fillcolor: t.faint, opacity: 0.07, line: { width: 0 }, layer: 'below' },
      { type: 'line', xref: 'x', yref: 'paper', x0: nowPacific, x1: nowPacific,
        y0: 0, y1: 1, line: { color: t.faint, width: 1, dash: 'dot' } },
    ],
    annotations: [
      { x: nowPacific, y: 1.02, xref: 'x', yref: 'paper', text: 'now', showarrow: false,
        font: { size: 10, color: t.faint }, xanchor: 'left' },
    ],
  });

  Plotly.react(node, traces, layout, PLOT_CONFIG);
  attachZoomRefetch(node);
}

function renderResponse(node, payload) {
  const t = theme();
  const points = payload.points || {};
  const traces = [{
    /* Plain SVG scatter, not scattergl: it keeps us on the plotly-basic bundle
     * (347KB vs 1.3MB) and renders a full year -- 8,760 points -- in ~94ms. */
    type: 'scatter', mode: 'markers',
    x: tempAll(points.temperature_2m), y: points.demand,
    marker: {
      size: 5, opacity: 0.62, color: points.hour_pt, colorscale: HOUR_SCALE,
      cmin: 0, cmax: 23,
      colorbar: {
        title: { text: 'Hour (PT)', font: { size: 11, color: t.faint } },
        thickness: 12, len: 0.72, tickvals: [0, 6, 12, 18, 23], outlinewidth: 0,
      },
    },
    customdata: points.hour_pt,
    hovertemplate: `%{x:.1f}${tempLabel()} · %{y:,.0f} MW · %{customdata}:00<extra></extra>`,
    name: '',
  }];
  const layout = baseLayout(t, {
    hovermode: 'closest',
    showlegend: false,
    xaxis: { title: { text: `Temperature ${tempLabel()}`, font: { size: 11, color: t.faint } },
             gridcolor: t.grid, zeroline: false },
    yaxis: { title: { text: 'Demand (MW)', font: { size: 11, color: t.faint } },
             gridcolor: t.grid, zeroline: false },
  });
  Plotly.react(node, traces, layout, PLOT_CONFIG);
}

function renderAccuracy(node, payload) {
  const t = theme();
  const caiso = payload.caiso || {};
  const byHour = payload.by_hour || {};
  const byLead = payload.weather_by_lead || {};
  const coupling = payload.coupling || {};

  const traces = [
    { type: 'scatter', mode: 'lines', name: 'Day-ahead error',
      x: toPacificAll(caiso.t), y: caiso.error_pct, fill: 'tozeroy',
      line: { color: t.accent, width: 1.4 }, fillcolor: 'rgba(210,105,30,.16)',
      xaxis: 'x', yaxis: 'y',
      hovertemplate: '%{y:+.2f}%<extra>forecast − actual</extra>' },

    { type: 'bar', name: 'MAPE by hour', x: byHour.hour_pt, y: byHour.mape,
      marker: { color: t.cool }, xaxis: 'x2', yaxis: 'y2',
      hovertemplate: '%{x}:00 — %{y:.2f}%<extra></extra>' },

    { type: 'scatter', mode: 'lines+markers', name: 'Temp MAE by lead',
      x: byLead.lead_days, y: (byLead.mae || []).map(deltaFrom),
      line: { color: t.accent, width: 2 }, marker: { size: 7 },
      xaxis: 'x3', yaxis: 'y3',
      hovertemplate: `day %{x}: %{y:.2f}${tempLabel()}<extra></extra>` },

    { type: 'scatter', mode: 'markers', name: 'One day',
      x: (coupling.temp_mae_c || []).map(deltaFrom), y: coupling.load_mape,
      text: (coupling.day || []).map((d) => String(d).slice(0, 10)),
      marker: {
        size: 9, opacity: 0.75, color: coupling.peak_mw, colorscale: 'YlOrRd',
        colorbar: { title: { text: 'Peak MW', font: { size: 10, color: t.faint } },
                    thickness: 10, len: 0.34, y: 0.17, outlinewidth: 0 },
      },
      xaxis: 'x4', yaxis: 'y4',
      hovertemplate: `%{text}<br>temp MAE %{x:.2f}${tempLabel()}<br>load MAPE %{y:.2f}%<extra></extra>` },
  ];

  const axis = (label, extra) => Object.assign({
    gridcolor: t.grid, zeroline: false,
    title: { text: label, font: { size: 11, color: t.faint } },
  }, extra || {});
  const layout = baseLayout(t, {
    grid: { rows: 2, columns: 2, pattern: 'independent', roworder: 'top to bottom',
            xgap: 0.14, ygap: 0.28 },
    showlegend: false,
    hovermode: 'closest',
    margin: { l: 62, r: 70, t: 46, b: 52 },
    xaxis:  axis('Pacific time'),
    yaxis:  axis('Forecast − actual (%)'),
    xaxis2: axis('Hour of day (PT)', { dtick: 3 }),
    yaxis2: axis('Mean abs. error (%)'),
    xaxis3: axis('Days ahead', { dtick: 1 }),
    yaxis3: axis(`Temp MAE (${tempLabel()})`),
    xaxis4: axis(`Temp forecast MAE (${tempLabel()})`),
    yaxis4: axis('Load forecast MAPE (%)'),
    annotations: [
      title('Load forecast error over time', 0, 1.0),
      title('Where the day-ahead forecast struggles', 0.57, 1.0),
      title('Weather forecast decay by lead time', 0, 0.44),
      title('Do bad weather forecasts mean bad load forecasts?', 0.57, 0.44),
    ],
  });

  function title(text, x, y) {
    return { text: `<b>${text}</b>`, x, y, xref: 'paper', yref: 'paper',
             xanchor: 'left', yanchor: 'bottom', showarrow: false,
             font: { size: 12, color: t.text } };
  }

  Plotly.react(node, traces, layout, PLOT_CONFIG);
}

function renderFuel(node, payload) {
  const t = theme();
  const mix = payload.mix || {};
  const weather = payload.weather || {};
  const x = toPacificAll(mix.t);

  const traces = [];
  for (const fuel of STACKED_FUELS) {
    if (!mix[fuel] || !mix[fuel].some((v) => v !== null && v !== 0)) continue;
    traces.push({
      type: 'scatter', mode: 'lines', name: fuel.replace(/_/g, ' '),
      x, y: mix[fuel], stackgroup: 'generation',
      line: { width: 0.5, color: FUEL_COLORS[fuel] },
      fillcolor: FUEL_COLORS[fuel],
      hovertemplate: '%{y:,.0f} MW<extra>' + fuel.replace(/_/g, ' ') + '</extra>',
    });
  }
  for (const fuel of LINE_FUELS) {
    if (!mix[fuel]) continue;
    traces.push({
      type: 'scatter', mode: 'lines', name: fuel + ' (net)',
      x, y: mix[fuel], line: { color: FUEL_COLORS[fuel], width: 1.6, dash: 'dot' },
      hovertemplate: '%{y:,.0f} MW<extra>' + fuel + '</extra>',
    });
  }
  if (weather.t && weather.t.length) {
    traces.push({
      type: 'scatter', mode: 'lines', name: 'Irradiance (W/m²)',
      x: toPacificAll(weather.t), y: weather.shortwave_radiation, yaxis: 'y2',
      line: { color: t.text, width: 1.2, dash: 'dot' },
      hovertemplate: '%{y:.0f} W/m²<extra>Irradiance</extra>',
    });
  }

  const layout = baseLayout(t, {
    xaxis: { gridcolor: t.grid, zeroline: false,
             rangeslider: { visible: true, thickness: 0.08, bgcolor: 'rgba(0,0,0,0)',
                            bordercolor: t.border, borderwidth: 1 },
             title: { text: 'Pacific time', font: { size: 11, color: t.faint } } },
    yaxis: { title: { text: 'Generation (MW)', font: { size: 11, color: t.faint } },
             gridcolor: t.grid, zeroline: true, zerolinecolor: t.border },
    yaxis2: { title: { text: 'Irradiance (W/m²)', font: { size: 11, color: t.faint } },
              overlaying: 'y', side: 'right', showgrid: false, zeroline: false },
  });
  Plotly.react(node, traces, layout, PLOT_CONFIG);
}

/* ---------- zoom-to-refetch (API mode only) --------------------------------- */
let zoomTimer = null;
function attachZoomRefetch(node) {
  if (Data.mode !== 'api' || node.dataset.zoomBound === '1') return;
  node.dataset.zoomBound = '1';
  node.on('plotly_relayout', (event) => {
    const from = event['xaxis.range[0]'] ?? (event['xaxis.range'] || [])[0];
    const to = event['xaxis.range[1]'] ?? (event['xaxis.range'] || [])[1];
    if (!from || !to) {
      if (event['xaxis.autorange']) { state.window = null; scheduleRefresh(); }
      return;
    }
    /* Re-request the visible window so the server re-buckets it. This is what
     * makes zooming reveal 5-minute detail instead of stretching hourly points. */
    state.window = { start: pacificToUtcIso(String(from)), end: pacificToUtcIso(String(to)) };
    scheduleRefresh();
  });
}
function scheduleRefresh() {
  clearTimeout(zoomTimer);
  zoomTimer = setTimeout(() => refresh({ keepZoom: true }), 420);
}

/* ---------- shell ----------------------------------------------------------- */
const $ = (id) => document.getElementById(id);

function setStatus(mode, extra) {
  const dot = $('mode-dot');
  dot.className = 'dot ' + (mode === 'api' ? 'live' : 'static');
  $('mode-label').textContent = (mode === 'api' ? 'live API' : 'static snapshot') + (extra ? ` · ${extra}` : '');
}

async function refresh(options) {
  const tab = TABS[state.tab];
  const node = $(tab.chart);
  const request = {
    location: state.location,
    preset: state.preset,
    start: state.window && state.window.start,
    end: state.window && state.window.end,
  };
  const key = `${tab.view}|${request.location}|${request.start || request.preset}|${request.end || ''}`;

  try {
    let payload = state.cache.get(key);
    if (!payload) {
      payload = await Data.view(tab.view, request);
      state.cache.set(key, payload);
    }
    $('empty').classList.add('is-hidden');
    tab.render(node, payload);
    const count = (payload.grid && payload.grid.t) ? payload.grid.t.length
                : (payload.mix && payload.mix.t) ? payload.mix.t.length
                : (payload.points && payload.points.t) ? payload.points.t.length : null;
    $('bucket-hint').textContent = payload.bucket
      ? `${payload.bucket} buckets${count ? ` · ${count.toLocaleString()} points` : ''}`
      : '';
  } catch (error) {
    console.error(error);
    $('empty').classList.remove('is-hidden');
    $('empty').querySelector('p').textContent = error.message;
  }
}

function buildControls() {
  const locations = $('location');
  locations.innerHTML = '';
  for (const place of state.meta.locations) {
    const option = document.createElement('option');
    option.value = place.key;
    option.textContent = place.name;
    locations.append(option);
  }
  locations.value = state.location;
  locations.addEventListener('change', () => {
    state.location = locations.value;
    state.window = null;
    refresh();
  });

  const presets = $('presets');
  presets.innerHTML = '';
  for (const [key, label] of Object.entries(state.meta.presets)) {
    const button = document.createElement('button');
    button.textContent = label;
    button.dataset.preset = key;
    if (key === state.preset) button.classList.add('is-active');
    button.addEventListener('click', () => {
      state.preset = key;
      state.window = null;
      presets.querySelectorAll('button').forEach((b) => b.classList.toggle('is-active', b === button));
      refresh();
    });
    presets.append(button);
  }

  $('units').addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (!button) return;
    state.unit = button.dataset.unit;
    $('units').querySelectorAll('button').forEach((b) => b.classList.toggle('is-active', b === button));
    refresh();
  });

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      state.tab = tab.dataset.tab;
      document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('is-active', b === tab));
      for (const [name, config] of Object.entries(TABS)) {
        $(`panel-${name}`).classList.toggle('is-hidden', name !== state.tab);
        if (name === state.tab) Plotly.Plots.resize($(config.chart));
      }
      refresh();
    });
  });

  window.matchMedia('(prefers-color-scheme: dark)')
    .addEventListener('change', () => { state.cache.clear(); refresh(); });
}

async function main() {
  try {
    state.meta = await Data.init();
  } catch (error) {
    setStatus('static', 'no data');
    $('empty').classList.remove('is-hidden');
    $('empty').querySelector('p').textContent = error.message;
    return;
  }
  state.location = state.meta.default_location || (state.meta.locations[0] || {}).key;
  state.preset = state.meta.default_preset || '14d';
  const generated = state.meta.generated_at
    ? new Date(state.meta.generated_at).toLocaleString(undefined, { timeZone: 'America/Los_Angeles' })
    : null;
  setStatus(Data.mode, generated ? `updated ${generated} PT` : null);
  $('generated').textContent = generated ? `Snapshot generated ${generated} PT.` : '';
  buildControls();
  await refresh();
}

main();
