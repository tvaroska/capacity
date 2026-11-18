function switchTab(panelId, btnEl) {
  document.querySelectorAll('.tab-panel').forEach((el) => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach((el) => el.classList.remove('active'));
  const target = document.getElementById(panelId);
  if (target) target.classList.add('active');
  if (btnEl) btnEl.classList.add('active');
}

function mainDataset() {
  const mainEl = document.querySelector('main');
  return mainEl ? mainEl.dataset : {};
}

function isLiveApiEnabled() {
  return mainDataset().liveApi === 'true';
}

function readNum(id) {
  const el = document.getElementById(id);
  if (!el) return Number.NaN;
  const raw = String(el.value).trim();
  if (raw === '') return Number.NaN;
  return Number(raw);
}

function hasInvalidNums(ids) {
  return ids.some((id) => !Number.isFinite(readNum(id)));
}

function renderCardError(badgeIds, bannerId, badgeLabel, message) {
  for (const id of badgeIds) {
    const badge = document.getElementById(id);
    if (badge) {
      badge.className = 'badge badge-danger';
      badge.textContent = badgeLabel;
    }
  }
  const banner = document.getElementById(bannerId);
  if (banner) {
    banner.className = 'danger-banner';
    banner.textContent = message;
  }
}

function formatApiDetail(detail, status) {
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0];
    const loc = Array.isArray(first.loc) ? first.loc[first.loc.length - 1] : '';
    const msg = first.msg || 'Invalid value';
    return loc ? `${loc}: ${msg}` : msg;
  }
  return `API request failed (HTTP ${status})`;
}

const DEBOUNCE_MS = 150;
const _handlerTurns = {};

function cancelHandlerTurn(key) {
  const prev = _handlerTurns[key];
  if (!prev) return;
  prev.cancelled = true;
  if (prev.timer) {
    clearTimeout(prev.timer);
    prev.timer = null;
  }
  if (prev.wake) {
    const wake = prev.wake;
    prev.wake = null;
    wake(false);
  }
  if (prev.controller) {
    prev.controller.abort();
    prev.controller = null;
  }
}

async function beginHandlerTurn(key) {
  cancelHandlerTurn(key);
  const turn = { cancelled: false, timer: null, wake: null, controller: null };
  _handlerTurns[key] = turn;
  const proceed = await new Promise((resolve) => {
    turn.wake = resolve;
    turn.timer = setTimeout(() => {
      turn.timer = null;
      turn.wake = null;
      resolve(!turn.cancelled);
    }, DEBOUNCE_MS);
  });
  if (!proceed || turn.cancelled || _handlerTurns[key] !== turn) return null;
  turn.controller = new AbortController();
  return turn.controller.signal;
}

async function postJson(path, payload, signal) {
  let resp;
  try {
    resp = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal,
    });
  } catch (err) {
    if ((signal && signal.aborted) || (err && err.name === 'AbortError')) {
      return { ok: false, aborted: true };
    }
    const reason = err && err.message ? err.message : 'Network error';
    return { ok: false, aborted: false, error: `Request failed: ${reason}` };
  }
  if (signal && signal.aborted) {
    return { ok: false, aborted: true };
  }
  if (!resp.ok) {
    let detail = null;
    try {
      const errBody = await resp.json();
      detail = errBody ? errBody.detail : null;
    } catch (_parseErr) {
      detail = null;
    }
    return { ok: false, aborted: false, error: formatApiDetail(detail, resp.status) };
  }
  const data = await resp.json();
  if (signal && signal.aborted) {
    return { ok: false, aborted: true };
  }
  return { ok: true, aborted: false, data };
}

async function recalcDisagg() {
  if (!isLiveApiEnabled()) return;
  if (hasInvalidNums(['pd-qps', 'pd-in', 'pd-out', 'pd-ttft'])) {
    cancelHandlerTurn('disagg');
    renderCardError(
      ['kpi-disagg-badge'],
      'pd-output',
      'Invalid Input',
      'Enter a finite number in every Disaggregated Pool field.',
    );
    return;
  }
  const signal = await beginHandlerTurn('disagg');
  if (!signal) return;
  const ds = mainDataset();
  const res = await postJson(
    '/api/v1/self-hosted/disaggregated',
    {
      profile: ds.disaggProfile,
      peak_qps: readNum('pd-qps'),
      avg_input_tokens: Math.trunc(readNum('pd-in')),
      avg_output_tokens: Math.trunc(readNum('pd-out')),
      target_ttft_ms: readNum('pd-ttft'),
      target_tpot_ms: Number(ds.disaggTpotMs),
    },
    signal,
  );
  if (res.aborted || signal.aborted) return;
  if (!res.ok) {
    renderCardError(['kpi-disagg-badge'], 'pd-output', 'Request Error', res.error);
    return;
  }
  const data = res.data;

  const repsEl = document.getElementById('kpi-disagg-reps');
  if (repsEl) repsEl.textContent = `${data.prefill_replicas}P + ${data.decode_replicas}D`;
  const badgeEl = document.getElementById('kpi-disagg-badge');
  if (badgeEl) {
    if (data.replica_delta > 0) {
      badgeEl.className = 'badge badge-green';
      badgeEl.textContent = `Saves ${data.replica_delta} Replica(s) vs Monolithic`;
    } else {
      badgeEl.className = 'badge badge-warn';
      badgeEl.textContent =
        `Monolithic Cheaper (${data.monolithic_replicas} vs ${data.total_disaggregated_replicas} Reps)`;
    }
  }
  const outEl = document.getElementById('pd-output');
  if (outEl) {
    outEl.className = 'info-banner';
    outEl.textContent = data.recommendation;
  }
}

async function recalcBurstAndCache() {
  if (!isLiveApiEnabled()) return;
  const simFields = [
    'sim-gsus',
    'sim-60s-tpm',
    'sim-peak-tps',
    'sim-total-in',
    'sim-static-in',
    'sim-dyn-offset',
  ];
  if (hasInvalidNums(simFields)) {
    cancelHandlerTurn('burst');
    renderCardError(
      ['burst-badge', 'cache-block0-badge'],
      'sim-remediation-box',
      'Invalid Input',
      'Enter a finite number in every Simulator field.',
    );
    return;
  }
  const signal = await beginHandlerTurn('burst');
  if (!signal) return;
  const ds = mainDataset();
  const model = document.getElementById('sim-model').value;
  const mode = document.getElementById('sim-strip-sig').value;
  const dynOffset = Math.trunc(readNum('sim-dyn-offset'));

  const [burstRes, cacheRes] = await Promise.all([
    postJson(
      '/api/v1/burst/window',
      {
        model,
        provisioned_gsus: Math.trunc(readNum('sim-gsus')),
        sixty_sec_tpm: readNum('sim-60s-tpm'),
        peak_1s_tps: readNum('sim-peak-tps'),
      },
      signal,
    ),
    postJson(
      '/api/v1/prefix-cache/model-tier',
      {
        model,
        total_input_tokens: Math.trunc(readNum('sim-total-in')),
        static_prefix_tokens: Math.trunc(readNum('sim-static-in')),
        output_tokens: Number(ds.cacheOutTokens),
        thoughts_tokens: Number(ds.cacheThoughtsTokens),
        dynamic_header_offset_tokens: dynOffset,
        single_turn_request: mode === 'single_unanchored',
        dummy_turn_anchor_applied: mode !== 'single_unanchored',
        strip_thought_signatures: mode === 'strip_sig',
        num_replicas: Number(ds.cacheReplicas),
      },
      signal,
    ),
  ]);
  if (burstRes.aborted || cacheRes.aborted || signal.aborted) return;
  if (!burstRes.ok || !cacheRes.ok) {
    const errMsg = !burstRes.ok ? burstRes.error : cacheRes.error;
    renderCardError(['burst-badge', 'cache-block0-badge'], 'sim-remediation-box', 'Request Error', errMsg);
    return;
  }
  const burst = burstRes.data;
  const cache = cacheRes.data;

  if (burst) {
    const valEl = document.getElementById('burst-60s-val');
    if (valEl) {
      valEl.textContent =
        `${burst.apparent_60s_utilization_pct.toFixed(1)}% \u2192 ${burst.peak_window_utilization_pct.toFixed(1)}%`;
    }
    const bBadge = document.getElementById('burst-badge');
    if (bBadge) {
      if (burst.illusion_detected) {
        bBadge.className = 'badge badge-danger';
        bBadge.textContent = `60s Illusion Detected (${burst.refill_window_seconds}s Bucket)`;
      } else if (burst.triggers_429_or_spillover) {
        bBadge.className = 'badge badge-warn';
        bBadge.textContent = `Spillover Active (${burst.refill_window_seconds}s Bucket)`;
      } else {
        bBadge.className = 'badge badge-green';
        bBadge.textContent = `Within ${burst.refill_window_seconds}s Token Bucket`;
      }
    }
    const capEl = document.getElementById('burst-caption');
    if (capEl) {
      capEl.textContent =
        `Requires ${burst.gsus_required_for_zero_spillover} GSUs for zero 429s/spillover`;
    }
  }

  if (cache) {
    const curPct = (cache.effective_cache_hit_rate * 100).toFixed(1);
    const optPct = (cache.optimized_cache_hit_rate * 100).toFixed(1);
    const hitEl = document.getElementById('cache-hit-val');
    if (hitEl) hitEl.textContent = `${curPct}% \u2192 ${optPct}%`;

    const cBadge = document.getElementById('cache-block0-badge');
    const remBox = document.getElementById('sim-remediation-box');
    const firstStep = cache.remediation_steps[0] || '';
    if (cache.block_zero_contaminated) {
      const bound = cache.checkpoint_granularity === 'token_boundary'
        ? cache.min_prefix_floor_tokens
        : cache.block_size_tokens;
      if (cBadge) {
        cBadge.className = 'badge badge-danger';
        cBadge.textContent = `Dynamic Token @ Offset ${dynOffset.toLocaleString()} (<${bound.toLocaleString()})`;
      }
      if (remBox) {
        remBox.className = 'danger-banner';
        remBox.textContent =
          `Block 0 Contamination Active: ${firstStep} (cuts weighted burndown waste by ${cache.burndown_waste_pct.toFixed(1)}%).`;
      }
    } else if (cache.below_minimum_floor) {
      if (cBadge) {
        cBadge.className = 'badge badge-warn';
        cBadge.textContent = `Below Cache Floor (<${cache.min_prefix_floor_tokens.toLocaleString()} tok)`;
      }
      if (remBox) {
        remBox.className = 'warn-banner';
        remBox.textContent = firstStep;
      }
    } else if (cache.strip_thought_signatures) {
      if (cBadge) {
        cBadge.className = 'badge badge-warn';
        cBadge.textContent = 'thought_signature Stripped';
      }
      if (remBox) {
        remBox.className = 'warn-banner';
        remBox.textContent = firstStep;
      }
    } else if (cache.single_turn_request && !cache.dummy_turn_anchor_applied) {
      if (cBadge) {
        cBadge.className = 'badge badge-warn';
        cBadge.textContent = 'Single-Turn 1/N Hash';
      }
      if (remBox) {
        remBox.className = 'warn-banner';
        remBox.textContent = firstStep;
      }
    } else {
      if (cBadge) {
        cBadge.className = 'badge badge-green';
        cBadge.textContent = `Prefix Cache Healthy (${curPct}% Hit)`;
      }
      if (remBox) {
        remBox.className = 'info-banner';
        remBox.textContent = firstStep;
      }
    }
  }
}

async function recalcGatewayAudit() {
  if (!isLiveApiEnabled()) return;
  if (hasInvalidNums(['gw-rps', 'gw-sse', 'gw-pods', 'gw-workers'])) {
    cancelHandlerTurn('gateway');
    renderCardError(
      ['gw-status-badge'],
      'gw-audit-msg',
      'Invalid Input',
      'Enter a finite number in every Gateway Scale field.',
    );
    return;
  }
  const signal = await beginHandlerTurn('gateway');
  if (!signal) return;
  const ds = mainDataset();
  const res = await postJson(
    '/api/v1/gateway/config',
    {
      model: ds.gwModel,
      project_id: ds.gwProject,
      location: ds.gwLocation,
      provisioned_gsus: Number(ds.gwGsus),
      target_rps: Math.trunc(readNum('gw-rps')),
      concurrent_sse_streams: Math.trunc(readNum('gw-sse')),
      pods: Math.trunc(readNum('gw-pods')),
      uvicorn_workers_per_pod: Math.trunc(readNum('gw-workers')),
      self_hosted_model: ds.gwSelfHostedModel,
      business_unit_shares: Number(ds.gwBuShares),
    },
    signal,
  );
  if (res.aborted || signal.aborted) return;
  if (!res.ok) {
    renderCardError(['gw-status-badge'], 'gw-audit-msg', 'Request Error', res.error);
    return;
  }
  const bundle = res.data;

  const audit = bundle.litellm_audit;
  const valEl = document.getElementById('gw-prisma-val');
  if (valEl) valEl.textContent = `${audit.total_prisma_rust_engines} Engines`;
  const pgEl = document.getElementById('gw-pg-caption');
  if (pgEl) {
    pgEl.textContent =
      `PgBouncer pool=${audit.recommended_pgbouncer_pool_size}, Postgres max_conns\u2265${audit.recommended_postgres_max_connections}`;
  }
  const cdEl = document.getElementById('gw-cooldown-val');
  if (cdEl) cdEl.textContent = `${audit.anti_blackhole_cooldown_seconds}s Cooldown`;

  const badge = document.getElementById('gw-status-badge');
  const msg = document.getElementById('gw-audit-msg');
  if (audit.scale_status === 'migrate_dataplane_to_envoy') {
    if (badge) {
      badge.className = 'badge badge-danger';
      badge.textContent = 'Migrate Data Plane to Envoy AI Gateway';
    }
    if (msg) msg.className = 'danger-banner';
  } else if (audit.scale_status === 'tune_required') {
    if (badge) {
      badge.className = 'badge badge-warn';
      badge.textContent = 'Tune Required';
    }
    if (msg) msg.className = 'warn-banner';
  } else {
    if (badge) {
      badge.className = 'badge badge-green';
      badge.textContent = 'Healthy Scale Envelope';
    }
    if (msg) msg.className = 'info-banner';
  }
  if (msg && audit.bottleneck_warnings.length > 0) {
    msg.textContent = audit.bottleneck_warnings[audit.bottleneck_warnings.length - 1];
  }
}
