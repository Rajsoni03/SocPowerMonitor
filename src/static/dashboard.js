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
    formDirty: {
      port: false,
      config: false,
      sampleCount: false,
      delayMs: false,
    },
  };

  const MAX_HISTORY_POINTS = 180;
  const RAIL_COLORS = ['#38bdf8', '#22c55e', '#f59e0b', '#a78bfa', '#f472b6', '#fb7185', '#14b8a6', '#f97316'];
  let chartTooltip = null;
  let chartTooltipGuardsInstalled = false;

  const elements = {
    portSelect: document.getElementById('port-select'),
    refreshPorts: document.getElementById('refresh-ports'),
    syncDashboard: document.getElementById('sync-dashboard'),
    configSelect: document.getElementById('config-select'),
    sampleCount: document.getElementById('sample-count'),
    delayMs: document.getElementById('delay-ms'),
    startMonitoring: document.getElementById('start-monitoring'),
    stopMonitoring: document.getElementById('stop-monitoring'),
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
        .map((railName) => normalizeRailKey(railName))
    );
    console.log([...ignoredRails]);
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

    readings.forEach((reading) => {
      const name = reading.rail || 'unknown';
      rails[name] = {
        voltage_v: reading.voltage_v,
        current_ma: reading.current_ma,
        power_mw: reading.power_mw,
      };
      if (!ignoredRails.has(name)) {
        totalPower += reading.power_mw || 0;
      }
    });

    return { ts, rails, totalPower };
  }

  function mergePoint(point) {
    const existing = state.history.find((item) => item.ts === point.ts);
    if (existing) {
      existing.rails = point.rails;
      existing.totalPower = point.totalPower;
    } else {
      state.history.push(point);
      state.history.sort((left, right) => new Date(left.ts) - new Date(right.ts));
      if (state.history.length > MAX_HISTORY_POINTS) {
        state.history.splice(0, state.history.length - MAX_HISTORY_POINTS);
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

    const offset = 14;
    const rect = tooltip.getBoundingClientRect();
    let left = event.clientX + offset;
    let top = event.clientY - rect.height - offset;

    if (left + rect.width > window.innerWidth - 8) {
      left = event.clientX - rect.width - offset;
    }
    if (top < 8) {
      top = event.clientY + offset;
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

  function renderSessions() {
    elements.sessionsList.innerHTML = '';
    if (!state.sessions.length) {
      elements.sessionsList.innerHTML = '<p>No sessions recorded yet.</p>';
      return;
    }

    state.sessions.slice(0, 8).forEach((session) => {
      const item = document.createElement('article');
      item.className = 'session-item';
      const activeLabel = state.status?.active_session_id === session.id ? 'Active session' : 'View history';
      item.innerHTML = `
        <h3>Session #${session.id}</h3>
        <p>${session.config_name}</p>
        <p>${formatSessionTime(session.started_at)} to ${formatSessionTime(session.ended_at)}</p>
        <p><a href="/api/export.csv?session_id=${session.id}">Export CSV</a> • <a href="#" data-session-id="${session.id}">${activeLabel}</a></p>
      `;
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

    const points = values.map((value, index) => ({
      x: leftPad + xStep * index,
      y: Number.isFinite(value) ? height - bottomPad - ((value - minValue) / range) * plotHeight : null,
      value,
      label: labels[index] || '',
    }));
    const hoverPoints = points.filter((point) => Number.isFinite(point.y));
    const renderChart = () => {
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
        context.fillText(`${tickValue.toFixed(0)} ${unitLabel}`, leftPad - 6, y + 4);
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

      if (activePoint) {
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
      }

      context.fillStyle = 'rgba(203, 213, 225, 0.9)';
      context.textAlign = 'right';
      context.fillText(`${rawMaxValue.toFixed(1)} ${unitLabel}`, width - rightPad, 18);
      context.textAlign = 'left';
    };

    renderChart();

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

      activePoint = nearest;
      renderChart();
      showChartTooltip(event, nearest, unitLabel);
    };
    canvas.onmouseleave = () => {
      activePoint = null;
      renderChart();
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
      const delta = Number.isFinite(previousPower) ? latestPower - previousPower : NaN;
      const color = railColor(railName, index);
      const values = state.history.map((point) => {
        const power = point.rails[railName]?.power_mw;
        return Number.isFinite(power) ? power : null;
      });

      const card = document.createElement('article');
      card.className = 'rail-chart-card';
      card.innerHTML = `
        <h3>${railName}</h3>
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
      chartDefs.forEach(({ canvas, values, labels, color, railName }) => {
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

  function ensureStream() {
    if (state.eventSource) {
      state.eventSource.close();
    }

    state.eventSource = new EventSource('/api/stream');
    state.eventSource.onmessage = (event) => {
      const payload = JSON.parse(event.data);
      applyStreamPayload(payload);
    };
    state.eventSource.onerror = () => {
      setMessage('Live stream disconnected. Click Sync to refresh dashboard state.', true);
    };
  }

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
      }),
    });

    markDirty('port', false);
    markDirty('config', false);
    markDirty('sampleCount', false);
    markDirty('delayMs', false);
    state.history = [];
    state.viewedSessionId = session.session_id;
    state.viewedConfigName = state.status?.active_config || null;
    await loadStatus();
    syncControlValue(elements.portSelect, state.status?.selected_port, 'port', { force: true });
    syncControlValue(elements.configSelect, state.status?.active_config_id, 'config', { force: true });
    syncControlValue(elements.sampleCount, state.status?.samples_per_command, 'sampleCount', { force: true });
    syncControlValue(elements.delayMs, state.status?.delay_ms, 'delayMs', { force: true });
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

  async function initialize() {
    try {
      installChartTooltipGuards();
      await loadStatus();
      state.viewedSessionId = state.status?.active_session_id || null;
      state.viewedConfigName = state.status?.active_config || null;
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
  });

  elements.sampleCount.addEventListener('input', () => {
    markDirty('sampleCount');
  });

  elements.delayMs.addEventListener('input', () => {
    markDirty('delayMs');
  });

  elements.startMonitoring.addEventListener('click', () => {
    handleStartMonitoring().catch((error) => setMessage(error.message, true));
  });

  elements.stopMonitoring.addEventListener('click', () => {
    handleStopMonitoring().catch((error) => setMessage(error.message, true));
  });

  window.addEventListener('resize', renderCharts);

  initialize();
}());
