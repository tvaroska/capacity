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
  return el ? Number(el.value) : 0;
}

async function postJson(path, payload) {
  const resp = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) return null;
  return await resp.json();
}

async function recalcDisagg() {
  if (!isLiveApiEnabled()) return;
  const ds = mainDataset();
  const data = await postJson('/api/v1/self-hosted/disaggregated', {
    profile: ds.disaggProfile,
    peak_qps: readNum('pd-qps'),
    avg_input_tokens: Math.trunc(readNum('pd-in')),
    avg_output_tokens: Math.trunc(readNum('pd-out')),
    target_ttft_ms: readNum('pd-ttft'),
    target_tpot_ms: Number(ds.disaggTpotMs),
  });
  if (!data) return;

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
  if (outEl) outEl.textContent = data.recommendation;
}

async function recalcBurstAndCache() {
  if (!isLiveApiEnabled()) return;
  const ds = mainDataset();
  const model = document.getElementById('sim-model').value;
  const mode = document.getElementById('sim-strip-sig').value;
  const dynOffset = Math.trunc(readNum('sim-dyn-offset'));

  const [burst, cache] = await Promise.all([
    postJson('/api/v1/burst/window', {
      model,
      provisioned_gsus: Math.trunc(readNum('sim-gsus')),
      sixty_sec_tpm: readNum('sim-60s-tpm'),
      peak_1s_tps: readNum('sim-peak-tps'),
    }),
    postJson('/api/v1/prefix-cache/model-tier', {
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
    }),
  ]);

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
  const ds = mainDataset();
  const bundle = await postJson('/api/v1/gateway/config', {
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
  });
  if (!bundle) return;

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
