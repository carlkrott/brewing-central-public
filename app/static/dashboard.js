    const charts = {};
    let currentDevices = [];

    async function loadHealth() {
        try {
            const health = await fetch('/api/system-health').then(r=>r.json());
            const target = document.getElementById('health-status');
            target.replaceChildren();
            const addEvidence = (label, evidence) => {
                const row = document.createElement('div'); row.style.marginBottom = '8px';
                const state = evidence && evidence.status || 'missing';
                const dot = document.createElement('span');
                dot.className = 'status-indicator ' + (state === 'ok' ? 'status-ok' : state === 'warning' ? 'status-warning' : 'status-error');
                const strong = document.createElement('strong'); strong.textContent = label + ': ';
                const text = document.createElement('span');
                text.textContent = evidence ? `${state}: ${evidence.detail || evidence.content || 'No data'}` : 'missing';
                row.append(dot, strong, text); target.appendChild(row);
            };
            const batteryLabel = health.battery && health.battery.scope === 'device_telemetry'
                ? 'iSpindel battery' : 'Host battery';
            addEvidence(batteryLabel, health.battery);
            addEvidence('Heartbeat', health.heartbeat);
        } catch(e) {
            document.getElementById('health-status').textContent = "Failed to load health data.";
        }
    }

    async function refresh() {
      loadHealth();

      const s = await fetch('/api/status').then(r=>r.json());
      const summary = document.getElementById('summary'); summary.replaceChildren();
      [['Total Devices', s.devices], ['Stored', s.stored_devices ?? 0], ['Preparing', s.preparing_devices ?? 0], ['Brewing', s.brewing_devices ?? 0], ['Stale Devices', s.stale_devices], ['Total Samples', s.total_samples]].forEach(([label, value]) => {
        const row = document.createElement('div'); const strong = document.createElement('strong');
        strong.textContent = label + ': '; const valueNode = document.createElement('span'); valueNode.textContent = String(value);
        if (label === 'Stale Devices' && Number(value) > 0) valueNode.style.color = '#b91c1c';
        row.append(strong, valueNode); summary.appendChild(row);
      });
      if (s.not_required_no_active_brew) {
        const row = document.createElement('div');
        const strong = document.createElement('strong'); strong.textContent = 'Camera: ';
        const valueNode = document.createElement('span'); valueNode.textContent = 'Not required — no active brew';
        row.append(strong, valueNode); summary.appendChild(row);
      }

      const res = await fetch('/api/devices').then(r=>r.json());
      currentDevices = res.devices;

      const tbody = document.getElementById('devices-body');
      tbody.replaceChildren();
      const select = document.getElementById('device-select');
      const currentSelected = select.value;
      select.replaceChildren();
      const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = 'Select Device...'; select.appendChild(placeholder);

      for(const d of currentDevices){
        // Populate table
        let d_date = d.last_seen ? new Date(d.last_seen).toLocaleString() : 'Never';
        const row = document.createElement('tr');
        [d.device_id, d.device_name || '', d.operating_mode || 'brewing', d.camera_policy || 'active_brew_structural', d.expected_interval_sec, d_date, d.sample_count].forEach(value => {
          const cell = document.createElement('td'); cell.textContent = String(value); row.appendChild(cell);
        });
        const actions = document.createElement('td'); const edit = document.createElement('button');
        edit.className = 'secondary'; edit.textContent = 'Edit'; edit.addEventListener('click', () => openEditModal(d.device_id, edit));
        actions.appendChild(edit); row.appendChild(actions); tbody.appendChild(row);

        // Populate dropdown
        const opt = document.createElement('option');
        opt.value = d.device_id;
        opt.innerText = d.device_name || d.device_id;
        if(d.device_id === currentSelected) opt.selected = true;
        select.appendChild(opt);
      }

      if (!currentSelected && currentDevices.length > 0) {
          select.value = currentDevices[0].device_id;
      }

      // Update the device count badge in the collapsible header
      const countEl = document.getElementById('devices-count');
      if (countEl) countEl.textContent = `(${currentDevices.length})`;

      // Do NOT call loadChart() here — startChartPolling() owns the chart refresh cadence.
      // This keeps the dashboard's device table fresh without forcing chart redraws.
    }

    // ---- In-flight chart-poll guard (mutual exclusion) ----
    let isFetching = false;
    let inFlightController = null;
    let lastErrorMessage = '';

    async function loadChart() {
        if (isFetching) return null;
        const device = document.getElementById('device-select').value;
        const hours = document.getElementById('time-window').value;
        if (!device) return null;

        isFetching = true;
        const controller = (typeof AbortController !== 'undefined') ? new AbortController() : null;
        inFlightController = controller;
        const fetchOpts = controller ? { signal: controller.signal } : {};
        try {
            const response = await fetch(`/api/device/${device}/samples?hours=${hours}`, fetchOpts);
            if (!response.ok) {
                lastErrorMessage = `HTTP ${response.status}`;
                showChartError(`Failed to load samples (HTTP ${response.status})`);
                return null;
            }
            const json = await response.json();
            const samples = Array.isArray(json.samples) ? json.samples.filter(s => s && s.ts) : [];
            if (samples.length === 0) {
                lastErrorMessage = 'No new samples';
                showChartError('No new samples (keeping previous chart)');
                return null;
            }
            hideChartError();
            lastErrorMessage = '';
            updateChartLive(samples);
            return samples;
        } catch(e) {
            if (e && e.name === 'AbortError') return null;
            lastErrorMessage = String(e && e.message ? e.message : e);
            showChartError(`Failed to load samples (${lastErrorMessage})`);
            console.error('Failed to load chart', e);
            return null;
        } finally {
            // An aborted older request must not clear a newer request's guard.
            if (inFlightController === controller) {
                isFetching = false;
                inFlightController = null;
            }
        }
    }

    function showChartError(msg) {
        const el = document.getElementById('chartError');
        if (el) {
            el.textContent = msg;
            el.classList.add('is-shown');
        }
    }
    function hideChartError() {
        const el = document.getElementById('chartError');
        if (el) {
            el.textContent = '';
            el.classList.remove('is-shown');
        }
    }

    /* ============================================================
       Multi-metric live chart — Chart.js 4.x
       ============================================================ */
    const METRICS = [
        { id: 'gravity', label: 'Gravity (SG)',     color: '#4f46e5', unit: 'SG',  default: true  },
        { id: 'temp_c',  label: 'Temperature (°C)', color: '#ef4444', unit: '°C',  default: true  },
        { id: 'battery', label: 'Battery',          color: '#f59e0b', unit: '?',   default: true  },
        { id: 'angle',   label: 'Tilt angle (°)',   color: '#10b981', unit: '°',   default: false },
        { id: 'rssi',    label: 'WiFi RSSI (dBm)',  color: '#8b5cf6', unit: 'dBm', default: false },
    ];
    const VIEWS = {
        'fermentation':      { label: '🍺 Fermentation',    metrics: ['gravity','temp_c','battery'] },
        'gravity-only':      { label: '📏 Gravity Only',    metrics: ['gravity'] },
        'gravity-temp':      { label: '🌡 Gravity + Temp',  metrics: ['gravity','temp_c'] },
        'battery-telemetry': { label: '🔋 Battery / Wi-Fi', metrics: ['battery','rssi','temp_c'] },
        'all-sensors':       { label: '🛰 All Sensors',     metrics: ['gravity','angle','temp_c','battery','rssi'] },
    };
    const METRIC_DECIMALS = { gravity:3, temp_c:2, battery:2, angle:1, rssi:0 };

    function metricUnit(metric, point) {
        if (metric.id !== 'battery') return metric.unit;
        return point && (point.battery_unit === 'V' || point.battery_unit === '%')
            ? point.battery_unit : '?';
    }

    function metricValue(metric, sample) {
        if (!sample) return null;
        if (metric.id !== 'gravity') return sample[metric.id];
        const mode = (document.getElementById('gravity-mode') || {}).value || 'raw';
        return mode === 'calibrated'
            ? sample.calibrated_gravity
            : (sample.raw_gravity ?? sample.gravity);
    }

    function updateGravityMetricLabel() {
        const gravity = METRICS.find(metric => metric.id === 'gravity');
        const mode = (document.getElementById('gravity-mode') || {}).value || 'raw';
        gravity.label = mode === 'calibrated' ? 'Calibrated Gravity (SG)' : 'Raw Gravity (SG)';
    }

    let chart = null;
    let pollTimer = null;
    let countdownTimer = null;
    let nextRefreshAt = 0;

    function tsToMs(ts) {
        // API returns ISO with +00:00; Date handles it. Defensive: no tz → assume Z.
        if (typeof ts !== 'string') return Number(ts);
        if (!ts.endsWith('Z') && !/[+-]\d{2}:?\d{2}$/.test(ts)) return new Date(ts + 'Z').getTime();
        return new Date(ts).getTime();
    }

    function computeXRange(hours, now) {
        // Sliding window: min/mid derived from the WALL CLOCK on every call.
        // The width stays hours*3600e3; we add a small trailing padding so
        // the rightmost sample isn't pinned exactly to the axis edge.
        const hoursMs = hours * 3600 * 1000;
        const min = now - hoursMs;
        // Tail padding: 2% of the window (so 24h -> ~28.8 min "future" slack).
        const max = now + hoursMs * 0.02;
        return { min: min, max: max };
    }

    function visibleSet() {
        return new Set([...document.querySelectorAll('.metric-toggle:checked')].map(el => el.dataset.metric));
    }

    /* Per-metric non-color chart identifiers: each metric gets a distinct dash
       pattern AND a distinct pointStyle so the line is recognisable without
       relying on colour (deuteranopic / colourblind safety). The order is
       deterministic and indexed against the METRICS array position. */
    const METRIC_DASH_STYLE = [
        [],                 // gravity - solid
        [6, 4],              // temp_c  - long dash
        [2, 2],              // battery - dot
        [8, 4, 2, 4],        // angle   - dash-dot
        [10, 6, 2, 6, 2, 6],// rssi    - long dash + dot
    ];
    const METRIC_POINT_STYLE = [
        'circle',           // gravity
        'triangle',         // temp_c
        'rect',             // battery
        'rectRot',          // angle
        'cross',            // rssi
    ];

    function buildDatasets(samples, vis) {
        // Each visible metric maps to its own dedicated y-axis on the left.
        // Axis ID is `y_<metric>` (e.g. `y_gravity`); axis is created/removed
        // dynamically inside updateChartLive() based on which metrics are visible.
        return METRICS.map((m, idx) => ({
            label:           m.label,
            metric:          m.id,
            data:            samples
                .filter(s => metricValue(m, s) != null)
                .map(s => ({ x: tsToMs(s.ts), y: metricValue(m, s), battery_unit: s.battery_unit })),
            borderColor:     m.color,
            backgroundColor: m.color + '20',
            borderWidth:     2,
            pointRadius:     (document.getElementById('opt-markers') || {}).checked ? 3 : 0,
            pointHoverRadius: 4,
            // Non-color identifiers: distinct dash + pointStyle per metric.
            borderDash:      METRIC_DASH_STYLE[idx] || [],
            pointStyle:      METRIC_POINT_STYLE[idx] || 'circle',
            tension:         (document.getElementById('opt-smooth') || {}).checked ? 0.25 : 0,
            spanGaps:        true,
            yAxisID:         'y_' + m.id,
            hidden:          !vis.has(m.id),
        }));
    }

    // Build one deterministic y-axis configuration for every metric. Keeping all
    // five explicit prevents Chart.js from auto-creating implicit axes for hidden
    // datasets. Inactive axes use display:false and therefore consume no lane.
    function buildScales(vis, showGrid) {
        const visibleMetrics = METRICS.filter(m => vis.has(m.id));
        const axes = {};
        METRICS.forEach((m, idx) => {
            const axisId = 'y_' + m.id;
            const visibleIndex = visibleMetrics.findIndex(v => v.id === m.id);
            const isFirst = visibleIndex === 0;
            axes[axisId] = {
                type: 'linear',
                position: 'left',
                weight: METRICS.length - idx,
                alignToPixels: true,
                display: vis.has(m.id),
                title: {
                    display: true,
                    text: m.unit,
                    color: m.color,
                    font: { weight: 'bold', size: 11 },
                },
                ticks: {
                    color: m.color,
                    font: { weight: '500', size: 10 },
                    maxTicksLimit: 6,
                },
                grid: {
                    display: showGrid,
                    // Only the first visible axis draws horizontal plot gridlines.
                    drawOnChartArea: isFirst,
                    color: 'rgba(0,0,0,0.06)',
                },
                border: { color: m.color, display: true, width: 2 },
            };
        });
        return axes;
    }

    function setOverlay(show, msg) {
        const el = document.getElementById('chartOverlay');
        if (!el) return;
        el.textContent = msg || 'Waiting for data…';
        el.classList.toggle('is-shown', !!show);
    }

    function updateChartLive(samples) {
        updateGravityMetricLabel();
        const latestBattery = [...samples].reverse().find(s => s.battery != null);
        const batteryMetric = METRICS.find(m => m.id === 'battery');
        batteryMetric.unit = metricUnit(batteryMetric, latestBattery);
        batteryMetric.label = batteryMetric.unit === '?' ? 'Battery' : `Battery (${batteryMetric.unit})`;
        const hours = Number(document.getElementById('time-window').value || 24);
        const now = Date.now();
        const xRange = computeXRange(hours, now);
        const vis = visibleSet();
        const showGrid = (document.getElementById('opt-grid') || {}).checked !== false;

        setOverlay(samples.length < 2, samples.length === 0 ? 'Waiting for data…' : 'Need ≥2 samples to draw axis…');

        const visKey = [...vis].sort().join(',');

        // First paint OR axis visibility changed → rebuild chart with new scale structure.
        if (!chart || chart._visKey !== visKey) {
            if (chart) { chart.destroy(); chart = null; }
            chart = new Chart(document.getElementById('chartCanvas').getContext('2d'), {
                type: 'line',
                data: { datasets: buildDatasets(samples, vis) },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: false,
                    parsing: false,
                    layout: { padding: { right: 12, left: 0 } },
                    interaction: { mode: 'nearest', intersect: false, axis: 'x' },
                    plugins: {
                        legend: {
                            position: 'top',
                            labels: { boxWidth: 12, boxHeight: 12, padding: 12 },
                            onClick: (e, item, legend) => {
                                Chart.defaults.plugins.legend.onClick.call(this, e, item, legend);
                                const cb = document.querySelector(`.metric-toggle[data-metric="${METRICS[item.datasetIndex].id}"]`);
                                if (cb && chart) cb.checked = chart.isDatasetVisible(item.datasetIndex);
                            },
                        },
                        tooltip: {
                            callbacks: {
                                label: (ctx) => {
                                    const ds = ctx.dataset;
                                    const m = METRICS.find(x => x.id === ds.metric) || METRICS[ctx.datasetIndex];
                                    const dec = METRIC_DECIMALS[m.id] ?? 3;
                                    return `${m.label}: ${ctx.parsed.y.toFixed(dec)} ${metricUnit(m, ctx.raw)}`;
                                },
                                title: (items) => items.length ? new Date(items[0].parsed.x).toLocaleTimeString() : '',
                            },
                        },
                    },
                    scales: {
                        x: {
                            type: 'time',
                            min: xRange.min,
                            max: xRange.max,
                            time: {
                                unit: 'minute',
                                tooltipFormat: 'yyyy-MM-dd HH:mm:ss',
                                displayFormats: { minute:'HH:mm', hour:'HH:mm', day:'MMM d', second:'HH:mm:ss' },
                            },
                            ticks: { source:'auto', maxTicksLimit:8, autoSkip:true, maxRotation:0, color:'#374151' },
                            grid:  { display: showGrid, color:'rgba(0,0,0,0.06)' },
                            border: { color: '#9ca3af' },
                        },
                        ...buildScales(vis, showGrid),
                    },
                },
            });
            chart._visKey = visKey;
        } else {
            // Same visible-set → in-place update (no destroy, no flicker).
            METRICS.forEach((m, i) => {
                chart.data.datasets[i].data = samples
                    .filter(s => metricValue(m, s) != null)
                    .map(s => ({ x: tsToMs(s.ts), y: metricValue(m, s), battery_unit: s.battery_unit }));
                chart.data.datasets[i].label = m.label;
                chart.options.scales['y_' + m.id].title.text = m.unit;
                chart.data.datasets[i].hidden = !vis.has(m.id);
                chart.data.datasets[i].pointRadius = (document.getElementById('opt-markers') || {}).checked ? 3 : 0;
                chart.data.datasets[i].tension    = (document.getElementById('opt-smooth') || {}).checked ? 0.25 : 0;
            });
            // Phase 1A: BOTH x.min AND x.max advance from the wall clock on every
            // successful unpaused poll — the right-edge guard from the previous
            // build is removed because the wall clock is now the source of truth
            // (not the latest sample timestamp).
            chart.options.scales.x.min = xRange.min;
            chart.options.scales.x.max = xRange.max;
            chart.options.scales.x.grid.display = showGrid;
            chart.update('none');
        }

        // Adjacent-sample deltas: walk the response backwards to find the
        // TWO most recent samples (per metric) and compute the pill delta
        // from the SECOND-LATEST minus the LATEST inside the SAME response.
        // No cross-poll cache participates in this calculation.
        const lastPerMetric = {};
        const prevPerMetric = {};
        for (let i = samples.length - 1; i >= 0; i--) {
            const s = samples[i];
            METRICS.forEach(m => {
                if (metricValue(m, s) == null) return;
                if (lastPerMetric[m.id] == null) { lastPerMetric[m.id] = s; return; }
                if (prevPerMetric[m.id] == null) { prevPerMetric[m.id] = s; }
            });
        }
        updateValuePills(lastPerMetric, prevPerMetric);
        updateChartAccessibleData(lastPerMetric);
    }

    function updateChartAccessibleData(latest) {
        const body = document.querySelector('#chart-data-table tbody');
        const summary = document.getElementById('chart-summary');
        if (!body || !summary) return;
        body.replaceChildren();
        const values = [];
        METRICS.forEach(m => {
            const point = latest[m.id]; if (!point) return;
            const row = document.createElement('tr');
            [m.label, `${Number(metricValue(m, point)).toFixed(METRIC_DECIMALS[m.id] ?? 3)} ${metricUnit(m, point)}`, new Date(point.ts).toLocaleString()].forEach(value => {
                const cell = document.createElement('td'); cell.textContent = value; row.appendChild(cell);
            }); body.appendChild(row); values.push(m.label);
        });
        summary.textContent = values.length ? `Latest values are available for ${values.join(', ')} in the telemetry data table.` : 'No telemetry values are available.';
    }

    function updateValuePills(latest, prev) {
        // `latest` is the latest sample-with-value per metric; `prev` is the
        // SECOND-LATEST sample-with-value per metric (in the SAME response).
        // The delta is now strictly the in-response adjacent gap, not a
        // carry-over from the previous poll.
        document.querySelectorAll('.value-pill').forEach(pill => {
            const m = pill.dataset.metric;
            const point = latest[m];
            const numEl = pill.querySelector('.num');
            const unitEl = pill.querySelector('.unit');
            const ageEl = pill.querySelector('.age');
            const deltaEl = pill.querySelector('.delta');
            if (!point) {
                pill.classList.add('is-stale');
                numEl.textContent = '—';
                unitEl.textContent = m === 'battery' ? '?' : (METRICS.find(item => item.id === m) || {}).unit || '';
                ageEl.textContent = '—';
                deltaEl.textContent = 'no data';
                deltaEl.className = 'delta';
                return;
            }
            pill.classList.remove('is-stale');
            const dec = METRIC_DECIMALS[m] ?? 3;
            const metric = METRICS.find(item => item.id === m);
            numEl.textContent = Number(metricValue(metric, point)).toFixed(dec);
            unitEl.textContent = metricUnit(metric, point);
            const ageMs = Date.now() - tsToMs(point.ts);
            const ageS = Math.max(0, Math.round(ageMs / 1000));
            ageEl.textContent = ageS < 60 ? `${ageS}s ago` : `${Math.round(ageS/60)}m ago`;
            const p = prev[m];
            const currentValue = metricValue(metric, point);
            const previousValue = metricValue(metric, p);
            if (p && Number.isFinite(currentValue) && Number.isFinite(previousValue) && currentValue !== previousValue) {
                const diff = currentValue - previousValue;
                const sign = diff > 0 ? '▲' : '▼';
                const cls = diff > 0 ? 'up' : 'down';
                deltaEl.textContent = `${sign}${Math.abs(diff).toFixed(dec)} since last sample`;
                deltaEl.className = `delta ${cls}`;
            } else {
                deltaEl.textContent = '— unchanged';
                deltaEl.className = 'delta';
            }
        });
    }

    function buildMetricToggles() {
        const c = document.getElementById('metricToggles');
        if (!c) return;
        c.replaceChildren();
        METRICS.forEach(m => {
            const wrap = document.createElement('label');
            wrap.className = 'metric-toggle-wrap';
            wrap.style.setProperty('--pill-color', m.color);
            const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'metric-toggle'; cb.dataset.metric = m.id; cb.checked = m.default;
            const swatch = document.createElement('span'); swatch.className = 'swatch'; swatch.setAttribute('aria-hidden', 'true');
            const label = document.createTextNode(m.label); wrap.append(cb, swatch, label);
            cb.addEventListener('change', () => {
                wrap.classList.toggle('is-muted', !cb.checked);
                // Toggling changes the visible-set → trigger a full chart rebuild so
                // the y-axis for this metric appears/disappears on the left.
                loadChart();
            });
            wrap.classList.toggle('is-muted', !m.default);
            c.appendChild(wrap);
        });
    }

    function setView(name) {
        const v = VIEWS[name];
        if (!v) return;
        localStorage.setItem('ispindel.view', name);
        document.querySelectorAll('.metric-toggle').forEach(cb => {
            const active = v.metrics.includes(cb.dataset.metric);
            cb.checked = active;
            cb.parentElement.classList.toggle('is-muted', !active);
        });
        // View change = visible-set change = y-axes must be reconfigured.
        loadChart();
    }
    function loadSavedView() {
        const saved = localStorage.getItem('ispindel.view');
        if (saved && VIEWS[saved]) {
            document.getElementById('view-select').value = saved;
            setView(saved);
        }
    }

    function startChartPolling() {
        if (pollTimer) return;
        const tick = async () => {
            if ((document.getElementById('opt-paused') || {}).checked) {
                pulsePaused();
                return;
            }
            const samples = await loadChart();
            if (samples) pulseTick();
        };
        tick();
        pollTimer = setInterval(tick, 5000);
    }
    function stopChartPolling() {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }

    function formatTimeZoneLabel(d) {
        // Try the modern short-name first; fall back to long, then 'Z' (UTC).
        try {
            const short = d.toLocaleTimeString(undefined, { timeZoneName: 'short' });
            const long  = d.toLocaleTimeString(undefined, { timeZoneName: 'long' });
            // toLocaleTimeString may already embed the zone; extract it.
            const m = (short || long || '').match(/([A-Z]{2,5})(?:[+-]\\d+)?$/);
            if (m) return m[1];
        } catch (e) { /* ignore */ }
        // UTC fallback (e.g. headless without tz data).
        try {
            return new Intl.DateTimeFormat(undefined, { timeZoneName: 'short' }).formatToParts(d)
                .find(p => p.type === 'timeZoneName').value;
        } catch (e) { return 'UTC'; }
    }

    function pulseTick() {
        const dot = document.getElementById('tick-dot');
        const container = document.getElementById('chartContainer');
        if (dot) { dot.classList.remove('is-pulsing'); void dot.offsetWidth; dot.classList.add('is-pulsing'); }
        if (container) { container.classList.remove('is-refreshing'); void container.offsetWidth; container.classList.add('is-refreshing'); }
        const last = document.getElementById('tick-last');
        const next = document.getElementById('tick-next');
        const now = new Date();
        if (last) {
            const tz = formatTimeZoneLabel(now);
            // e.g. "14:32:07 PDT"
            last.textContent = `Last refresh ${now.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'})} ${tz}`;
        }
        nextRefreshAt = Date.now() + 5000;
        if (next) next.textContent = 'next in 5s';
        hideChartError();
    }

    // Pulse used while paused: do NOT animate the green tick dot and do NOT
    // touch the "Last refresh" label; instead, freeze it AND show a literal
    // "Paused" so the dashboard doesn't pretend to be live.
    function pulsePaused() {
        const dot = document.getElementById('tick-dot');
        const container = document.getElementById('chartContainer');
        if (dot) dot.classList.remove('is-pulsing');
        if (container) container.classList.remove('is-refreshing');
        const next = document.getElementById('tick-next');
        if (next) next.textContent = 'Paused';
        nextRefreshAt = 0; // halt the countdown
    }

    function startCountdown() {
        if (countdownTimer) return;
        countdownTimer = setInterval(() => {
            const el = document.getElementById('tick-next');
            if (!el) return;
            // While paused: literal "Paused" — no countdown.
            if ((document.getElementById('opt-paused') || {}).checked) {
                el.textContent = 'Paused';
                return;
            }
            const remain = Math.max(0, Math.round((nextRefreshAt - Date.now()) / 1000));
            if (remain <= 0) el.textContent = 'refreshing…';
            else el.textContent = `next in ${remain}s`;
        }, 500);
    }

    function onDeviceOrWindowChange() {
        // Force a fresh chart for the new device/window (data identity changed).
        // In-flight poll (if any) is aborted so the new fetch can win cleanly.
        if (inFlightController) { try { inFlightController.abort(); } catch(e){} }
        isFetching = false;
        if (chart) { chart.destroy(); chart = null; }
        loadChart();
    }

    // Manual Refresh button: refresh devices AND re-fetch the chart WITHOUT
    // destroying the existing chart synchronously. loadChart() builds the new
    // chart from a fresh samples list and replaces atomically — if that fetch
    // fails, the previous chart remains visible.
    async function refreshAll() {
        await refresh();
        startCountdown();
        const samples = await loadChart();
        if (samples) pulseTick();
    }

    function exportChartPNG() {
        if (!chart) return;
        const link = document.createElement('a');
        link.href = chart.toBase64Image('image/png', 1);
        link.download = `ispindel-${document.getElementById('device-select').value || 'chart'}-${Date.now()}.png`;
        link.click();
    }

    /* ============================================================ */
    let modalOpener = null;
    function setModalBackgroundInert(modal, isOpen) {
        const main = document.getElementById('main-content');
        if (!main) return;
        if (isOpen) main.setAttribute('data-modal-open', 'true');
        else main.removeAttribute('data-modal-open');
        [...main.children].forEach(child => {
            if (child !== modal) child.inert = isOpen;
        });
    }
    function openModal(modal, opener) {
        modalOpener = opener || document.activeElement;
        modal.classList.add('open'); modal.setAttribute('aria-hidden', 'false');
        setModalBackgroundInert(modal, true);
        const first = modal.querySelector('input:not([type="hidden"]), button, select, textarea, [tabindex]:not([tabindex="-1"])');
        if (first) first.focus();
    }
    function openEditModal(id, opener) {
        const dev = currentDevices.find(d => d.device_id === id);
        if(!dev) return;
        document.getElementById('edit-device-id').value = id;
        document.getElementById('edit-device-name').value = dev.device_name || '';
        document.getElementById('edit-device-interval').value = dev.expected_interval_sec || 300;
        openModal(document.getElementById('edit-modal'), opener);
    }

    async function saveDevice() {
        const id = document.getElementById('edit-device-id').value;
        const name = document.getElementById('edit-device-name').value;
        const interval = document.getElementById('edit-device-interval').value;

        await fetch(`/api/device/${id}`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({device_name: name, expected_interval_sec: parseInt(interval, 10)})
        });

        closeModal('edit-modal');
        refresh();
    }

    async function openCalibrationModal(opener) {
        const device = document.getElementById('device-select').value;
        if (!device) return alert("Select a device first.");
        document.getElementById('cal-device-id').value = device;
        document.getElementById('cal-status').textContent = "";
        openModal(document.getElementById('cal-modal'), opener);
        try {
            await loadCalibrationHistory(device);
        } catch (error) {
            document.getElementById('cal-status').textContent = `Error: ${error.message}`;
        }
    }

    function calibrationFormula(calibration) {
        const coefficients = calibration.coefficients || [];
        return coefficients.map((value, index) => `${Number(value).toFixed(6)}${index ? `·x^${index}` : ''}`).join(' + ');
    }

    function renderCalibrationHistory(state) {
        const target = document.getElementById('cal-history');
        target.replaceChildren();
        const title = document.createElement('h3');
        title.textContent = 'Calibration history';
        target.appendChild(title);
        if (!state || !state.history || state.history.length === 0) {
            const empty = document.createElement('p');
            empty.textContent = 'No saved calibrations.';
            target.appendChild(empty);
            return;
        }
        state.history.forEach(calibration => {
            const row = document.createElement('div');
            row.className = 'calibration-history-row';
            row.classList.toggle('is-active', Boolean(calibration.is_active));
            row.setAttribute('aria-current', calibration.is_active ? 'true' : 'false');
            const text = document.createElement('span');
            const monotonic = calibration.monotonic ? ', monotonic' : '';
            text.textContent = `${calibration.is_active ? 'Active — ' : ''}${calibration.label}: ${calibrationFormula(calibration)} (R² ${Number(calibration.fit_r2).toFixed(5)}${monotonic})`;
            row.appendChild(text);
            if (!calibration.is_active) {
                const button = document.createElement('button');
                button.type = 'button';
                button.textContent = 'Activate';
                button.setAttribute('aria-label', `Activate calibration ${calibration.label}`);
                button.addEventListener('click', () => activateCalibration(calibration.id));
                row.appendChild(button);
            }
            target.appendChild(row);
        });
    }

    async function loadCalibrationHistory(device) {
        const response = await fetch(`/api/device/${device}/calibration`);
        if (!response.ok) throw new Error(await response.text());
        renderCalibrationHistory(await response.json());
    }

    async function activateCalibration(calibrationId) {
        const device = document.getElementById('cal-device-id').value;
        const status = document.getElementById('cal-status');
        try {
            const response = await fetch(`/api/device/${device}/calibration/${calibrationId}/active`, {method: 'PUT'});
            if (!response.ok) throw new Error(await response.text());
            status.textContent = 'Calibration activated.';
            await loadCalibrationHistory(device);
            await loadChart();
        } catch (error) {
            status.textContent = `Error: ${error.message}`;
        }
    }

    async function saveCalibration() {
        const device = document.getElementById('cal-device-id').value;
        const label = document.getElementById('cal-label').value;
        const order = Number(document.getElementById('cal-order').value);
        const status = document.getElementById('cal-status');
        const points = [];

        const trs = document.querySelectorAll('#cal-points tr');
        trs.forEach(tr => {
            const a = tr.querySelector('.cal-a').value;
            const v = tr.querySelector('.cal-v').value;
            if(a && v) {
                points.push({angle: parseFloat(a), value: parseFloat(v)});
            }
        });

        if (points.length < order + 1) {
            status.textContent = `Need at least ${order + 1} points.`;
            return;
        }

        try {
            const res = await fetch(`/api/device/${device}/calibration`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({label, order, points, activate: false})
            });
            if (!res.ok) throw new Error(await res.text());
            const data = await res.json();
            status.textContent = `Saved inactive ${data.label}: ${calibrationFormula(data)}. Activate only after the stable water-reference check.`;
            await loadCalibrationHistory(device);
            await loadChart();
        } catch (e) {
            status.textContent = `Error: ${e.message}`;
        }
    }

    function closeModal(id) {
        const modal = document.getElementById(id); modal.classList.remove('open'); modal.setAttribute('aria-hidden', 'true');
        setModalBackgroundInert(modal, false);
        if (modalOpener && modalOpener.focus) modalOpener.focus();
        modalOpener = null;
    }

    function trapModalFocus(event) {
        const modal = event.currentTarget;
        if (!modal.classList.contains('open')) return;
        if (event.key === 'Escape') { event.preventDefault(); closeModal(modal.id); return; }
        if (event.key !== 'Tab') return;
        const items = [...modal.querySelectorAll('button, [href], input:not([type="hidden"]), select, textarea, [tabindex]:not([tabindex="-1"])')]
            .filter(el => !el.disabled && !el.hidden && el.tabIndex >= 0);
        if (!items.length) return;
        const first = items[0], last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }

    // ---- Bootstrap ----
    buildMetricToggles();
    document.getElementById('device-select').addEventListener('change', onDeviceOrWindowChange);
    document.getElementById('time-window').addEventListener('change', onDeviceOrWindowChange);
    document.getElementById('gravity-mode').addEventListener('change', loadChart);
    document.getElementById('view-select').addEventListener('change', event => setView(event.target.value));
    document.getElementById('refresh-button').addEventListener('click', refreshAll);
    document.getElementById('calibration-button').addEventListener('click', event => openCalibrationModal(event.currentTarget));
    document.getElementById('export-chart-button').addEventListener('click', exportChartPNG);
    document.getElementById('edit-cancel').addEventListener('click', () => closeModal('edit-modal'));
    document.getElementById('edit-save').addEventListener('click', saveDevice);
    document.getElementById('cal-cancel').addEventListener('click', () => closeModal('cal-modal'));
    document.getElementById('cal-save').addEventListener('click', saveCalibration);
    document.querySelectorAll('.modal').forEach(modal => modal.addEventListener('keydown', trapModalFocus));
    loadSavedView();
    refresh().then(() => {
        startChartPolling();
        startCountdown();
    });
    window.addEventListener('beforeunload', () => {
        stopChartPolling();
        if (chart) { chart.destroy(); chart = null; }
    });

    // Devices section collapsible — persist open/closed state across reloads.
    // Resize the chart after the collapse animation so it fills the new space.
    (function initDevicesCollapse() {
        const d = document.getElementById('devices-collapse');
        if (!d) return;
        const saved = localStorage.getItem('ispindel.devices-open');
        if (saved === 'open') d.open = true;
        else if (saved === 'closed') d.open = false;
        else d.open = true;  // default: open (visible by default)
        d.addEventListener('toggle', () => {
            localStorage.setItem('ispindel.devices-open', d.open ? 'open' : 'closed');
            // 220ms > the CSS chevron transform (150ms) + body padding
            setTimeout(() => { if (chart) chart.resize(); }, 220);
        });
    })();

    /* ============================================================
       Chart resize handle — Pointer Events + keyboard, persisted and clamped.
       ============================================================ */
    (function initChartResize() {
        const handle = document.getElementById('chartResizeHandle');
        const container = document.getElementById('chartContainer');
        if (!handle || !container) return;

        const MIN_PX = 250;
        const KEY_STEP_PX = 16;
        const SHIFT_STEP_PX = 64;

        function chartResizeBounds() {
            return { min: MIN_PX, max: Math.max(600, Math.floor(window.innerHeight * 0.90)) };
        }
        function clamp(v) {
            const bounds = chartResizeBounds();
            return Math.max(bounds.min, Math.min(bounds.max, Math.round(v)));
        }
        function syncAria(px) {
            const bounds = chartResizeBounds();
            handle.setAttribute('aria-valuemin', String(bounds.min));
            handle.setAttribute('aria-valuemax', String(bounds.max));
            handle.setAttribute('aria-valuenow', String(Math.round(px)));
        }
        function applyHeight(px, persist = true) {
            const next = clamp(px);
            container.style.setProperty('--chart-height', next + 'px');
            container.style.height = next + 'px';
            syncAria(next);
            if (chart) chart.resize();
            if (persist) localStorage.setItem('ispindel.chart-height', String(next));
        }
        function resetHeight() {
            localStorage.removeItem('ispindel.chart-height');
            container.style.removeProperty('height');
            container.style.removeProperty('--chart-height');
            requestAnimationFrame(() => {
                syncAria(clamp(container.getBoundingClientRect().height));
                if (chart) chart.resize();
            });
        }

        const savedPx = Number.parseInt(localStorage.getItem('ispindel.chart-height') || '', 10);
        if (Number.isFinite(savedPx)) applyHeight(savedPx, true);
        else syncAria(clamp(container.getBoundingClientRect().height));

        let activePointer = null;
        let startY = 0;
        let startH = 0;
        handle.addEventListener('pointerdown', (e) => {
            if (activePointer !== null) return;
            activePointer = e.pointerId;
            startY = e.clientY;
            startH = container.getBoundingClientRect().height;
            handle.classList.add('is-dragging');
            handle.setPointerCapture(e.pointerId);
            e.preventDefault();
        });
        handle.addEventListener('pointermove', (e) => {
            if (e.pointerId !== activePointer) return;
            applyHeight(startH + (e.clientY - startY), false);
            e.preventDefault();
        });
        function finishPointer(e) {
            if (e.pointerId !== activePointer) return;
            applyHeight(container.getBoundingClientRect().height, true);
            if (handle.hasPointerCapture(e.pointerId)) handle.releasePointerCapture(e.pointerId);
            activePointer = null;
            handle.classList.remove('is-dragging');
        }
        handle.addEventListener('pointerup', finishPointer);
        handle.addEventListener('pointercancel', finishPointer);

        handle.addEventListener('keydown', (e) => {
            const cur = container.getBoundingClientRect().height;
            let next = cur;
            const step = e.shiftKey ? SHIFT_STEP_PX : KEY_STEP_PX;
            switch (e.key) {
                case 'ArrowUp':   next = cur + step; break;
                case 'ArrowDown': next = cur - step; break;
                case 'PageUp':    next = cur + 100; break;
                case 'PageDown':  next = cur - 100; break;
                case 'Home':      next = chartResizeBounds().max; break;
                case 'End':       next = chartResizeBounds().min; break;
                case 'Escape':    e.preventDefault(); resetHeight(); return;
                default: return;
            }
            e.preventDefault();
            applyHeight(next);
        });
        handle.addEventListener('dblclick', resetHeight);

        window.addEventListener('resize', () => {
            const saved = Number.parseInt(localStorage.getItem('ispindel.chart-height') || '', 10);
            if (Number.isFinite(saved)) applyHeight(saved, true);
            else syncAria(clamp(container.getBoundingClientRect().height));
        });

        const resizeObserver = new ResizeObserver(() => {
            if (chart && chart.canvas && chart.canvas.isConnected) chart.resize();
        });
        resizeObserver.observe(container);
    })();
