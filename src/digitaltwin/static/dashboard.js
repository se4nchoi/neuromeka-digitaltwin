/**
 * Indy7 Digital Twin - Production & KPI Analytics Dashboard
 * Milestone 2: Production metrics, step breakdowns, downtime Pareto, state timeline,
 * event audit trail, and run details inspector.
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
  let lastEventsData = [];
  let lastWorkOrdersData = [];
  let lastRecipesData = [];
  let lastBenchmarksData = [];
  let activeBenchmarkDetail = null;
  let activeWorkOrderSummary = null;
  let lastTelemetryState = null;
  let activeEventFilter = 'all';

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
      // Fetch recipes on startup if not loaded yet
      if (lastRecipesData.length === 0) {
        try {
          const recRes = await fetch('/api/recipes');
          if (recRes.ok) {
            lastRecipesData = await recRes.json();
            renderRecipesSelect(lastRecipesData);
            renderBenchmarkingRecipes(lastRecipesData);
          }
        } catch (e) {
          console.warn('[Dashboard] Failed to fetch recipes:', e);
        }
      }

      const [kpiRes, runsRes, timelineRes, eventsRes, ordersRes, stateRes] = await Promise.all([
        fetch('/api/production/kpi/summary'),
        fetch('/api/production/runs?limit=30'),
        fetch('/api/production/timeline?limit=30'),
        fetch('/api/production/events?limit=40'),
        fetch('/api/work-orders?limit=30'),
        fetch('/api/state'),
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

      if (eventsRes.ok) {
        lastEventsData = await eventsRes.json();
        renderEventsFeed(lastEventsData, activeEventFilter);
      }

      if (stateRes.ok) {
        lastTelemetryState = await stateRes.json();
      }

      if (ordersRes.ok) {
        lastWorkOrdersData = await ordersRes.json();
        renderActiveWorkOrder(lastTelemetryState, lastWorkOrdersData);
        renderWorkOrdersTable(lastWorkOrdersData);
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
  // Top-Bar Quick KPI Badge (Present on 3D Twin Page)
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
    if (byId('kpiValOutput')) {
      byId('kpiValOutput').textContent = kpi.total_parts_placed || 0;
      byId('kpiSubOutput').textContent = `${kpi.completed_runs || 0} completed / ${kpi.total_runs || 0} total runs`;
    }

    // 2. Throughput Rate
    if (byId('kpiValThroughput')) {
      const tpHr = kpi.throughput_parts_per_hour || 0;
      const tpMin = kpi.throughput_parts_per_minute || 0;
      byId('kpiValThroughput').textContent = `${tpHr.toFixed(1)}`;
      byId('kpiSubThroughput').textContent = `${tpMin.toFixed(2)} parts / min`;
    }

    // 3. Average Cycle Time
    if (byId('kpiValAvgCycle')) {
      const avgC = (kpi.avg_cycle_time_sec || 0).toFixed(2);
      const minC = (kpi.min_cycle_time_sec || 0).toFixed(2);
      const maxC = (kpi.max_cycle_time_sec || 0).toFixed(2);
      byId('kpiValAvgCycle').textContent = `${avgC}s`;
      byId('kpiSubAvgCycle').textContent = `Range: ${minC}s – ${maxC}s (n=${kpi.completed_cycles_count || 0})`;
    }

    // 4. P95 Cycle Time (Process Tail Latency)
    if (byId('kpiValP95Cycle')) {
      const p95C = (kpi.p95_cycle_time_sec || 0).toFixed(2);
      byId('kpiValP95Cycle').textContent = `${p95C}s`;
      byId('kpiSubP95Cycle').textContent = p95C > 0 ? `95% of cycles ≤ ${p95C}s` : 'No completed cycles yet';
    }

    // 5. Workcell Operational Availability
    if (byId('kpiValAvailability')) {
      const avail = (kpi.operational_availability_pct || 100.0).toFixed(1);
      const actProdSec = (kpi.active_production_time_sec || 0).toFixed(1);
      byId('kpiValAvailability').textContent = `${avail}%`;
      byId('kpiSubAvailability').textContent = `Active runtime: ${actProdSec}s`;
    }

    // 6. Downtime & Alarms
    if (byId('kpiValDowntime')) {
      const dtSec = (kpi.total_downtime_seconds || 0).toFixed(1);
      byId('kpiValDowntime').textContent = `${dtSec}s`;
      byId('kpiSubDowntime').textContent = `${kpi.total_faults || 0} total fault alarms`;
    }
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
      listEl.innerHTML = '<div class="step-bar-empty">Execute Palletizing routines to populate step telemetry</div>';
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
          <span class="mono-val" style="color: var(--text-dim);">${info.count}</span>
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
              <strong>${stat.count} occurrences</strong> (${stat.pct_of_faults}%) &bull; 
              <span class="text-amber">${stat.downtime_seconds.toFixed(1)}s downtime</span> &bull; 
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
          <span style="color: var(--text-muted);">Trigger: ${trigger || 'N/A'} &bull; ${time} UTC</span>`;
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
  // Real-time Operational Event Stream / Audit Log
  // ---------------------------------------------------------------------------
  function renderEventsFeed(events, filter) {
    const container = byId('eventsListContainer');
    if (!container) return;

    if (!events || events.length === 0) {
      container.innerHTML = '<div class="events-empty">No events logged yet</div>';
      return;
    }

    const filtered = filter === 'all'
      ? events
      : events.filter(e => e.event_type === filter);

    if (filtered.length === 0) {
      container.innerHTML = `<div class="events-empty">No ${filter} events found</div>`;
      return;
    }

    let html = '';
    filtered.forEach((e) => {
      const type = e.event_type || 'info';
      const time = e.timestamp ? e.timestamp.replace('T', ' ').slice(11, 19) : '';
      const summary = e.summary || '';
      const detail = e.detail ? `(${e.detail})` : '';

      html += `
        <div class="event-row event-${type}">
          <div class="event-main">
            <span class="event-badge badge-${type}">${type}</span>
            <span class="event-summary">${summary}</span>
            <span class="event-detail">${detail}</span>
          </div>
          <span class="event-time">${time}</span>
        </div>`;
    });

    container.innerHTML = html;
  }

  // ---------------------------------------------------------------------------
  // Historical Production Runs Table with Run Inspection
  // ---------------------------------------------------------------------------
  function renderRunsTable(runs) {
    const tbody = byId('runsTableBody');
    if (!tbody) return;

    if (!runs || runs.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" class="text-center py-4 text-muted">No production runs recorded yet</td></tr>`;
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
          <td class="mono-val" style="color: var(--text-dim);">${timeStr}</td>
          <td style="text-align: right;">
            <button class="btn-inspect-run" data-runid="${r.run_id}">Inspect</button>
          </td>
        </tr>`;
    });

    tbody.innerHTML = rowsHtml;

    // Attach inspect click handlers
    tbody.querySelectorAll('.btn-inspect-run').forEach((btn) => {
      btn.addEventListener('click', () => {
        const runId = btn.dataset.runid;
        inspectRun(runId);
      });
    });
  }

  async function inspectRun(runId) {
    const modal = byId('modalRunDetails');
    const subTitle = byId('modalRunSubtitle');
    const body = byId('modalRunBody');
    if (!modal || !body) return;

    subTitle.textContent = `Loading run details for ${runId}...`;
    body.innerHTML = '<div class="events-empty">Loading cycle and step telemetry...</div>';
    modal.style.display = 'flex';

    try {
      const res = await fetch(`/api/production/runs/${runId}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      subTitle.textContent = `Run ID: ${data.run_id} · Routine: ${data.command} · Status: ${data.status} · Duration: ${data.duration_seconds || 0}s`;

      if (!data.cycles || data.cycles.length === 0) {
        body.innerHTML = '<div class="events-empty">No cycle records captured for this run.</div>';
        return;
      }

      let html = '';
      data.cycles.forEach((c) => {
        const cDur = c.duration_seconds !== null ? `${c.duration_seconds.toFixed(2)}s` : 'active';
        html += `
          <div class="cycle-inspect-card">
            <div class="cycle-inspect-header">
              <span>CYCLE #${c.cycle_index + 1} (Target Slot: S${c.target_slot + 1})</span>
              <span style="color: var(--accent-cyan);">${cDur} &bull; ${c.status}</span>
            </div>
            <div class="steps-mini-grid">`;

        if (c.steps && c.steps.length > 0) {
          c.steps.forEach((s) => {
            const sDur = s.duration_seconds !== null ? `${s.duration_seconds.toFixed(3)}s` : 'active';
            html += `
              <div class="step-mini-pill">
                <span class="step-mini-name">${s.step_name}</span>
                <span class="step-mini-dur">${sDur}</span>
              </div>`;
          });
        } else {
          html += '<span style="font-size: 10px; color: var(--text-dim);">No steps recorded</span>';
        }

        html += `</div></div>`;
      });

      body.innerHTML = html;
    } catch (err) {
      body.innerHTML = `<div class="events-empty" style="color: var(--accent-crimson);">Failed to load run details: ${err.message}</div>`;
    }
  }

  // ---------------------------------------------------------------------------
  // Work Order & Recipe Workflow (Milestone 3)
  // ---------------------------------------------------------------------------
  function renderRecipesSelect(recipes) {
    const sel = byId('selOrderRecipe');
    if (!sel) return;
    if (!recipes || recipes.length === 0) {
      sel.innerHTML = '<option value="">No recipes available</option>';
      return;
    }
    sel.innerHTML = recipes.map((r) => {
      const cap = r.max_capacity || (r.grid_floors * r.grid_rows * r.grid_cols) || 8;
      return `<option value="${r.recipe_id}">${r.name} (${cap} slots · v${r.version})</option>`;
    }).join('');
  }

  // ---------------------------------------------------------------------------
  // Milestone 4: Benchmarking and Optimization UI
  // ---------------------------------------------------------------------------
  function renderBenchmarkingRecipes(recipes) {
    const selBase = byId('selBenchBaseRecipe');
    const selCand = byId('selBenchCandRecipe');
    if (!selBase || !selCand || !recipes || recipes.length === 0) return;

    const baseHtml = recipes.map(r => {
      const isSel = r.recipe_id === 'pallet-2x2x2-default' ? 'selected' : '';
      return `<option value="${r.recipe_id}" ${isSel}>${r.name} (${r.recipe_id})</option>`;
    }).join('');

    const candHtml = recipes.map(r => {
      const isSel = r.recipe_id === 'pallet-high-speed' ? 'selected' : '';
      return `<option value="${r.recipe_id}" ${isSel}>${r.name} (${r.recipe_id})</option>`;
    }).join('');

    selBase.innerHTML = baseHtml;
    selCand.innerHTML = candHtml;
  }

  function renderBenchmarkResults(b) {
    if (!b || !b.summary) return;
    const s = b.summary;

    // 1. Cycle Times
    const baseMean = s.baseline ? s.baseline.mean.toFixed(2) + 's' : '-- s';
    const baseSub = s.baseline ? `P95: ${s.baseline.p95.toFixed(2)}s (σ: ±${s.baseline.std.toFixed(2)}s)` : '--';
    const candMean = s.candidate ? s.candidate.mean.toFixed(2) + 's' : '-- s';
    const candSub = s.candidate ? `P95: ${s.candidate.p95.toFixed(2)}s (σ: ±${s.candidate.std.toFixed(2)}s)` : '--';

    if (byId('benchBaseMean')) byId('benchBaseMean').textContent = baseMean;
    if (byId('benchBaseSub')) byId('benchBaseSub').textContent = baseSub;
    if (byId('benchCandMean')) byId('benchCandMean').textContent = candMean;
    if (byId('benchCandSub')) byId('benchCandSub').textContent = candSub;

    // 2. Net Delta
    const deltaVal = s.delta_cycle_sec !== undefined ? (s.delta_cycle_sec > 0 ? `+${s.delta_cycle_sec.toFixed(2)}s` : `${s.delta_cycle_sec.toFixed(2)}s`) : '-- s';
    const pctVal = s.pct_reduction !== undefined ? `${s.pct_reduction > 0 ? '-' : '+'}${Math.abs(s.pct_reduction).toFixed(1)}%` : '--%';
    const uplift = s.throughput_uplift_parts_hr !== undefined ? `Throughput uplift: +${s.throughput_uplift_parts_hr.toFixed(1)} parts/hr (${s.throughput_candidate_parts_hr} total)` : '--';

    if (byId('benchDeltaVal')) byId('benchDeltaVal').textContent = deltaVal;
    if (byId('benchSavingsPill')) {
      byId('benchSavingsPill').textContent = pctVal;
      byId('benchSavingsPill').style.color = s.pct_reduction > 0 ? 'var(--accent-emerald)' : 'var(--accent-crimson)';
    }
    if (byId('benchThroughputUplift')) byId('benchThroughputUplift').textContent = uplift;

    // 3. Statistical Significance
    if (s.confidence_interval_95 && byId('benchCiVal')) {
      byId('benchCiVal').textContent = `[${s.confidence_interval_95[0].toFixed(2)}, ${s.confidence_interval_95[1].toFixed(2)}] s`;
    }
    if (byId('benchSignificanceBadge')) {
      const isSig = s.is_significant;
      byId('benchSignificanceBadge').className = isSig ? 'status-pill pill-completed' : 'status-pill pill-neutral';
      byId('benchSignificanceBadge').textContent = isSig ? 'SIGNIFICANT (p < 0.05)' : 'NOT SIGNIFICANT';
    }
    if (byId('benchPValueText')) {
      byId('benchPValueText').textContent = `p = ${s.p_value < 0.0001 ? '< 0.0001' : s.p_value.toFixed(4)}`;
    }

    // 4. Kinematics & Feasibility
    const kinCand = s.kinematics && s.kinematics.candidate ? s.kinematics.candidate : {};
    const feasibility = kinCand.feasibility || 'FEASIBLE';
    if (byId('benchFeasibilityBadge')) {
      byId('benchFeasibilityBadge').textContent = feasibility;
      byId('benchFeasibilityBadge').style.color = feasibility === 'FEASIBLE' ? 'var(--accent-emerald)' : (feasibility === 'WARNING' ? 'var(--accent-amber)' : 'var(--accent-crimson)');
    }
    if (byId('benchJerkStats')) {
      byId('benchJerkStats').textContent = `Peak Jerk: ${kinCand.max_joint_jerk_deg_s3 || 0}°/s³ · Max Vel: ${kinCand.max_joint_vel_deg_s || 0}°/s`;
    }

    // 5. Step Delta Breakdown
    const stepContainer = byId('benchStepDeltaContainer');
    if (stepContainer && s.step_breakdown) {
      let stepHtml = '';
      const steps = s.step_breakdown;
      const maxBase = Math.max(...Object.values(steps).map(v => v.baseline_sec || 1.0));
      for (const [stepName, item] of Object.entries(steps)) {
        const baseW = Math.min(100, Math.round((item.baseline_sec / maxBase) * 100));
        const candW = Math.min(100, Math.round((item.candidate_sec / maxBase) * 100));
        const saved = item.delta_sec < 0;
        const deltaText = item.delta_sec > 0 ? `+${item.delta_sec.toFixed(2)}s` : `${item.delta_sec.toFixed(2)}s`;
        const valClass = saved ? 'step-delta-val saving' : 'step-delta-val neutral';
        stepHtml += `
          <div class="step-delta-row">
            <span class="step-delta-name" title="${stepName}">${stepName}</span>
            <div class="step-delta-bar-wrap" title="Base: ${item.baseline_sec}s vs Cand: ${item.candidate_sec}s">
              <div class="step-delta-fill-base" style="width: ${baseW}%;"></div>
              <div class="step-delta-fill-cand" style="width: ${candW}%;"></div>
            </div>
            <span class="${valClass}">${deltaText}</span>
          </div>`;
      }
      stepContainer.innerHTML = stepHtml;
    }

    // 6. Kinematic Limits Table
    const kinTbody = byId('benchKinematicsTableBody');
    if (kinTbody && s.kinematics) {
      const kb = s.kinematics.baseline || {};
      const kc = s.kinematics.candidate || {};
      kinTbody.innerHTML = `
        <tr>
          <td>Peak Joint Velocity</td>
          <td>${kb.max_joint_vel_deg_s || '--'} °/s</td>
          <td style="color: ${kc.max_joint_vel_deg_s > 180 ? 'var(--accent-amber)' : 'var(--accent-emerald)'}; font-weight: 700;">${kc.max_joint_vel_deg_s || '--'} °/s</td>
          <td>180.0 °/s</td>
          <td>${kc.max_joint_vel_deg_s ? Math.round(180 - kc.max_joint_vel_deg_s) + ' °/s margin' : '--'}</td>
        </tr>
        <tr>
          <td>Peak Joint Acceleration</td>
          <td>${kb.max_joint_acc_deg_s2 || '--'} °/s²</td>
          <td style="color: ${kc.max_joint_acc_deg_s2 > 450 ? 'var(--accent-amber)' : 'var(--accent-emerald)'}; font-weight: 700;">${kc.max_joint_acc_deg_s2 || '--'} °/s²</td>
          <td>450.0 °/s²</td>
          <td>${kc.max_joint_acc_deg_s2 ? Math.round(450 - kc.max_joint_acc_deg_s2) + ' °/s² margin' : '--'}</td>
        </tr>
        <tr>
          <td>Peak Joint Jerk</td>
          <td>${kb.max_joint_jerk_deg_s3 || '--'} °/s³</td>
          <td style="font-weight: 700; color: #cbd5e1;">${kc.max_joint_jerk_deg_s3 || '--'} °/s³</td>
          <td>--</td>
          <td>Smooth C² Quintic</td>
        </tr>
        <tr>
          <td>Trajectory Feasibility</td>
          <td><span class="status-pill pill-completed">${kb.feasibility || 'FEASIBLE'}</span></td>
          <td><span class="status-pill ${kc.feasibility === 'FEASIBLE' ? 'pill-completed' : (kc.feasibility === 'WARNING' ? 'pill-paused_pallet_change' : 'pill-failed')}">${kc.feasibility || 'FEASIBLE'}</span></td>
          <td>Step &le; 15°</td>
          <td>Continuous</td>
        </tr>
      `;
    }
  }

  async function fetchBenchmarksHistory() {
    const tbody = byId('benchmarksTableBody');
    if (!tbody) return;
    try {
      const res = await fetch('/api/benchmarks?limit=20');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      lastBenchmarksData = await res.json();
      renderBenchmarksTable(lastBenchmarksData);
      if (lastBenchmarksData.length > 0 && !activeBenchmarkDetail) {
        inspectBenchmark(lastBenchmarksData[0].benchmark_id);
      }
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="10" style="text-align: center; color: var(--accent-crimson);">Failed to load benchmarks: ${err.message}</td></tr>`;
    }
  }

  function renderBenchmarksTable(list) {
    const tbody = byId('benchmarksTableBody');
    if (!tbody) return;
    if (!list || list.length === 0) {
      tbody.innerHTML = '<tr><td colspan="10" style="text-align: center; color: var(--text-muted); padding: 18px;">No benchmark experiments recorded. Run a benchmark above to compare recipes.</td></tr>';
      return;
    }
    tbody.innerHTML = list.map(b => {
      const s = b.summary || {};
      const deltaT = s.delta_cycle_sec !== undefined ? `${s.delta_cycle_sec > 0 ? '+' : ''}${s.delta_cycle_sec.toFixed(2)}s` : '--';
      const pct = s.pct_reduction !== undefined ? `${s.pct_reduction > 0 ? '-' : '+'}${Math.abs(s.pct_reduction).toFixed(1)}%` : '--';
      const pVal = s.p_value !== undefined ? (s.p_value < 0.001 ? '<0.001' : s.p_value.toFixed(3)) : '--';
      const f = s.kinematics && s.kinematics.candidate ? s.kinematics.candidate.feasibility : 'FEASIBLE';
      const dateStr = b.created_at ? b.created_at.replace('T', ' ').substring(0, 19) : '--';
      return `
        <tr>
          <td style="font-weight: 700; color: #fff;">${b.name || 'Benchmark'}</td>
          <td><span class="wo-recipe-tag" style="font-size: 10px;">${b.baseline_recipe_id}</span></td>
          <td><span class="wo-recipe-tag" style="font-size: 10px; color: var(--accent-cyan);">${b.candidate_recipe_id}</span></td>
          <td>${b.trials_per_variant}</td>
          <td style="font-family: var(--font-mono); font-weight: 700; color: var(--accent-emerald);">${deltaT}</td>
          <td><span class="bench-delta-pill">${pct}</span></td>
          <td style="font-family: var(--font-mono); font-size: 11px;">${pVal}</td>
          <td><span class="status-pill ${f === 'FEASIBLE' ? 'pill-completed' : (f === 'WARNING' ? 'pill-paused_pallet_change' : 'pill-failed')}">${f}</span></td>
          <td style="font-family: var(--font-mono); font-size: 11px; color: var(--text-muted);">${dateStr}</td>
          <td style="text-align: right;">
            <button class="btn-inspect-bench" data-bid="${b.benchmark_id}" style="background: rgba(6, 182, 212, 0.15); border: 1px solid rgba(6, 182, 212, 0.3); color: var(--accent-cyan); padding: 3px 8px; border-radius: 4px; font-family: var(--font-mono); font-size: 10px; font-weight: 700; cursor: pointer;">INSPECT</button>
          </td>
        </tr>`;
    }).join('');

    tbody.querySelectorAll('.btn-inspect-bench').forEach(btn => {
      btn.addEventListener('click', () => {
        inspectBenchmark(btn.dataset.bid);
      });
    });
  }

  async function inspectBenchmark(bid) {
    if (!bid) return;
    try {
      const res = await fetch(`/api/benchmarks/${bid}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      activeBenchmarkDetail = data;
      renderBenchmarkResults(data);
    } catch (err) {
      console.warn('[Benchmark] Failed to inspect benchmark:', err);
    }
  }

  function renderActiveWorkOrder(state, orders) {
    const badge = byId('woActiveBadge');
    const container = byId('woActiveContainer');
    const swapBanner = byId('bannerPalletSwap');
    const swapDesc = byId('bannerPalletSwapDesc');

    if (!badge || !container) return;

    // Find active order from telemetry state or orders list
    const activeWoId = state && state.work_order ? state.work_order.order_id : null;
    let activeWo = null;

    if (activeWoId && orders && orders.length > 0) {
      activeWo = orders.find(o => o.order_id === activeWoId);
    }
    if (!activeWo && orders && orders.length > 0) {
      activeWo = orders.find(o => o.status === 'in_progress' || o.status === 'paused_pallet_change');
    }

    const isSwapRequired = (state && state.pallet_change_required) || (activeWo && activeWo.status === 'paused_pallet_change');

    // Handle empty state
    if (!activeWo) {
      badge.className = 'status-pill pill-neutral';
      badge.textContent = 'NO ACTIVE ORDER';
      container.innerHTML = `
        <div class="wo-empty-state">
          <span class="wo-empty-icon">📋</span>
          <div class="wo-empty-text">No active work order executing. Select a recipe and target quantity to launch an order.</div>
        </div>`;
      if (swapBanner) swapBanner.style.display = 'none';
      return;
    }

    // Active order exists
    const status = activeWo.status || 'in_progress';
    badge.className = `status-pill pill-${status}`;
    badge.textContent = status.toUpperCase().replace(/_/g, ' ');

    const targetQty = activeWo.target_quantity || 8;
    const completedQty = activeWo.completed_quantity || 0;
    const pct = targetQty > 0 ? Math.min(100, Math.round((completedQty / targetQty) * 100)) : 0;
    const palletIdx = (state && state.current_pallet_index) ? state.current_pallet_index : 1;

    let cancelBtnHtml = '';
    if (status === 'in_progress' || status === 'paused_pallet_change') {
      cancelBtnHtml = `<button class="btn-wo-cancel" id="btnCancelActiveOrder" data-orderid="${activeWo.order_id}">✕ Cancel Order</button>`;
    }

    container.innerHTML = `
      <div class="wo-active-card">
        <div class="wo-active-header">
          <div class="wo-order-title-group">
            <span class="wo-order-title">${activeWo.order_number}</span>
            <span class="wo-recipe-tag">RECIPE: ${activeWo.recipe_id}</span>
          </div>
          <div>
            ${cancelBtnHtml}
          </div>
        </div>

        <div class="wo-metrics-grid">
          <div class="wo-metric-item">
            <span class="wo-metric-label">PARTS COMPLETED</span>
            <span class="wo-metric-value">${completedQty} / ${targetQty}</span>
            <span class="wo-metric-sub">${pct}% fulfilled</span>
          </div>
          <div class="wo-metric-item">
            <span class="wo-metric-label">ACTIVE PALLET</span>
            <span class="wo-metric-value">Pallet #${palletIdx}</span>
            <span class="wo-metric-sub">${isSwapRequired ? 'Swap required' : 'In position'}</span>
          </div>
          <div class="wo-metric-item">
            <span class="wo-metric-label">STATUS</span>
            <span class="wo-metric-value" style="font-size: 13px; color: var(--accent-cyan);">${status}</span>
            <span class="wo-metric-sub">Auto-sequenced</span>
          </div>
          <div class="wo-metric-item">
            <span class="wo-metric-label">CREATED</span>
            <span class="wo-metric-value" style="font-size: 11px;">${(activeWo.created_at || '').slice(11, 19) || '-'}</span>
            <span class="wo-metric-sub">${(activeWo.created_at || '').slice(0, 10) || 'UTC'}</span>
          </div>
        </div>

        <div class="wo-progress-section">
          <div class="wo-progress-header">
            <span>BATCH COMPLETION PROGRESS</span>
            <span style="color: var(--accent-cyan); font-weight: 700;">${pct}%</span>
          </div>
          <div class="wo-progress-track">
            <div class="wo-progress-fill" style="width: ${pct}%;"></div>
          </div>
        </div>
      </div>`;

    // Hook up active cancel button
    const btnCancel = byId('btnCancelActiveOrder');
    if (btnCancel) {
      btnCancel.addEventListener('click', async () => {
        if (!confirm(`Cancel active work order ${activeWo.order_number}?`)) return;
        try {
          const res = await fetch(`/api/work-orders/${activeWo.order_id}/cancel`, { method: 'POST' });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          fetchDashboardData();
        } catch (e) {
          alert('Failed to cancel work order: ' + e.message);
        }
      });
    }

    // Toggle swap banner
    if (swapBanner) {
      if (isSwapRequired) {
        swapBanner.style.display = 'flex';
        if (swapDesc) {
          swapDesc.textContent = `Pallet #${palletIdx} reached full capacity. Place empty pallet and confirm swap to place remaining ${targetQty - completedQty} parts.`;
        }
      } else {
        swapBanner.style.display = 'none';
      }
    }
  }

  function renderWorkOrdersTable(orders) {
    const tbody = byId('workOrdersTableBody');
    if (!tbody) return;

    if (!orders || orders.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" class="events-empty">No work orders recorded in database yet.</td></tr>';
      return;
    }

    let rowsHtml = '';
    orders.forEach((o) => {
      const statusClass = `pill-${o.status || 'pending'}`;
      const statusText = (o.status || 'pending').toUpperCase().replace(/_/g, ' ');
      const createdStr = o.created_at ? o.created_at.replace('T', ' ').slice(0, 19) : '-';
      const completedStr = o.completed_at ? o.completed_at.replace('T', ' ').slice(0, 19) : (o.status === 'in_progress' ? 'executing...' : '-');
      const target = o.target_quantity || 8;
      const completed = o.completed_quantity || 0;
      const pct = target > 0 ? Math.min(100, Math.round((completed / target) * 100)) : 0;
      const estPallets = Math.ceil(target / 8);

      let actionBtns = `<button class="btn-inspect-run btn-order-summary" data-orderid="${o.order_id}">Summary</button>`;
      if (o.status === 'pending') {
        actionBtns += ` <button class="btn-ctrl btn-order-start" data-orderid="${o.order_id}" style="margin-left: 4px;">▶ Start</button>`;
      } else if (o.status === 'paused_pallet_change') {
        actionBtns += ` <button class="btn-ctrl btn-order-swap" data-orderid="${o.order_id}" style="margin-left: 4px; border-color: var(--accent-amber); color: var(--accent-amber);">📦 Swap</button>`;
      }

      rowsHtml += `
        <tr>
          <td><strong style="color: #fff; font-family: var(--font-mono);">${o.order_number}</strong></td>
          <td><span class="badge-origin">${o.recipe_id} v${o.recipe_version || '1.0.0'}</span></td>
          <td>
            <div style="display: flex; align-items: center; gap: 8px;">
              <span class="mono-val">${completed} / ${target}</span>
              <div style="width: 50px; height: 6px; background: rgba(255,255,255,0.08); border-radius: 3px; overflow: hidden;">
                <div style="width: ${pct}%; height: 100%; background: var(--accent-cyan);"></div>
              </div>
            </div>
          </td>
          <td class="mono-val">${estPallets} ${estPallets > 1 ? 'pallets' : 'pallet'}</td>
          <td><span class="status-pill ${statusClass}">${statusText}</span></td>
          <td class="mono-val" style="color: var(--text-dim);">${createdStr}</td>
          <td class="mono-val" style="color: var(--text-dim);">${completedStr}</td>
          <td style="text-align: right;">
            ${actionBtns}
          </td>
        </tr>`;
    });

    tbody.innerHTML = rowsHtml;

    // Hook summary buttons
    tbody.querySelectorAll('.btn-order-summary').forEach((b) => {
      b.addEventListener('click', () => openOrderSummaryModal(b.dataset.orderid));
    });

    // Hook start buttons
    tbody.querySelectorAll('.btn-order-start').forEach((b) => {
      b.addEventListener('click', async () => {
        try {
          const res = await fetch(`/api/work-orders/${b.dataset.orderid}/start`, { method: 'POST' });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          fetchDashboardData();
        } catch (e) {
          alert('Failed to start work order: ' + e.message);
        }
      });
    });

    // Hook swap buttons
    tbody.querySelectorAll('.btn-order-swap').forEach((b) => {
      b.addEventListener('click', async () => {
        try {
          const res = await fetch(`/api/work-orders/${b.dataset.orderid}/swap-pallet`, { method: 'POST' });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          fetchDashboardData();
        } catch (e) {
          alert('Failed to swap pallet: ' + e.message);
        }
      });
    });
  }

  async function openOrderSummaryModal(orderId) {
    const modal = byId('modalOrderSummary');
    const subTitle = byId('modalOrderSubtitle');
    const body = byId('modalOrderBody');
    if (!modal || !body) return;

    modal.style.display = 'flex';
    subTitle.textContent = `Loading summary for ${orderId}...`;
    body.innerHTML = '<div class="events-empty">Loading production summary &amp; traceability records...</div>';

    try {
      const res = await fetch(`/api/work-orders/${orderId}/summary`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      activeWorkOrderSummary = data;

      const wo = data.order || {};
      const metrics = data.metrics || {};
      subTitle.textContent = `Order: ${wo.order_number || orderId} · Recipe: ${wo.recipe_id} (v${wo.recipe_version || '1.0.0'}) · Status: ${wo.status}`;

      let html = `
        <!-- Summary KPIs -->
        <div class="wo-summary-kpis">
          <div class="wo-kpi-card">
            <span class="wo-kpi-label">TOTAL FULFILLED</span>
            <span class="wo-kpi-value">${data.total_completed} / ${data.total_target}</span>
            <span class="wo-kpi-sub">${data.total_target > 0 ? Math.round((data.total_completed / data.total_target) * 100) : 0}% completion</span>
          </div>
          <div class="wo-kpi-card">
            <span class="wo-kpi-label">PALLETS UTILIZED</span>
            <span class="wo-kpi-value">${data.total_pallets_used}</span>
            <span class="wo-kpi-sub">${data.runs_count || 0} run executions</span>
          </div>
          <div class="wo-kpi-card">
            <span class="wo-kpi-label">ELAPSED TIME</span>
            <span class="wo-kpi-value">${metrics.elapsed_seconds || 0}s</span>
            <span class="wo-kpi-sub">Active: ${metrics.active_motion_seconds || 0}s</span>
          </div>
          <div class="wo-kpi-card">
            <span class="wo-kpi-label">AVG CYCLE TIME</span>
            <span class="wo-kpi-value">${metrics.avg_cycle_sec || 0}s</span>
            <span class="wo-kpi-sub">P95: ${metrics.p95_cycle_sec || 0}s</span>
          </div>
        </div>

        <!-- Pallets Breakdown -->
        <div class="wo-section-heading">📦 PALLET BREAKDOWN (${(data.pallets || []).length} Pallets)</div>
        <div class="wo-pallets-grid">`;

      if (data.pallets && data.pallets.length > 0) {
        data.pallets.forEach((p) => {
          html += `
            <div class="wo-pallet-card">
              <div class="wo-pallet-card-header">
                <span>PALLET #${p.pallet_index}</span>
                <span style="color: var(--accent-cyan);">${p.parts_count} parts</span>
              </div>
              <div class="wo-pallet-card-detail">
                ${p.part_serials ? p.part_serials.join(', ') : ''}
              </div>
            </div>`;
        });
      } else {
        html += '<div class="events-empty" style="padding: 10px;">No pallets utilized yet.</div>';
      }

      html += `
        </div>

        <!-- Traceability Table -->
        <div class="wo-section-heading">🏷️ PART TRACEABILITY &amp; SERIAL REGISTRY (${(data.parts || []).length} Parts)</div>
        <div class="table-scroll" style="max-height: 240px;">
          <table class="data-table">
            <thead>
              <tr>
                <th>SERIAL NUMBER</th>
                <th>PALLET</th>
                <th>SLOT (F:R:C)</th>
                <th>CYCLE DURATION</th>
                <th>PLACED AT (UTC)</th>
                <th>RUN ID</th>
              </tr>
            </thead>
            <tbody>`;

      if (data.parts && data.parts.length > 0) {
        data.parts.forEach((p) => {
          const placedStr = p.placed_at ? p.placed_at.replace('T', ' ').slice(0, 19) : '-';
          const durStr = p.cycle_duration !== null ? `${Number(p.cycle_duration).toFixed(2)}s` : '-';
          const shortRun = p.run_id ? p.run_id.slice(0, 8) : '-';
          html += `
            <tr>
              <td><span class="wo-part-sn-code">${p.part_serial}</span></td>
              <td class="mono-val">Pallet #${p.pallet_index}</td>
              <td><span class="wo-slot-badge">L${p.slot_level}:R${p.slot_row}:C${p.slot_col} (#${p.slot_index + 1})</span></td>
              <td class="mono-val">${durStr}</td>
              <td class="mono-val" style="color: var(--text-dim);">${placedStr}</td>
              <td><code class="run-id-code" title="${p.run_id}">${shortRun}</code></td>
            </tr>`;
        });
      } else {
        html += '<tr><td colspan="6" class="events-empty">No parts placed for this order yet.</td></tr>';
      }

      html += `
            </tbody>
          </table>
        </div>`;

      body.innerHTML = html;
    } catch (err) {
      body.innerHTML = `<div class="events-empty" style="color: var(--accent-crimson);">Failed to load order summary: ${err.message}</div>`;
    }
  }

  function exportOrderSummaryCsv() {
    if (!activeWorkOrderSummary || !activeWorkOrderSummary.parts || activeWorkOrderSummary.parts.length === 0) {
      alert('No part traceability records available to export.');
      return;
    }
    const headers = ['part_serial', 'order_id', 'pallet_index', 'slot_index', 'slot_level', 'slot_row', 'slot_col', 'cycle_duration', 'placed_at', 'run_id'];
    const lines = [headers.join(',')];

    activeWorkOrderSummary.parts.forEach((p) => {
      const row = headers.map((h) => {
        const val = p[h] !== null && p[h] !== undefined ? String(p[h]).replace(/"/g, '""') : '';
        return `"${val}"`;
      });
      lines.push(row.join(','));
    });

    const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
    const orderNum = activeWorkOrderSummary.order ? activeWorkOrderSummary.order.order_number : 'order';
    downloadBlob(blob, `traceability_${orderNum}_${Date.now()}.csv`);
  }

  function exportOrderSummaryJson() {
    if (!activeWorkOrderSummary) {
      alert('No order summary data available to export.');
      return;
    }
    const blob = new Blob([JSON.stringify(activeWorkOrderSummary, null, 2)], { type: 'application/json' });
    const orderNum = activeWorkOrderSummary.order ? activeWorkOrderSummary.order.order_number : 'order';
    downloadBlob(blob, `summary_${orderNum}_${Date.now()}.json`);
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
      event_stream: lastEventsData,
    };
    const blob = new Blob([JSON.stringify(exportBundle, null, 2)], { type: 'application/json' });
    downloadBlob(blob, `production_analytics_${Date.now()}.json`);
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
    fetchBenchmarksHistory();
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

    // 4. Export Buttons (Runs)
    const btnExpCsv = byId('btnExportCsv');
    if (btnExpCsv) btnExpCsv.addEventListener('click', exportCsv);
    const btnExpJson = byId('btnExportJson');
    if (btnExpJson) btnExpJson.addEventListener('click', exportJson);

    // 4b. Export Buttons (Work Order Summary)
    const btnExpOrderCsv = byId('btnExportOrderCsv');
    if (btnExpOrderCsv) btnExpOrderCsv.addEventListener('click', exportOrderSummaryCsv);
    const btnExpOrderJson = byId('btnExportOrderJson');
    if (btnExpOrderJson) btnExpOrderJson.addEventListener('click', exportOrderSummaryJson);

    // 5. Run Inspector Modal Close
    const btnCloseModal = byId('btnCloseRunModal');
    const modal = byId('modalRunDetails');
    if (btnCloseModal && modal) {
      btnCloseModal.addEventListener('click', () => {
        modal.style.display = 'none';
      });
      modal.addEventListener('click', (e) => {
        if (e.target === modal) modal.style.display = 'none';
      });
    }

    // 5b. Work Order Summary Modal Close
    const btnCloseOrderModal = byId('btnCloseOrderModal');
    const modalOrder = byId('modalOrderSummary');
    if (btnCloseOrderModal && modalOrder) {
      btnCloseOrderModal.addEventListener('click', () => {
        modalOrder.style.display = 'none';
      });
      modalOrder.addEventListener('click', (e) => {
        if (e.target === modalOrder) modalOrder.style.display = 'none';
      });
    }

    // 5c. Create Work Order Form & Preset Buttons
    const inpTarget = byId('inpOrderTarget');
    document.querySelectorAll('.btn-preset').forEach((btn) => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.btn-preset').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        if (inpTarget && btn.dataset.qty) {
          inpTarget.value = btn.dataset.qty;
        }
      });
    });

    const btnCreateOrder = byId('btnSubmitCreateOrder');
    if (btnCreateOrder) {
      btnCreateOrder.addEventListener('click', async () => {
        const selRecipe = byId('selOrderRecipe');
        const recipeId = selRecipe ? selRecipe.value : '';
        const targetQty = parseInt(inpTarget ? inpTarget.value : '8', 10);
        const notes = byId('inpOrderNotes') ? byId('inpOrderNotes').value.trim() : '';

        if (!recipeId) {
          alert('Please select a recipe.');
          return;
        }

        btnCreateOrder.disabled = true;
        btnCreateOrder.innerHTML = '<span>⏳</span> CREATING...';
        try {
          const res = await fetch('/api/work-orders', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              recipe_id: recipeId,
              target_quantity: targetQty,
              notes: notes,
              start_immediately: true,
            }),
          });
          if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || `HTTP ${res.status}`);
          }
          await fetchDashboardData();
        } catch (err) {
          alert('Failed to create order: ' + err.message);
        } finally {
          btnCreateOrder.disabled = false;
          btnCreateOrder.innerHTML = '<span>🚀</span> CREATE &amp; START ORDER';
        }
      });
    }

    // 5d. Pallet Swap Button
    const btnSwapAction = byId('btnActionSwapPallet');
    if (btnSwapAction) {
      btnSwapAction.addEventListener('click', async () => {
        btnSwapAction.disabled = true;
        btnSwapAction.innerHTML = '<span>⏳</span> CONFIRMING SWAP...';
        try {
          const res = await fetch('/api/pallet/swap', { method: 'POST' });
          if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || `HTTP ${res.status}`);
          }
          setTimeout(fetchDashboardData, 400);
        } catch (err) {
          alert('Failed to confirm pallet swap: ' + err.message);
        } finally {
          btnSwapAction.disabled = false;
          btnSwapAction.innerHTML = '<span>📦</span> SWAP PALLET &amp; CONTINUE';
        }
      });
    }

    // 5e. Refresh Orders Button
    const btnRefreshOrders = byId('btnRefreshOrders');
    if (btnRefreshOrders) {
      btnRefreshOrders.addEventListener('click', () => {
        fetchDashboardData();
      });
    }

    // 5f. Benchmark Controls
    const btnRunBench = byId('btnRunBenchmark');
    if (btnRunBench) {
      btnRunBench.addEventListener('click', async () => {
        const baseId = byId('selBenchBaseRecipe') ? byId('selBenchBaseRecipe').value : '';
        const candId = byId('selBenchCandRecipe') ? byId('selBenchCandRecipe').value : '';
        const trials = parseInt(byId('inpBenchTrials') ? byId('inpBenchTrials').value : '5', 10);

        if (!baseId || !candId) {
          alert('Please select both a baseline and a candidate recipe.');
          return;
        }

        btnRunBench.disabled = true;
        const icon = byId('benchRunIcon');
        const text = byId('benchRunText');
        if (icon) icon.textContent = '⏳';
        if (text) text.textContent = 'COMPUTING KINEMATICS & TRIALS...';
        const statusBadge = byId('benchStatusBadge');
        if (statusBadge) statusBadge.textContent = 'RUNNING';

        try {
          const res = await fetch('/api/benchmarks/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              name: `Optimization: ${baseId} vs ${candId}`,
              baseline_recipe_id: baseId,
              candidate_recipe_id: candId,
              trials: trials,
              fast_mode: true,
            }),
          });
          if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || `HTTP ${res.status}`);
          }
          const data = await res.json();
          activeBenchmarkDetail = data;
          renderBenchmarkResults(data);
          await fetchBenchmarksHistory();
          if (statusBadge) {
            statusBadge.textContent = 'COMPLETED';
            setTimeout(() => { if (statusBadge) statusBadge.textContent = 'READY'; }, 3000);
          }
        } catch (err) {
          alert('Benchmark failed: ' + err.message);
          if (statusBadge) statusBadge.textContent = 'FAILED';
        } finally {
          btnRunBench.disabled = false;
          if (icon) icon.textContent = '⚡';
          if (text) text.textContent = 'RUN BENCHMARK EXPERIMENT';
        }
      });
    }

    const btnRefreshBench = byId('btnRefreshBenchmarks');
    if (btnRefreshBench) {
      btnRefreshBench.addEventListener('click', () => {
        fetchBenchmarksHistory();
      });
    }

    // 6. Event Filter Buttons
    document.querySelectorAll('.btn-filter').forEach((btn) => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.btn-filter').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        activeEventFilter = btn.dataset.filter || 'all';
        renderEventsFeed(lastEventsData, activeEventFilter);
      });
    });

    // 7. Quick Badge Click (if present on 3D twin page, navigates to /production)
    const quickBadge = byId('kpiQuickBadge');
    if (quickBadge) {
      quickBadge.addEventListener('click', () => {
        window.location.href = '/production';
      });
    }

    // 8. Instant Refresh on Telemetry & Cycle Events
    window.addEventListener('workcell-telemetry', () => {
      if (document.visibilityState === 'visible' && Math.random() < 0.2) {
        renderQuickBadge(lastKpiData);
      }
    });

    window.addEventListener('workcell-command', () => {
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
