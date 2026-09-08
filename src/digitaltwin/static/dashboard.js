/**
 * Indy7 Digital Twin - Production & KPI Analytics Dashboard
 * Milestone 2: Production metrics, step breakdowns, downtime Pareto, state timeline.
 */
(() => {
  const byId = (id) => document.getElementById(id);

  // State
  let pollIntervalMs = 3000;
  let pollTimer = null;
  let isPolling = true;
  let lastKpiData = null;
  let lastRunsData = [];
  let lastTimelineData = [];

  // Phase color palette for the 8 process steps
  const STEP_COLORS = {
    PICK_APPROACH: '#06b6d4',       // cyan
    PICK_PLUNGE: '#3b82f6',         // blue
    PICK_GRIP: '#6366f1',           // indigo
    PICK_EXTRACT: '#8b5cf6',        // violet
    PLACE_APPROACH: '#10b981',      // emerald
    PLACE_PLUNGE: '#14b8a6',        // teal
    PLACE_RELEASE: '#f59e0b',       // amber
    PLACE_EXTRACT: '#ec4899',       // pink
    SLOT_PICK_APPROACH: '#06b6d4',
    SLOT_PICK_PLUNGE: '#3b82f6',
    SLOT_PICK_GRIP: '#6366f1',
    SLOT_PICK_EXTRACT: '#8b5cf6',
    MAGAZINE_PLACE_APPROACH: '#10b981',
    MAGAZINE_PLACE_PLUNGE: '#14b8a6',
    MAGAZINE_PLACE_RELEASE: '#f59e0b',
    MAGAZINE_PLACE_EXTRACT: '#ec4899',
  };

  const STATE_COLORS = {
    RUNNING: '#10b981',
    IDLE: '#3b82f6',
    FAULTED: '#ef4444',
    STOPPED: '#f59e0b',
    OFFLINE: '#64748b',
  };

  // ---------------------------------------------------------------------------
  // Data Fetching
  // ---------------------------------------------------------------------------
  async function fetchDashboardData() {
    try {
      const [kpiRes, runsRes, timelineRes] = await Promise.all([
        fetch('/api/production/kpi/summary'),
        fetch('/api/production/runs?limit=15'),
        fetch('/api/production/timeline?limit=30'),
      ]);

      if (kpiRes.ok) {
        lastKpiData = await kpiRes.json();
        renderQuickBadge(lastKpiData);
        renderExecutiveCards(lastKpiData);
        renderStepBreakdown(lastKpiData.step_time_breakdown, lastKpiData.bottleneck_step);
        renderDowntimePareto(lastKpiData.fault_stats, lastKpiData.total_faults);
      }

      if (runsRes.ok) {
        lastRunsData = await runsRes.json();
        renderRunsTable(lastRunsData);
      }

      if (timelineRes.ok) {
        lastTimelineData = await timelineRes.json();
        renderStateTimeline(lastTimelineData);
      }

      updateSyncIndicator(true);
    } catch (err) {
      console.error('[Dashboard] Error fetching analytics:', err);
      updateSyncIndicator(false);
    }
  }

  function updateSyncIndicator(ok) {
    const pulse = byId('kpiSyncPulse');
    const label = byId('kpiSyncLabel');
    if (!pulse || !label) return;
    if (ok) {
      pulse.className = 'pulse-dot active-pulse';
      label.textContent = `SYNCED · ${new Date().toLocaleTimeString()}`;
    } else {
      pulse.className = 'pulse-dot error-pulse';
      label.textContent = 'SYNC ERROR';
    }
  }

  // ---------------------------------------------------------------------------
  // Top-Bar Quick KPI Badge
  // ---------------------------------------------------------------------------
  function renderQuickBadge(kpi) {
    if (!kpi) return;
    const partsEl = byId('kpiQuickParts');
    const rateEl = byId('kpiQuickThroughput');
    const cycleEl = byId('kpiQuickCycle');

    if (partsEl) partsEl.textContent = `${kpi.total_parts_placed || 0} Parts`;
    if (rateEl) rateEl.textContent = `${kpi.throughput_parts_per_hour || 0}/h`;
    if (cycleEl) cycleEl.textContent = `${(kpi.avg_cycle_time_sec || 0).toFixed(2)}s avg`;
  }

  // ---------------------------------------------------------------------------
  // Executive KPI Stat Cards (6 Cards)
  // ---------------------------------------------------------------------------
  function renderExecutiveCards(kpi) {
    if (!kpi) return;

    // 1. Total Parts Placed
    byId('kpiValOutput').textContent = kpi.total_parts_placed || 0;
    byId('kpiSubOutput').textContent = `${kpi.completed_runs || 0} completed / ${kpi.total_runs || 0} total runs`;

    // 2. Throughput Rate
    const tpHr = kpi.throughput_parts_per_hour || 0;
    const tpMin = kpi.throughput_parts_per_minute || 0;
    byId('kpiValThroughput').textContent = `${tpHr.toFixed(1)}`;
    byId('kpiSubThroughput').textContent = `${tpMin.toFixed(2)} parts / min`;

    // 3. Average Cycle Time
    const avgC = (kpi.avg_cycle_time_sec || 0).toFixed(2);
    const minC = (kpi.min_cycle_time_sec || 0).toFixed(2);
    const maxC = (kpi.max_cycle_time_sec || 0).toFixed(2);
    byId('kpiValAvgCycle').textContent = `${avgC}s`;
    byId('kpiSubAvgCycle').textContent = `Range: ${minC}s – ${maxC}s (n=${kpi.completed_cycles_count || 0})`;

    // 4. P95 Cycle Time (Process Tail Latency)
    const p95C = (kpi.p95_cycle_time_sec || 0).toFixed(2);
    byId('kpiValP95Cycle').textContent = `${p95C}s`;
    byId('kpiSubP95Cycle').textContent = p95C > 0 ? `95% of cycles ≤ ${p95C}s` : 'No completed cycles yet';

    // 5. Workcell Operational Availability
    const avail = (kpi.operational_availability_pct || 100.0).toFixed(1);
    const actProdSec = (kpi.active_production_time_sec || 0).toFixed(1);
    byId('kpiValAvailability').textContent = `${avail}%`;
    byId('kpiSubAvailability').textContent = `Active runtime: ${actProdSec}s`;

    // 6. Downtime & Alarms
    const dtSec = (kpi.total_downtime_seconds || 0).toFixed(1);
    byId('kpiValDowntime').textContent = `${dtSec}s`;
    byId('kpiSubDowntime').textContent = `${kpi.total_faults || 0} total fault alarms`;
  }

  // ---------------------------------------------------------------------------
  // Process Step Breakdown (Segmented Bar + Legend Table)
  // ---------------------------------------------------------------------------
  function renderStepBreakdown(breakdown, bottleneck) {
    const barEl = byId('stepBreakdownBar');
    const listEl = byId('stepBreakdownList');
    const btagEl = byId('kpiBottleneckBadge');
    if (!barEl || !listEl) return;

    if (!breakdown || Object.keys(breakdown).length === 0) {
      barEl.innerHTML = '<div class="step-bar-empty">No cycle process steps recorded yet</div>';
      listEl.innerHTML = '<div class="kpi-empty-hint">Execute PB1 Palletizing to populate step breakdowns</div>';
      if (btagEl) btagEl.style.display = 'none';
      return;
    }

    if (btagEl && bottleneck) {
      btagEl.style.display = 'inline-flex';
      btagEl.textContent = `BOTTLENECK: ${bottleneck}`;
    }

    // Render Segmented Stacked Bar
    const entries = Object.entries(breakdown);
    let barHtml = '';
    entries.forEach(([stepName, info]) => {
      const pct = info.pct_of_cycle || 0;
      const color = STEP_COLORS[stepName] || '#94a3b8';
      if (pct > 0.5) {
        barHtml += `
          <div class="step-bar-segment" style="width: ${pct}%; background-color: ${color};"
               title="${stepName}: ${info.avg_duration_sec}s (${pct}%)"
               data-step="${stepName}">
            <span class="step-segment-label">${pct >= 10 ? pct + '%' : ''}</span>
          </div>`;
      }
    });
    barEl.innerHTML = barHtml;

    // Render Detailed Step Rows
    let listHtml = `
      <div class="step-table-header">
        <span>PROCESS STEP</span>
        <span>AVG DURATION</span>
        <span>% CYCLE</span>
        <span>SAMPLES</span>
      </div>`;

    entries.forEach(([stepName, info]) => {
      const color = STEP_COLORS[stepName] || '#94a3b8';
      const isBottleneck = stepName === bottleneck;
      listHtml += `
        <div class="step-table-row ${isBottleneck ? 'bottleneck-row' : ''}">
          <div class="step-name-cell">
            <span class="step-color-dot" style="background-color: ${color};"></span>
            <span class="step-name-text">${stepName}</span>
            ${isBottleneck ? '<span class="bottleneck-tag">MAX</span>' : ''}
          </div>
          <span class="mono-val">${info.avg_duration_sec.toFixed(3)}s</span>
          <span class="mono-val">${info.pct_of_cycle}%</span>
          <span class="mono-val text-muted">${info.count}</span>
        </div>`;
    });
    listEl.innerHTML = listHtml;
  }

  // ---------------------------------------------------------------------------
  // Downtime Pareto Analysis Chart
  // ---------------------------------------------------------------------------
  function renderDowntimePareto(faultStats, totalFaults) {
    const container = byId('paretoChartContainer');
    if (!container) return;

    if (!faultStats || Object.keys(faultStats).length === 0 || !totalFaults) {
      container.innerHTML = `
        <div class="pareto-empty-box">
          <span class="pareto-empty-icon">✓</span>
          <div class="pareto-empty-title">ZERO FAULT ALARMS RECORDED</div>
          <div class="pareto-empty-desc">System operating at nominal cadence without unhandled stops or downtime.</div>
        </div>`;
      return;
    }

    const entries = Object.entries(faultStats);
    let cumulativePct = 0;
    let html = '<div class="pareto-bars-list">';

    entries.forEach(([code, stat]) => {
      cumulativePct += stat.pct_of_faults;
      const cumRound = Math.min(100, Math.round(cumulativePct));
      const barWidth = Math.min(100, Math.max(8, stat.pct_of_faults));

      html += `
        <div class="pareto-row">
          <div class="pareto-row-header">
            <span class="pareto-code-name">⚠️ ${code}</span>
            <span class="pareto-counts">
              <strong>${stat.count} occurrences</strong> (${stat.pct_of_faults}%) · 
              <span class="text-amber">${stat.downtime_seconds.toFixed(1)}s downtime</span> · 
              <span class="text-cyan">Cum: ${cumRound}%</span>
            </span>
          </div>
          <div class="pareto-bar-track">
            <div class="pareto-bar-fill" style="width: ${barWidth}%;"></div>
            <div class="pareto-cum-marker" style="left: ${cumRound}%;" title="Cumulative: ${cumRound}%"></div>
          </div>
        </div>`;
    });

    html += '</div>';
    container.innerHTML = html;
  }

  // ---------------------------------------------------------------------------
  // Equipment State Timeline Ribbon
  // ---------------------------------------------------------------------------
  function renderStateTimeline(timeline) {
    const ribbonEl = byId('stateTimelineRibbon');
    const tooltipEl = byId('stateTimelineTooltip');
    if (!ribbonEl) return;

    if (!timeline || timeline.length === 0) {
      ribbonEl.innerHTML = '<div class="timeline-empty">No state transitions recorded</div>';
      return;
    }

    // Total duration of visible window
    const totalDuration = timeline.reduce((acc, cur) => acc + (cur.duration_seconds || 1), 0);

    let ribbonHtml = '';
    timeline.forEach((item, idx) => {
      const dur = item.duration_seconds || 1;
      const state = (item.to_state || 'IDLE').toUpperCase();
      const color = STATE_COLORS[state] || '#64748b';

      ribbonHtml += `
        <div class="timeline-block"
             style="flex-grow: ${dur}; background-color: ${color};"
             data-index="${idx}"
             data-state="${state}"
             data-trigger="${item.trigger || ''}"
             data-duration="${dur.toFixed(1)}s"
             data-time="${item.timestamp || ''}">
        </div>`;
    });

    ribbonEl.innerHTML = ribbonHtml;

    // Attach hover listeners for floating tooltip
    ribbonEl.querySelectorAll('.timeline-block').forEach((block) => {
      block.addEventListener('mouseenter', (e) => {
        if (!tooltipEl) return;
        const state = block.dataset.state;
        const trigger = block.dataset.trigger;
        const duration = block.dataset.duration;
        const time = block.dataset.time.slice(11, 19);

        tooltipEl.innerHTML = `
          <strong>${state}</strong> (${duration})<br>
          <span style="color: var(--text-muted);">Trigger: ${trigger || 'N/A'} · ${time} UTC</span>`;
        tooltipEl.style.display = 'block';
        positionTooltip(e, tooltipEl);
      });

      block.addEventListener('mousemove', (e) => {
        if (tooltipEl) positionTooltip(e, tooltipEl);
      });

      block.addEventListener('mouseleave', () => {
        if (tooltipEl) tooltipEl.style.display = 'none';
      });
    });
  }

  function positionTooltip(e, el) {
    const rect = el.parentElement.getBoundingClientRect();
    const x = e.clientX - rect.left;
    el.style.left = `${Math.min(x, rect.width - 160)}px`;
  }

  // ---------------------------------------------------------------------------
  // Recent Production Runs Table
  // ---------------------------------------------------------------------------
  function renderRunsTable(runs) {
    const tbody = byId('runsTableBody');
    if (!tbody) return;

    if (!runs || runs.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="text-center py-4 text-muted">No production runs recorded yet</td></tr>`;
      return;
    }

    let rowsHtml = '';
    runs.forEach((r) => {
      const shortId = r.run_id ? r.run_id.slice(0, 8) : 'unknown';
      const statusClass = `status-${r.status || 'running'}`;
      const dur = r.duration_seconds !== null ? `${r.duration_seconds.toFixed(2)}s` : 'active';
      const timeStr = r.start_time ? r.start_time.replace('T', ' ').slice(0, 19) : '';
      const cmdName = r.command === 'pb1' ? 'Palletize (PB1)' : (r.command === 'pb2' ? 'Put-Back (PB2)' : r.command);

      rowsHtml += `
        <tr>
          <td><code class="run-id-code" title="${r.run_id}">${shortId}</code></td>
          <td><strong>${cmdName}</strong></td>
          <td><span class="badge-origin">${r.origin || 'SIMULATION'}</span></td>
          <td><span class="status-pill ${statusClass}">${r.status}</span></td>
          <td class="mono-val">${r.completed_count || 0} / ${r.target_count || 8}</td>
          <td class="mono-val">${dur}</td>
          <td class="mono-val text-muted">${timeStr}</td>
        </tr>`;
    });

    tbody.innerHTML = rowsHtml;
  }

  // ---------------------------------------------------------------------------
  // Data Export (CSV & JSON)
  // ---------------------------------------------------------------------------
  function exportJson() {
    const exportBundle = {
      exported_at: new Date().toISOString(),
      kpi_summary: lastKpiData,
      recent_runs: lastRunsData,
      state_timeline: lastTimelineData,
    };
    const blob = new Blob([JSON.stringify(exportBundle, null, 2)], { type: 'application/json' });
    downloadBlob(blob, `production_kpi_${Date.now()}.json`);
  }

  function exportCsv() {
    if (!lastRunsData || lastRunsData.length === 0) {
      alert('No run data available to export.');
      return;
    }
    const headers = ['run_id', 'command', 'origin', 'recipe_id', 'recipe_version', 'status', 'target_count', 'completed_count', 'duration_seconds', 'start_time', 'end_time'];
    const lines = [headers.join(',')];

    lastRunsData.forEach((r) => {
      const row = headers.map((h) => {
        const val = r[h] !== null && r[h] !== undefined ? String(r[h]).replace(/"/g, '""') : '';
        return `"${val}"`;
      });
      lines.push(row.join(','));
    });

    const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
    downloadBlob(blob, `production_runs_${Date.now()}.csv`);
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  // ---------------------------------------------------------------------------
  // Polling Engine & Event Listeners
  // ---------------------------------------------------------------------------
  function startPolling() {
    stopPolling();
    if (pollIntervalMs > 0) {
      pollTimer = setInterval(() => {
        if (isPolling && document.visibilityState === 'visible') {
          fetchDashboardData();
        }
      }, pollIntervalMs);
    }
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function initDashboard() {
    // 1. Initial Fetch
    fetchDashboardData();
    startPolling();

    // 2. Refresh Button
    const btnRefresh = byId('btnRefreshKpi');
    if (btnRefresh) {
      btnRefresh.addEventListener('click', () => {
        fetchDashboardData();
      });
    }

    // 3. Interval Selector
    const selInterval = byId('selPollInterval');
    if (selInterval) {
      selInterval.addEventListener('change', (e) => {
        const val = parseInt(e.target.value, 10);
        if (val === 0) {
          isPolling = false;
          stopPolling();
          updateSyncIndicator(true);
          const label = byId('kpiSyncLabel');
          if (label) label.textContent = 'PAUSED';
        } else {
          isPolling = true;
          pollIntervalMs = val;
          startPolling();
          fetchDashboardData();
        }
      });
    }

    // 4. Export Buttons
    const btnExpCsv = byId('btnExportCsv');
    if (btnExpCsv) btnExpCsv.addEventListener('click', exportCsv);
    const btnExpJson = byId('btnExportJson');
    if (btnExpJson) btnExpJson.addEventListener('click', exportJson);

    // 5. Expand Drawer Toggle
    const btnExpand = byId('btnToggleExpandDrawer');
    const drawer = byId('multiCenterDrawer');
    if (btnExpand && drawer) {
      btnExpand.addEventListener('click', () => {
        drawer.classList.toggle('expanded-view');
        const isExp = drawer.classList.contains('expanded-view');
        btnExpand.textContent = isExp ? '⛶ COMPACT VIEW' : '⛶ EXPAND VIEW';
      });
    }

    // 6. Quick Badge Click -> Switch to Production Tab
    const quickBadge = byId('kpiQuickBadge');
    if (quickBadge) {
      quickBadge.addEventListener('click', () => {
        const tabBtn = byId('tabBtnProduction');
        if (tabBtn) tabBtn.click();
      });
    }

    // 7. Instant Refresh on Telemetry & Cycle Events
    window.addEventListener('workcell-telemetry', () => {
      // Light update of quick badge if polling is active
      if (document.visibilityState === 'visible' && Math.random() < 0.2) {
        renderQuickBadge(lastKpiData);
      }
    });

    window.addEventListener('workcell-command', () => {
      // Whenever a command completes or faults, trigger an immediate refresh
      setTimeout(fetchDashboardData, 400);
    });
  }

  // Self-initialize on DOM load
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initDashboard);
  } else {
    initDashboard();
  }
})();
