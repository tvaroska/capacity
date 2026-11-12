"""Client-side interactive simulator script for the GM3 Light 4-Tab Workbench."""

from __future__ import annotations

PORTAL_CLIENT_SCRIPT = """
  <script>
    window.CAPACITY_SHOWCASE_DATA = __EMBEDDED_JSON__;

    function switchTab(panelId, btnEl) {
      document.querySelectorAll('.tab-panel').forEach(el => el.classList.remove('active'));
      document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
      document.getElementById(panelId).classList.add('active');
      btnEl.classList.add('active');
    }

    function numVal(id, defVal) {
      return parseFloat(document.getElementById(id).value || defVal);
    }

    function recalcDisagg() {
      const qps = Math.max(0.01, numVal('pd-qps', '18'));
      const inTok = Math.max(1, Math.round(numVal('pd-in', '6400')));
      const outTok = Math.max(1, Math.round(numVal('pd-out', '450')));
      const ttft = Math.max(20, numVal('pd-ttft', '350'));

      const prefillScale = 4.5 * Math.min(1.0, ttft / 350.0);
      const prefillCap = 3200.0 * prefillScale;
      const reqPrefillTps = qps * inTok;
      const prefillReps = Math.max(1, Math.ceil(reqPrefillTps / prefillCap));
      const activeKv = qps * (outTok * 0.025) * (inTok + outTok);
      const kvSafe = 524288 * 0.80;
      const decodeReps = Math.max(
        1, Math.ceil(activeKv / kvSafe), Math.ceil((qps * outTok) / 800.0)
      );
      const totalDisagg = prefillReps + decodeReps;
      const monoWeighted = (
        (reqPrefillTps / Math.max(0.1, prefillScale)) + (qps * outTok * 4.0)
      ) * 1.30;
      const monoReps = Math.max(1, Math.ceil(monoWeighted / 3200.0), Math.ceil(activeKv / kvSafe));
      const saved = monoReps - totalDisagg;

      document.getElementById('kpi-disagg-reps').textContent =
        prefillReps + 'P + ' + decodeReps + 'D';
      const dBadge = document.getElementById('kpi-disagg-badge');
      if (saved > 0) {
        dBadge.className = 'badge badge-green';
        dBadge.textContent = 'Saves ' + saved + ' Replica(s) vs Monolithic';
        document.getElementById('pd-output').textContent =
          'Deploy ' + prefillReps + ' prefill + ' + decodeReps + ' decode replicas (' +
          totalDisagg + ' total vs ' + monoReps + ' monolithic, saving ' + saved +
          ' H100 replica[s] / $' + (saved * 18500).toLocaleString() +
          '/mo) behind GKE Inference Gateway (epp).';
      } else {
        dBadge.className = 'badge badge-warn';
        dBadge.textContent = 'Monolithic Cheaper at Low QPS';
        document.getElementById('pd-output').textContent =
          'Keep ' + monoReps + ' monolithic replica(s) at ' + qps.toFixed(2) +
          ' QPS (disaggregation requires ' + totalDisagg + ' minimum replicas).';
      }
    }

    function recalcBurstAndCache() {
      const model = document.getElementById('sim-model').value;
      const gsus = Math.max(1, Math.round(numVal('sim-gsus', '10')));
      const tpm60 = Math.max(0, numVal('sim-60s-tpm', '750000'));
      const peakTps = Math.max(0, numVal('sim-peak-tps', '25000'));
      const totalIn = Math.max(1, Math.round(numVal('sim-total-in', '18000')));
      const staticRaw = Math.round(numVal('sim-static-in', '14000'));
      const staticIn = Math.min(totalIn, Math.max(0, staticRaw));
      const dynOffset = Math.round(numVal('sim-dyn-offset', '120'));
      const mode = document.getElementById('sim-strip-sig').value;

      const isClaude = model.startsWith('claude-');
      let tpsPerGsu = 2015.0;
      if (model.includes('claude-haiku-5-5')) tpsPerGsu = 10417.0;
      else if (model.includes('claude-sonnet-5-5')) tpsPerGsu = 525.0;
      else if (model.includes('claude-opus-5-5')) tpsPerGsu = 261.0;
      else if (model.includes('claude-opus')) tpsPerGsu = 210.0;
      else if (isClaude) tpsPerGsu = 350.0;
      else if (model.includes('argon')) tpsPerGsu = 500.0;
      else if (model.includes('-fast')) tpsPerGsu = 1007.5;
      else if (model.includes('lite')) tpsPerGsu = 4030.0;

      const windowSec = isClaude ? 30 : (gsus >= 50 ? 1 : 10);
      const burstCredit = isClaude ? 0.0 : 0.05;
      const committedTps = gsus * tpsPerGsu;
      const ceilingTps = committedTps * (1.0 + burstCredit);
      const avgTps = tpm60 / 60.0;
      const rawPeak = Math.max(avgTps, peakTps);
      const conservedPeak = tpm60 > 0
        ? Math.min(rawPeak, tpm60 / windowSec)
        : (peakTps > 0 ? peakTps / windowSec : 0);
      const util60 = (avgTps / committedTps) * 100.0;
      const utilPeak = (conservedPeak / ceilingTps) * 100.0;
      const reqGsus = conservedPeak > 0
        ? Math.ceil(conservedPeak / (tpsPerGsu * (1.0 + burstCredit)))
        : 0;

      document.getElementById('burst-60s-val').textContent =
        util60.toFixed(1) + '% \u2192 ' + utilPeak.toFixed(1) + '%';
      const burstBadge = document.getElementById('burst-badge');
      if (util60 < 70.0 && utilPeak > 100.0) {
        burstBadge.className = 'badge badge-danger';
        burstBadge.textContent = '60s Illusion Detected (' + windowSec + 's Bucket)';
      } else if (utilPeak > 100.0) {
        burstBadge.className = 'badge badge-warn';
        burstBadge.textContent = 'Spillover Active (' + windowSec + 's Bucket)';
      } else {
        burstBadge.className = 'badge badge-green';
        burstBadge.textContent = 'Within ' + windowSec + 's Token Bucket';
      }
      document.getElementById('burst-caption').textContent =
        'Requires ' + reqGsus + ' GSUs for zero 429s/spillover';

      const isArgon = model.includes('argon');
      const isTokenBoundary = isArgon || isClaude;
      let minFloor = isArgon ? 2048 : 6144;
      if (isClaude) {
        if (model.endsWith('-5-5')) minFloor = 512;
        else if (model.includes('opus-4')) minFloor = 4096;
        else minFloor = 1024;
      }
      const blockSize = isTokenBoundary ? 1 : 4096;
      const effSpan = dynOffset < staticIn ? dynOffset : staticIn;
      const block0 = !isTokenBoundary && dynOffset < 4096;
      let aligned = 0;
      if (effSpan >= minFloor && !block0) {
        aligned = isTokenBoundary ? effSpan : Math.floor(effSpan / blockSize) * blockSize;
      }
      let affinity = 0.90;
      if (mode === 'single_unanchored') affinity = 0.125;
      else if (mode === 'strip_sig') affinity = 0.0077;
      const curHit = (Math.round(aligned * affinity) / totalIn) * 100.0;
      let optAligned = 0;
      if (staticIn >= minFloor) {
        optAligned = isTokenBoundary ? staticIn : Math.floor(staticIn / blockSize) * blockSize;
      }
      const optHit = (Math.round(optAligned * 0.90) / totalIn) * 100.0;

      document.getElementById('cache-hit-val').textContent =
        curHit.toFixed(1) + '% \u2192 ' + optHit.toFixed(1) + '%';
      const cBadge = document.getElementById('cache-block0-badge');
      const remBox = document.getElementById('sim-remediation-box');
      if (block0) {
        cBadge.className = 'badge badge-danger';
        cBadge.textContent = 'Block-0 Contaminated (Offset ' + dynOffset + ' < 4,096)';
        remBox.style.background = 'var(--danger-bg)';
        remBox.style.color = 'var(--danger)';
        remBox.textContent =
          'Block 0 Contamination Active: Dynamic token at offset ' + dynOffset +
          ' (<4,096) invalidates all 4,096-token blocks (0% cache hit). ' +
          'Move dynamic headers/UUIDs to the end of the prompt.';
      } else if (mode !== 'none') {
        cBadge.className = 'badge badge-warn';
        cBadge.textContent = mode === 'strip_sig'
          ? 'thought_signature Stripped'
          : 'Single-Turn 1/N Hash';
        remBox.style.background = 'var(--warning-bg)';
        remBox.style.color = 'var(--warning)';
        remBox.textContent = mode === 'strip_sig'
          ? 'Stripping thought_signature collapses multi-turn prefix cache hits ' +
            'to 0.77%. Preserve full modelContent across turns.'
          : 'Single-turn request misses prefix router affinity. Prepend a ' +
            '2-token static dummy (user + model) turn pair.';
      } else if (isClaude && effSpan < minFloor) {
        cBadge.className = 'badge badge-warn';
        cBadge.textContent = 'Below Cache Floor (<' + minFloor.toLocaleString() + ' tok)';
        remBox.style.background = 'var(--warning-bg)';
        remBox.style.color = 'var(--warning)';
        remBox.textContent =
          '3P Claude prompt caching requires an explicit cache_control breakpoint at >=' +
          minFloor.toLocaleString() + ' tokens (1.25x 5m / 2.0x 1h write vs 0.1x read; ' +
          '30s/60s dual bucket with 0% burst credit).';
      } else {
        cBadge.className = 'badge badge-green';
        cBadge.textContent = 'Prefix Cache Healthy (' + curHit.toFixed(1) + '% Hit)';
        remBox.style.background = 'var(--success-bg)';
        remBox.style.color = 'var(--success)';
        remBox.textContent = isClaude
          ? 'Prefix meets ' + minFloor.toLocaleString() + '-token floor (' +
            aligned.toLocaleString() + ' cacheable tokens). 3P Claude PT uses 30s/60s dual ' +
            'buckets (0% credit) and explicit cache_control (1.25x 5m / 2.0x 1h write).'
          : 'Prefix is aligned and routing affinity is active (' +
            aligned.toLocaleString() + ' cacheable prefix tokens).';
      }
    }

    function recalcGatewayAudit() {
      const rps = Math.max(1, Math.round(numVal('gw-rps', '650')));
      const sse = Math.max(0, Math.round(numVal('gw-sse', '400')));
      const pods = Math.max(1, Math.round(numVal('gw-pods', '12')));
      const workers = Math.max(1, Math.round(numVal('gw-workers', '4')));
      const prisma = pods * workers;
      const pgb = Math.max(20, Math.min(100, Math.floor(prisma / 2) + 10));
      const pgMax = Math.max(100, prisma + 40);

      document.getElementById('gw-prisma-val').textContent = prisma + ' Engines';
      document.getElementById('gw-pg-caption').textContent =
        'PgBouncer pool=' + pgb + ', Postgres max_conns\u2265' + pgMax;
      const badge = document.getElementById('gw-status-badge');
      const msg = document.getElementById('gw-audit-msg');
      if (rps > 1500 || sse > 1000) {
        badge.className = 'badge badge-danger';
        badge.textContent = 'Migrate Data Plane to Envoy AI Gateway';
        msg.style.background = 'var(--danger-bg)';
        msg.style.color = 'var(--danger)';
        msg.textContent =
          'At ' + rps + ' RPS / ' + sse + ' SSE streams, Python GIL and asyncio ' +
          'event-loop stalls inflate TTFT. Offload data-plane routing and CEL ' +
          'burndown rate limiting to Envoy AI Gateway.';
      } else if (rps >= 500 || sse >= 300 || prisma >= 32) {
        badge.className = 'badge badge-warn';
        badge.textContent = 'Tune Required (\u2265500 RPS)';
        msg.style.background = 'var(--warning-bg)';
        msg.style.color = 'var(--warning)';
        msg.textContent =
          prisma + ' Prisma Rust query engines active across ' + pods +
          ' pods. Enable use_redis_transaction_buffer: true (10s), ' +
          'LITELLM_RUST=1, and PgBouncer transaction pooling.';
      } else {
        badge.className = 'badge badge-green';
        badge.textContent = 'Healthy Scale Envelope';
        msg.style.background = 'var(--success-bg)';
        msg.style.color = 'var(--success)';
        msg.textContent =
          'LiteLLM Proxy is within healthy concurrency limits (' +
          prisma + ' Prisma engines, ' + rps +
          ' RPS). Keep use_redis_transaction_buffer: true enabled.';
      }
    }
  </script>
"""
