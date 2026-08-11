(function () {
  const state = {
    status: null,
    ports: [],
    configs: [],
    sessions: [],
    history: [],
    eventSource: null,
    viewedSessionId: null,
    viewedConfigName: null,
    persistToDb: false,
    logDumpActive: false,
    logDumpPath: null,
    logDumpSampleCount: 0,
    logDumpMaxSamples: null,
    systemUser: null,
    powerStateActive: false,
    formDirty: {
      port: false,
      config: false,
      sampleCount: false,
      delayMs: false,
      commandInterval: false,
      dumpPath: false,
    },
  };

  const MAX_HISTORY_POINTS = 180;
  const RAIL_COLORS = ['#38bdf8', '#22c55e', '#f59e0b', '#a78bfa', '#f472b6', '#fb7185', '#14b8a6', '#f97316'];
  let chartTooltip = null;
  let chartTooltipGuardsInstalled = false;

  // UI3: SSE reconnect state
  let sseRetryDelay = 1000;
  let sseRetryTimer = null;

  const elements = {
    portSelect: document.getElementById('port-select'),
    refreshPorts: document.getElementById('refresh-ports'),
    syncDashboard: document.getElementById('sync-dashboard'),
    configSelect: document.getElementById('config-select'),
    sampleCount: document.getElementById('sample-count'),
    delayMs: document.getElementById('delay-ms'),
    commandInterval: document.getElementById('command-interval'),
    startMonitoring: document.getElementById('start-monitoring'),
    stopMonitoring: document.getElementById('stop-monitoring'),
    dbCaptureToggle: document.getElementById('db-capture-toggle'),
    messageStrip: document.getElementById('message-strip'),
    statusPill: document.getElementById('monitor-status-pill'),
    statusDetail: document.getElementById('status-detail'),
    exportLink: document.getElementById('export-link'),
    statTotalPower: document.getElementById('stat-total-power'),
    statPowerTrend: document.getElementById('stat-power-trend'),
    statRailCount: document.getElementById('stat-rail-count'),
    statConfig: document.getElementById('stat-config'),
    totalChart: document.getElementById('total-power-chart'),
    railChartGrid: document.getElementById('rail-chart-grid'),
    sessionsList: document.getElementById('sessions-list'),
    dumpFilePath: document.getElementById('dump-file-path'),
    dumpSampleCount: document.getElementById('dump-sample-count'),
    dumpToggle: document.getElementById('dump-toggle'),
    dumpStatusStrip: document.getElementById('dump-status-strip'),
    dumpActivePath: document.getElementById('dump-active-path'),
    dumpProgress: document.getElementById('dump-progress'),
    powerStateToggle: document.getElementById('power-state-toggle'),
  };

  async function fetchJson(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let message = `Request failed with ${response.status}`;
      try {
        const payload = await response.json();
        if (payload && payload.error) {
          message = payload.error;
        }
      } catch (error) {
        // Ignore parse errors and keep default message.
      }
      throw new Error(message);
    }
    return response.json();
  }

  function setMessage(message, isError = false) {
    elements.messageStrip.textContent = message;
    elements.messageStrip.classList.toggle('error', isError);
  }

  function updateDbCaptureButton() {
    const btn = elements.dbCaptureToggle;
    btn.textContent = state.persistToDb ? 'DB: On' : 'DB: Off';
    btn.classList.toggle('db-capture-active', state.persistToDb);
    btn.disabled = Boolean(state.status?.is_monitoring);
  }

  function powerStateFilename() {
    return state.powerStateActive ? 'power_readings_active.txt' : 'power_readings_idle.txt';
  }

  function getDefaultDumpPath(configId) {
    const user = state.systemUser || 'user';
    const soc = configId || 'unknown';
    return `/home/${user}/nvme/adas/PowerOptimizationWorkarea/automation/data/${soc}/${powerStateFilename()}`;
  }

  function updatePowerStateButton() {
    const btn = elements.powerStateToggle;
    btn.textContent = state.powerStateActive ? 'Power State: Active' : 'Power State: Idle';
    btn.classList.toggle('power-state-active', state.powerStateActive);
  }

  function applyPowerStateToPath() {
    const currentPath = elements.dumpFilePath.value.trim();
    const filename = powerStateFilename();
    const lastSlash = currentPath.lastIndexOf('/');
    const dir = lastSlash >= 0 ? currentPath.slice(0, lastSlash + 1) : '';
    elements.dumpFilePath.value = dir + filename;
  }

  function handlePowerStateToggle() {
    state.powerStateActive = !state.powerStateActive;
    updatePowerStateButton();
    applyPowerStateToPath();
  }

  function updateDumpUI() {
    const active = state.logDumpActive;
    elements.dumpToggle.textContent = active ? 'Stop Dumping' : 'Start Dumping';
    elements.dumpToggle.classList.toggle('danger-button', active);
    elements.dumpToggle.classList.toggle('primary-button', !active);
    elements.dumpStatusStrip.hidden = !active;
    if (active) {
      elements.dumpActivePath.textContent = state.logDumpPath || '';
      if (state.logDumpMaxSamples) {
        elements.dumpProgress.textContent = `${state.logDumpSampleCount} / ${state.logDumpMaxSamples}`;
        elements.dumpProgress.hidden = false;
      } else {
        elements.dumpProgress.hidden = true;
      }
    }
  }

  async function handleDumpToggle() {
    if (state.logDumpActive) {
      await fetchJson('/api/log-dump', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'stop' }),
      });
      state.logDumpActive = false;
      state.logDumpPath = null;
      updateDumpUI();
      setMessage('Log dump stopped.');
    } else {
      const filePath = elements.dumpFilePath.value.trim();
      if (!filePath) {
        setMessage('Enter a file path before starting dump.', true);
        return;
      }
      const maxSamples = Math.max(1, Math.floor(Number(elements.dumpSampleCount.value) || 10));
      const result = await fetchJson('/api/log-dump', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'start', file_path: filePath, max_samples: maxSamples }),
      });
      state.logDumpActive = true;
      state.logDumpPath = result.log_dump_path;
      state.logDumpSampleCount = 0;
      state.logDumpMaxSamples = maxSamples;
      elements.dumpFilePath.value = result.log_dump_path;
      markDirty('dumpPath');
      updateDumpUI();
      setMessage(`Dumping to: ${result.log_dump_path}`);
    }
  }

  function formatNumber(value, digits = 1) {
    return Number.isFinite(value) ? value.toFixed(digits) : '-';
  }

  function formatTimestamp(ts) {
    if (!ts) {
      return 'Awaiting data';
    }
    return new Date(ts).toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  }

  function formatSessionTime(ts) {
    if (!ts) {
      return 'Active';
    }
    return new Date(ts).toLocaleString([], {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  function fillSelect(select, items, getValue, getLabel, placeholder) {
    const currentValue = select.value;
    select.innerHTML = '';

    if (placeholder) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = placeholder;
      select.appendChild(option);
    }

    items.forEach((item) => {
      const option = document.createElement('option');
      option.value = getValue(item);
      option.textContent = getLabel(item);
      select.appendChild(option);
    });

    if (currentValue && Array.from(select.options).some((option) => option.value === currentValue)) {
      select.value = currentValue;
    }
  }

  function syncControlValue(element, value, dirtyKey, options = {}) {
    const { force = false } = options;
    if (value === undefined || value === null || value === '') {
      return;
    }
    if (!force && (state.formDirty[dirtyKey] || document.activeElement === element)) {
      return;
    }
    element.value = String(value);
  }

  function markDirty(dirtyKey, isDirty = true) {
    state.formDirty[dirtyKey] = isDirty;
  }

  function normalizeRailKey(value) {
    return String(value || '').trim().toLowerCase();
  }

  function currentIgnoredRails() {
    const config = state.configs.find((item) => (
      item.name === state.viewedConfigName
      || item.config_id === state.viewedConfigName
      || item.config_id === state.status?.active_config_id
    ));
    const ignoredRails = new Set(
      (config?.rails || [])
        .filter((rail) => rail.ignore_for_soc_total)
        .flatMap((rail) => [rail.name, ...(rail.aliases || [])])
        .map((name) => normalizeRailKey(name))
    );
    return ignoredRails;
  }

  function currentConfigRailMap() {
    const config = state.configs.find((item) => (
      item.name === state.viewedConfigName
      || item.config_id === state.viewedConfigName
      || item.config_id === state.status?.active_config_id
    ));
    const entries = [];
    (config?.rails || []).forEach((rail) => {
      [rail.name, ...(rail.aliases || [])].forEach((name) => {
        entries.push([normalizeRailKey(name), rail]);
      });
    });
    return new Map(entries);
  }

  function computePoint(ts, readings) {
    const rails = {};
    let totalPower = 0;
    const ignoredRails = currentIgnoredRails();
    const configRails = currentConfigRailMap();

    readings.forEach((reading) => {
      const matchedRail = configRails.get(normalizeRailKey(reading.rail));
      const name = matchedRail?.name || reading.rail || 'unknown';
      const voltage = Number.isFinite(reading.display_voltage_v) ? reading.display_voltage_v : reading.voltage_v;
      const current = Number.isFinite(reading.actual_current_ma) ? reading.actual_current_ma : reading.current_ma;
      const power = Number.isFinite(reading.actual_power_mw) ? reading.actual_power_mw : reading.power_mw;
      rails[name] = {
        voltage_v: voltage,
        current_ma: current,
        power_mw: power,
        group: reading.group || matchedRail?.group || null,
        calculation_mode: reading.calculation_mode || matchedRail?.calculation_mode || null,
        ignore_for_soc_total: Boolean(
          reading.ignore_for_soc_total
          || matchedRail?.ignore_for_soc_total
          || ignoredRails.has(normalizeRailKey(name))
        ),
      };
      if (!ignoredRails.has(normalizeRailKey(name))) {
        totalPower += power || 0;
      }
    });

    return { ts, rails, totalPower };
  }

  // UI5: replaced sort-on-every-push with ordered insertion + shift trim
  function mergePoint(point) {
    const idx = state.history.findIndex((item) => item.ts === point.ts);
    if (idx !== -1) {
      state.history[idx].rails = point.rails;
      state.history[idx].totalPower = point.totalPower;
    } else {
      state.history.push(point);
      // SSE events arrive in chronological order so no sort needed
      if (state.history.length > MAX_HISTORY_POINTS) {
        state.history.shift();
      }
    }
  }

  function allRailNames() {
    const names = new Set();
    state.history.forEach((point) => {
      Object.keys(point.rails).forEach((rail) => names.add(rail));
    });
    return Array.from(names).sort();
  }

  function latestPoint() {
    return state.history[state.history.length - 1] || null;
  }

  function previousPoint() {
    return state.history[state.history.length - 2] || null;
  }

  function trendClass(delta) {
    if (delta > 0.5) {
      return 'trend-up';
    }
    if (delta < -0.5) {
      return 'trend-down';
    }
    return 'trend-flat';
  }

  function trendLabel(delta, unit) {
    if (!Number.isFinite(delta)) {
      return 'No trend yet';
    }
    const direction = delta > 0.5 ? 'up' : delta < -0.5 ? 'down' : 'flat';
    return `${direction} ${Math.abs(delta).toFixed(1)} ${unit}`;
  }

  function getChartTooltip() {
    if (!chartTooltip) {
      chartTooltip = document.createElement('div');
      chartTooltip.className = 'chart-tooltip';
      chartTooltip.hidden = true;
      document.body.appendChild(chartTooltip);
    }
    return chartTooltip;
  }

  function hideChartTooltip() {
    if (chartTooltip) {
      chartTooltip.hidden = true;
    }
  }

  function installChartTooltipGuards() {
    if (chartTooltipGuardsInstalled) {
      return;
    }
    document.addEventListener('pointermove', (event) => {
      const target = event.target;
      if (!(target instanceof HTMLCanvasElement) || target.dataset.chartCanvas !== 'true') {
        hideChartTooltip();
      }
    });
    window.addEventListener('scroll', hideChartTooltip, true);
    window.addEventListener('blur', hideChartTooltip);
    chartTooltipGuardsInstalled = true;
  }

  function showChartTooltip(event, point, unitLabel) {
    const tooltip = getChartTooltip();
    tooltip.innerHTML = `
      <strong>${point.label || ''}</strong>
      <span>${formatNumber(point.value)} ${unitLabel}</span>
    `;
    tooltip.hidden = false;

    // Force layout so getBoundingClientRect reflects actual size
    tooltip.style.left = '-9999px';
    tooltip.style.top = '-9999px';
    const rect = tooltip.getBoundingClientRect();

    const offset = 14;
    let left = event.clientX + offset;
    let top = event.clientY - rect.height - offset;

    // UI6: clamp horizontally
    if (left + rect.width > window.innerWidth - 8) {
      left = event.clientX - rect.width - offset;
    }
    // UI6: clamp vertically (top and bottom)
    if (top < 8) {
      top = event.clientY + offset;
    }
    if (top + rect.height > window.innerHeight - 8) {
      top = window.innerHeight - rect.height - 8;
    }

    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  }

  function railColor(name, index) {
    const fallback = RAIL_COLORS[index % RAIL_COLORS.length];
    let hash = 0;
    for (let i = 0; i < name.length; i += 1) {
      hash = ((hash << 5) - hash) + name.charCodeAt(i);
      hash |= 0;
    }
    return RAIL_COLORS[Math.abs(hash) % RAIL_COLORS.length] || fallback;
  }

  async function loadPorts() {
    state.ports = await fetchJson('/api/ports');
    fillSelect(
      elements.portSelect,
      state.ports,
      (item) => item.device,
      (item) => `${item.device}${item.description ? ` • ${item.description}` : ''}`,
      'Select a UART port'
    );

    syncControlValue(elements.portSelect, state.status?.selected_port, 'port');
  }

  async function loadConfigs() {
    state.configs = await fetchJson('/api/configs');
    fillSelect(
      elements.configSelect,
      state.configs,
      (item) => item.config_id || item.name,
      (item) => `${item.name} • ${item.rails ? item.rails.length : 0} rails`,
      'Select a config'
    );

    syncControlValue(elements.configSelect, state.status?.active_config_id, 'config');
  }

  async function loadStatus() {
    state.status = await fetchJson('/api/status');
    syncControlValue(elements.sampleCount, state.status.samples_per_command, 'sampleCount');
    syncControlValue(elements.delayMs, state.status.delay_ms, 'delayMs');
    syncControlValue(elements.commandInterval, state.status.command_interval, 'commandInterval');
    state.persistToDb = state.status.persist_to_db ?? false;
    state.logDumpActive = state.status.log_dump_active ?? false;
    state.logDumpPath = state.status.log_dump_path ?? null;
    state.logDumpSampleCount = state.status.log_dump_sample_count ?? 0;
    state.logDumpMaxSamples = state.status.log_dump_max_samples ?? null;
    state.systemUser = state.status.system_user ?? null;
    updateDbCaptureButton();
    updateDumpUI();
    if (state.status.latest_readings?.length && state.status.last_update_ts) {
      mergePoint(computePoint(state.status.last_update_ts, state.status.latest_readings));
    }
  }

  async function loadSessions() {
    state.sessions = await fetchJson('/api/sessions');
  }

  async function manualSync() {
    await loadStatus();
    await Promise.all([loadPorts(), loadConfigs(), loadSessions()]);
    if (state.status?.active_session_id && !state.status?.is_monitoring) {
      await loadHistoryForSession(state.status.active_session_id);
    }
    render();
    setMessage('Dashboard synced.');
  }

  async function loadHistoryForSession(sessionId) {
    if (!sessionId) {
      return;
    }
    const rows = [];
    const batchSize = 1000;
    let offset = 0;
    const session = state.sessions.find((item) => item.id === Number(sessionId));
    state.viewedConfigName = session?.config_name || state.status?.active_config || null;

    while (true) {
      const batch = await fetchJson(
        `/api/samples?session_id=${sessionId}&limit=${batchSize}&offset=${offset}&order=asc`
      );
      rows.push(...batch);
      if (batch.length < batchSize) {
        break;
      }
      offset += batchSize;
    }

    const groups = new Map();

    rows.forEach((row) => {
      const key = row.ts;
      if (!groups.has(key)) {
        groups.set(key, []);
      }
      groups.get(key).push(row);
    });

    state.history = Array.from(groups.entries())
      .sort((left, right) => new Date(left[0]) - new Date(right[0]))
      .slice(-MAX_HISTORY_POINTS)
      .map(([ts, readings]) => computePoint(ts, readings));
    state.viewedSessionId = Number(sessionId);
  }

  function renderStats() {
    const current = latestPoint();
    const previous = previousPoint();
    const totalPower = current ? current.totalPower : state.status?.total_power_mw || 0;
    const totalDelta = current && previous ? current.totalPower - previous.totalPower : NaN;
    const railCount = current ? Object.keys(current.rails).length : state.status?.rail_count || 0;
    const monitoring = Boolean(state.status?.is_monitoring);

    elements.statTotalPower.textContent = `${formatNumber(totalPower)} mW`;
    elements.statPowerTrend.textContent = trendLabel(totalDelta, 'mW');
    elements.statPowerTrend.className = `stat-meta ${trendClass(totalDelta)}`;
    elements.statRailCount.textContent = String(railCount);
    elements.statConfig.textContent = state.status?.active_config || 'No config active';

    elements.statusPill.textContent = monitoring ? 'Monitoring live' : (state.status?.last_error ? 'Link error' : 'Idle');
    elements.statusPill.classList.toggle('live', monitoring);
    elements.statusPill.classList.toggle('error', Boolean(state.status?.last_error));
    elements.statusDetail.textContent = state.status?.last_error
      ? state.status.last_error
      : monitoring
        ? `Streaming from ${state.status.selected_port || 'selected UART'}`
        : 'Waiting for UART selection.';

    if (state.status?.active_session_id) {
      elements.exportLink.hidden = false;
      elements.exportLink.href = `/api/export.csv?session_id=${state.status.active_session_id}`;
    } else {
      elements.exportLink.hidden = true;
    }

    elements.startMonitoring.disabled = monitoring;
    elements.stopMonitoring.disabled = !monitoring;
  }

  // S1: replaced innerHTML with DOM API to prevent XSS via config_name or other server data
  function renderSessions() {
    elements.sessionsList.innerHTML = '';
    if (!state.sessions.length) {
      const p = document.createElement('p');
      p.textContent = 'No sessions recorded yet.';
      elements.sessionsList.appendChild(p);
      return;
    }

    state.sessions.slice(0, 8).forEach((session) => {
      const item = document.createElement('article');
      item.className = 'session-item';

      const h3 = document.createElement('h3');
      h3.textContent = `Session #${session.id}`;
      item.appendChild(h3);

      const pConfig = document.createElement('p');
      pConfig.textContent = session.config_name;
      item.appendChild(pConfig);

      const pTime = document.createElement('p');
      pTime.textContent = `${formatSessionTime(session.started_at)} to ${formatSessionTime(session.ended_at)}`;
      item.appendChild(pTime);

      const pLinks = document.createElement('p');

      const exportA = document.createElement('a');
      exportA.href = `/api/export.csv?session_id=${session.id}`;
      exportA.textContent = 'Export CSV';
      pLinks.appendChild(exportA);

      pLinks.appendChild(document.createTextNode(' • '));

      const isActive = state.status?.active_session_id === session.id;
      const historyA = document.createElement('a');
      historyA.href = '#';
      historyA.dataset.sessionId = String(session.id);
      historyA.textContent = isActive ? 'Active session' : 'View history';
      pLinks.appendChild(historyA);

      if (!isActive) {
        pLinks.appendChild(document.createTextNode(' • '));
        const deleteA = document.createElement('a');
        deleteA.href = '#';
        deleteA.className = 'session-delete-link';
        deleteA.dataset.deleteSessionId = String(session.id);
        deleteA.textContent = 'Delete';
        pLinks.appendChild(deleteA);
      }

      item.appendChild(pLinks);
      elements.sessionsList.appendChild(item);
    });

    elements.sessionsList.querySelectorAll('[data-session-id]').forEach((link) => {
      link.addEventListener('click', async (event) => {
        event.preventDefault();
        const sessionId = event.currentTarget.getAttribute('data-session-id');
        await loadHistoryForSession(sessionId);
        render();
        setMessage(`Loaded history for session #${sessionId}.`);
      });
    });

    elements.sessionsList.querySelectorAll('[data-delete-session-id]').forEach((link) => {
      link.addEventListener('click', async (event) => {
        event.preventDefault();
        const sessionId = event.currentTarget.getAttribute('data-delete-session-id');
        if (!confirm(`Delete session #${sessionId} and all its measurements? This cannot be undone.`)) {
          return;
        }
        try {
          await fetchJson(`/api/sessions/${sessionId}`, { method: 'DELETE' });
          state.sessions = state.sessions.filter((s) => s.id !== Number(sessionId));
          if (state.viewedSessionId === Number(sessionId)) {
            state.viewedSessionId = null;
            state.history = [];
          }
          render();
          setMessage(`Session #${sessionId} deleted.`);
        } catch (error) {
          setMessage(error.message, true);
        }
      });
    });
  }

  // UI2: compute decimal places based on the value range so sub-mW readings show correctly
  function yAxisDecimals(range) {
    if (range < 1) return 2;
    if (range < 10) return 1;
    return 0;
  }

  function drawLineChart(canvas, values, labels, lineColor, fillColor, unitLabel, options = {}) {
    const context = canvas.getContext('2d');
    canvas.dataset.chartCanvas = 'true';
    const width = Math.round(canvas.clientWidth);
    if (!canvas.dataset.logicalHeight) {
      canvas.dataset.logicalHeight = String(
        Number(canvas.getAttribute('height')) || Math.round(canvas.getBoundingClientRect().height) || 240
      );
    }
    const height = Number(canvas.dataset.logicalHeight);
    const numericValues = values.filter((value) => Number.isFinite(value));
    const leftPad = 52;
    const rightPad = 20;
    const topPad = 36;
    const bottomPad = 34;
    const plotHeight = height - topPad - bottomPad;
    const plotWidth = width - leftPad - rightPad;
    const xTickCount = Math.min(4, Math.max(labels.length - 1, 1));
    const yTickCount = 4;
    const showXAxisLabels = options.showXAxisLabels !== false;
    let activePoint = null;

    canvas.style.height = `${height}px`;

    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }

    if (!numericValues.length) {
      context.setTransform(1, 0, 0, 1, 0, 0);
      context.clearRect(0, 0, canvas.width, canvas.height);
      context.fillStyle = 'rgba(148, 163, 184, 0.82)';
      context.font = '14px "IBM Plex Sans", sans-serif';
      context.fillText('No samples yet', 24, height / 2);
      return;
    }

    const rawMaxValue = Math.max(...numericValues, 1);
    const rawMinValue = options.zeroBaseline ? 0 : Math.min(...numericValues, 0);
    const paddedMaxValue = rawMaxValue + Math.max(rawMaxValue * 0.08, 1);
    const minRange = options.zeroBaseline
      ? Math.max(rawMaxValue * 0.2, 10)
      : Math.max((rawMaxValue - rawMinValue) * 0.25, 1);
    const maxValue = Math.max(paddedMaxValue, rawMinValue + minRange);
    const minValue = rawMinValue;
    const range = Math.max(maxValue - minValue, minRange);
    const xStep = values.length > 1 ? plotWidth / (values.length - 1) : 0;
    const yValueStep = range / yTickCount;
    const decimals = yAxisDecimals(range); // UI2

    const points = values.map((value, index) => ({
      x: leftPad + xStep * index,
      y: Number.isFinite(value) ? height - bottomPad - ((value - minValue) / range) * plotHeight : null,
      value,
      label: labels[index] || '',
    }));
    const hoverPoints = points.filter((point) => Number.isFinite(point.y));

    // UI1: renderStatic draws everything except the hover indicator
    // staticSnapshot stores pixel data so mousemove can restore without full repaint
    let staticSnapshot = null;

    const renderStatic = () => {
      context.setTransform(1, 0, 0, 1, 0, 0);
      context.clearRect(0, 0, canvas.width, canvas.height);
      context.font = '11px "IBM Plex Sans", sans-serif';

      context.strokeStyle = 'rgba(148, 163, 184, 0.14)';
      context.lineWidth = 1;
      for (let i = 0; i <= yTickCount; i += 1) {
        const y = topPad + (plotHeight / yTickCount) * i;
        context.beginPath();
        context.moveTo(leftPad, y);
        context.lineTo(width - rightPad, y);
        context.stroke();
      }

      context.fillStyle = 'rgba(148, 163, 184, 0.9)';
      context.textAlign = 'right';
      for (let i = 0; i <= yTickCount; i += 1) {
        const tickValue = minValue + (yValueStep * (yTickCount - i));
        const y = topPad + (plotHeight / yTickCount) * i;
        // UI2: use dynamic decimal precision
        context.fillText(`${tickValue.toFixed(decimals)} ${unitLabel}`, leftPad - 6, y + 4);
      }

      if (showXAxisLabels) {
        context.textAlign = 'center';
        for (let i = 0; i <= xTickCount; i += 1) {
          const labelIndex = Math.min(
            labels.length - 1,
            Math.round((labels.length - 1) * (i / xTickCount))
          );
          const x = leftPad + plotWidth * (i / xTickCount);
          context.fillText(labels[labelIndex] || '', x, height - 10);
        }
      }

      context.beginPath();
      let started = false;
      points.forEach((point, index) => {
        if (!Number.isFinite(point.y)) {
          started = false;
          return;
        }
        if (!started || index === 0) {
          context.moveTo(point.x, point.y);
          started = true;
        } else {
          context.lineTo(point.x, point.y);
        }
      });
      context.lineWidth = 3;
      context.strokeStyle = lineColor;
      context.stroke();

      if (numericValues.length === values.length) {
        context.beginPath();
        points.forEach((point, index) => {
          if (index === 0) {
            context.moveTo(point.x, point.y);
          } else {
            context.lineTo(point.x, point.y);
          }
        });
        context.lineTo(width - rightPad, height - bottomPad);
        context.lineTo(leftPad, height - bottomPad);
        context.closePath();
        context.fillStyle = fillColor;
        context.fill();
      }

      context.fillStyle = 'rgba(203, 213, 225, 0.9)';
      context.textAlign = 'right';
      context.fillText(`${rawMaxValue.toFixed(decimals)} ${unitLabel}`, width - rightPad, 18);
      context.textAlign = 'left';

      // UI1: capture static pixels so hover repaint only draws the indicator layer
      staticSnapshot = context.getImageData(0, 0, canvas.width, canvas.height);
    };

    // UI1: renderHover restores static snapshot then draws only the hover indicator
    const renderHover = () => {
      if (!activePoint) return;
      if (staticSnapshot) {
        context.putImageData(staticSnapshot, 0, 0);
      }

      context.strokeStyle = 'rgba(226, 232, 240, 0.28)';
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(activePoint.x, topPad);
      context.lineTo(activePoint.x, height - bottomPad);
      context.stroke();

      context.fillStyle = lineColor;
      context.beginPath();
      context.arc(activePoint.x, activePoint.y, 4.5, 0, Math.PI * 2);
      context.fill();

      context.strokeStyle = 'rgba(241, 245, 249, 0.95)';
      context.lineWidth = 2;
      context.beginPath();
      context.arc(activePoint.x, activePoint.y, 7, 0, Math.PI * 2);
      context.stroke();
    };

    renderStatic();

    canvas.onmousemove = (event) => {
      if (!hoverPoints.length) {
        hideChartTooltip();
        return;
      }
      const rect = canvas.getBoundingClientRect();
      const mouseX = event.clientX - rect.left;
      let nearest = hoverPoints[0];
      let nearestDistance = Math.abs(mouseX - nearest.x);

      hoverPoints.forEach((point) => {
        const distance = Math.abs(mouseX - point.x);
        if (distance < nearestDistance) {
          nearest = point;
          nearestDistance = distance;
        }
      });

      // UI1: only repaint hover layer, not the full chart
      activePoint = nearest;
      renderHover();
      showChartTooltip(event, nearest, unitLabel);
    };
    canvas.onmouseleave = () => {
      activePoint = null;
      if (staticSnapshot) {
        context.putImageData(staticSnapshot, 0, 0);
      }
      hideChartTooltip();
    };
  }

  function renderTotalChart() {
    const labels = state.history.map((point) => formatTimestamp(point.ts));
    const totalValues = state.history.map((point) => point.totalPower);
    drawLineChart(
      elements.totalChart,
      totalValues,
      labels,
      '#38bdf8',
      'rgba(56, 189, 248, 0.16)',
      'mW',
      { zeroBaseline: true }
    );
  }

  function renderRailCharts() {
    const railNames = allRailNames();
    const current = latestPoint();
    const previous = previousPoint();
    const previousRails = previous ? previous.rails : {};
    const labels = state.history.map((point) => formatTimestamp(point.ts));

    elements.railChartGrid.innerHTML = '';
    if (!railNames.length) {
      elements.railChartGrid.innerHTML = '<p>No rail history yet. Start monitoring to populate individual rail charts.</p>';
      return;
    }

    const chartDefs = [];
    const fragment = document.createDocumentFragment();

    railNames.forEach((railName, index) => {
      const latest = current?.rails[railName];
      const previousPower = previousRails[railName]?.power_mw;
      const latestPower = Number.isFinite(latest?.power_mw) ? latest.power_mw : NaN;
      const latestVoltageMv = Number.isFinite(latest?.voltage_v) ? latest.voltage_v * 1000 : NaN;
      const latestCurrentMa = Number.isFinite(latest?.current_ma) ? latest.current_ma : NaN;
      const excludedFromTotal = Boolean(latest?.ignore_for_soc_total);
      const delta = Number.isFinite(previousPower) ? latestPower - previousPower : NaN;
      const color = railColor(railName, index);
      const values = state.history.map((point) => {
        const power = point.rails[railName]?.power_mw;
        return Number.isFinite(power) ? power : null;
      });

      const card = document.createElement('article');
      card.className = 'rail-chart-card';
      card.innerHTML = `
        <div class="rail-chart-heading">
          <h3>${railName}</h3>
          ${excludedFromTotal ? '<span class="rail-scope-badge-excluded">Excluded</span>' : '<span class="rail-scope-badge-included">Included</span>'}
        </div>
        <strong>${formatNumber(latestPower)} mW</strong>
        <div class="rail-chart-meta">
          <span>${formatNumber(latestVoltageMv, 0)} mV</span>
          <span>${formatNumber(latestCurrentMa)} mA</span>
        </div>
        <p class="${trendClass(delta)}">${trendLabel(delta, 'mW')}</p>
        <canvas height="132"></canvas>
      `;
      fragment.appendChild(card);

      const canvas = card.querySelector('canvas');
      chartDefs.push({
        canvas,
        values,
        labels,
        color,
        railName,
      });
    });

    elements.railChartGrid.appendChild(fragment);
    window.requestAnimationFrame(() => {
      chartDefs.forEach(({ canvas, values, labels, color }) => {
        drawLineChart(canvas, values, labels, color, `${color}22`, 'mW', { showXAxisLabels: false });
      });
    });
  }

  function renderCharts() {
    renderTotalChart();
    renderRailCharts();
  }

  function preserveScrollPosition(callback) {
    const scrollX = window.scrollX;
    const scrollY = window.scrollY;
    callback();
    window.requestAnimationFrame(() => {
      window.scrollTo(scrollX, scrollY);
    });
  }

  function render() {
    preserveScrollPosition(() => {
      hideChartTooltip();
      if (state.status?.selected_port && Array.from(elements.portSelect.options).some((option) => option.value === state.status.selected_port)) {
        syncControlValue(elements.portSelect, state.status.selected_port, 'port');
      }
      if (state.status?.active_config_id && Array.from(elements.configSelect.options).some((option) => option.value === state.status.active_config_id)) {
        syncControlValue(elements.configSelect, state.status.active_config_id, 'config');
      }
      renderStats();
      renderSessions();
      renderCharts();
    });
  }

  function applyStreamPayload(payload) {
    if (payload && 'log_dump_active' in payload) {
      if (state.logDumpActive && !payload.log_dump_active) {
        state.logDumpActive = false;
        state.logDumpPath = null;
        state.logDumpSampleCount = payload.log_dump_sample_count ?? state.logDumpSampleCount;
        state.logDumpMaxSamples = payload.log_dump_max_samples ?? state.logDumpMaxSamples;
        updateDumpUI();
        setMessage('Log dump completed — sample limit reached.');
      } else if (state.logDumpActive) {
        state.logDumpSampleCount = payload.log_dump_sample_count ?? state.logDumpSampleCount;
        updateDumpUI();
      }
    }

    if (!payload || !Array.isArray(payload.readings) || payload.readings.length === 0) {
      if (payload?.error) {
        setMessage(payload.error, true);
      }
      render();
      return;
    }

    if (state.viewedSessionId && state.status?.active_session_id && state.viewedSessionId !== state.status.active_session_id) {
      return;
    }

    mergePoint(computePoint(payload.ts, payload.readings));
    render();
  }

  // UI3: SSE wrapper with exponential backoff reconnect
  function ensureStream() {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    if (sseRetryTimer) {
      clearTimeout(sseRetryTimer);
      sseRetryTimer = null;
    }

    const es = new EventSource('/api/stream');
    state.eventSource = es;

    es.onopen = () => {
      sseRetryDelay = 1000; // reset backoff on successful connect
    };

    es.onmessage = (event) => {
      const payload = JSON.parse(event.data);
      applyStreamPayload(payload);
    };

    es.onerror = () => {
      es.close();
      if (state.eventSource === es) {
        state.eventSource = null;
      }
      setMessage(`Live stream disconnected. Reconnecting in ${Math.round(sseRetryDelay / 1000)}s…`, true);
      sseRetryTimer = setTimeout(() => {
        sseRetryDelay = Math.min(sseRetryDelay * 2, 30000); // cap at 30s
        ensureStream();
      }, sseRetryDelay);
    };
  }

  // UI4: close EventSource on page unload to release the HTTP connection
  window.addEventListener('beforeunload', () => {
    if (sseRetryTimer) clearTimeout(sseRetryTimer);
    state.eventSource?.close();
  });

  async function handlePortRefresh() {
    await loadPorts();
    render();
    setMessage('UART list refreshed.');
  }

  async function handleStartMonitoring() {
    const port = elements.portSelect.value;
    const configName = elements.configSelect.value;
    const samples = Number(elements.sampleCount.value);
    const delay = Number(elements.delayMs.value);
    const commandInterval = Number(elements.commandInterval.value);

    if (!port) {
      setMessage('Select a UART port before starting.', true);
      return;
    }
    if (!configName) {
      setMessage('Select a config before starting.', true);
      return;
    }

    await fetchJson('/api/ports/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ port }),
    });

    await fetchJson('/api/configs/activate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: configName }),
    });

    const session = await fetchJson('/api/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: 'start',
        samples_per_command: samples,
        delay_ms: delay,
        command_interval: commandInterval,
      }),
    });

    markDirty('port', false);
    markDirty('config', false);
    markDirty('sampleCount', false);
    markDirty('delayMs', false);
    markDirty('commandInterval', false);
    state.history = [];
    state.viewedSessionId = session.session_id;
    state.viewedConfigName = state.status?.active_config || null;
    await loadStatus();
    syncControlValue(elements.portSelect, state.status?.selected_port, 'port', { force: true });
    syncControlValue(elements.configSelect, state.status?.active_config_id, 'config', { force: true });
    syncControlValue(elements.sampleCount, state.status?.samples_per_command, 'sampleCount', { force: true });
    syncControlValue(elements.delayMs, state.status?.delay_ms, 'delayMs', { force: true });
    syncControlValue(elements.commandInterval, state.status?.command_interval, 'commandInterval', { force: true });
    await loadHistoryForSession(session.session_id);
    await loadSessions();
    render();
    setMessage(`Monitoring started on ${port}.`);
  }

  async function handleStopMonitoring() {
    await fetchJson('/api/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'stop' }),
    });
    await loadStatus();
    await loadSessions();
    if (state.status?.active_session_id) {
      state.viewedSessionId = state.status.active_session_id;
    }
    state.viewedConfigName = state.status?.active_config || null;
    render();
    setMessage('Monitoring stopped.');
  }

  async function handleDbCaptureToggle() {
    const newValue = !state.persistToDb;
    await fetchJson('/api/db-capture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: newValue }),
    });
    state.persistToDb = newValue;
    updateDbCaptureButton();
    setMessage(`DB capture ${newValue ? 'enabled — measurements will be saved to the database' : 'disabled — live stream only'}.`);
  }

  async function initialize() {
    try {
      installChartTooltipGuards();
      await loadStatus();
      state.viewedSessionId = state.status?.active_session_id || null;
      state.viewedConfigName = state.status?.active_config || null;
      updatePowerStateButton();
      if (!state.formDirty.dumpPath && !state.logDumpActive) {
        elements.dumpFilePath.value = getDefaultDumpPath(state.status?.active_config_id);
      } else if (state.logDumpActive && state.logDumpPath) {
        elements.dumpFilePath.value = state.logDumpPath;
      }
      await Promise.all([loadPorts(), loadConfigs(), loadSessions()]);
      if (state.status?.active_session_id) {
        await loadHistoryForSession(state.status.active_session_id);
      }
      render();
      ensureStream();
      setMessage('Dashboard ready.');
    } catch (error) {
      setMessage(error.message, true);
    }
  }

  elements.refreshPorts.addEventListener('click', () => {
    handlePortRefresh().catch((error) => setMessage(error.message, true));
  });

  elements.syncDashboard.addEventListener('click', () => {
    manualSync().catch((error) => setMessage(error.message, true));
  });

  elements.portSelect.addEventListener('change', () => {
    markDirty('port');
  });

  elements.configSelect.addEventListener('change', () => {
    markDirty('config');
    if (!state.formDirty.dumpPath) {
      elements.dumpFilePath.value = getDefaultDumpPath(elements.configSelect.value);
    }
  });

  elements.sampleCount.addEventListener('input', () => {
    markDirty('sampleCount');
  });

  elements.delayMs.addEventListener('input', () => {
    markDirty('delayMs');
  });

  elements.commandInterval.addEventListener('input', () => {
    markDirty('commandInterval');
  });

  elements.startMonitoring.addEventListener('click', () => {
    handleStartMonitoring().catch((error) => setMessage(error.message, true));
  });

  elements.stopMonitoring.addEventListener('click', () => {
    handleStopMonitoring().catch((error) => setMessage(error.message, true));
  });

  elements.dbCaptureToggle.addEventListener('click', () => {
    handleDbCaptureToggle().catch((error) => setMessage(error.message, true));
  });

  elements.dumpFilePath.addEventListener('input', () => {
    markDirty('dumpPath');
  });

  elements.dumpToggle.addEventListener('click', () => {
    handleDumpToggle().catch((error) => setMessage(error.message, true));
  });

  elements.powerStateToggle.addEventListener('click', () => {
    handlePowerStateToggle();
  });

  window.addEventListener('resize', renderCharts);

  initialize();
}());
