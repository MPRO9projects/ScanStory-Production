/**
 * Throttled-network certification for the resumable upload protocol.
 *
 * Drives the REAL installed Chrome over CDP (no Playwright, no npm
 * dependency - Node's global WebSocket is enough) against a real running
 * ScanStory instance, with the uplink genuinely throttled by
 * Network.emulateNetworkConditions. The in-page script runs the same
 * adaptive-chunk and retry policy the uploader ships: the policy block is
 * sliced out of templates/user/user_create_project.html between its two
 * marker comments and injected, so nothing is reimplemented here.
 *
 * SCOPE, stated plainly: this certifies the upload PROTOCOL and its client
 * policy under real throttled conditions. It does not click through the
 * create-project wizard's cropping UI, so it does not certify that DOM
 * flow. The synthesized payload is a plain byte buffer, so runs stop at
 * finalize for the pure-transfer profiles (finalize would reject bytes
 * that are not a decodable image/video, which is the server behaving
 * correctly and is covered by the pytest scenarios instead).
 *
 * Usage:
 *   node scripts/dev/low_bandwidth_upload_certification.mjs \
 *     --origin http://127.0.0.1:5099 --out evidence/low_bandwidth
 */
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, tok, i, all) => {
    if (tok.startsWith('--')) acc.push([tok.slice(2), all[i + 1]]);
    return acc;
  }, [])
);
const ORIGIN = args.origin || 'http://127.0.0.1:5099';
const OUT_DIR = args.out || 'evidence/low_bandwidth';
const EMAIL = args.email || 'lowbandwidth@example.com';
const PASSWORD = args.password || 'password123';
const PAYLOAD_BYTES = Number(args.bytes || 512 * 1024);
// A fixed port silently attaches to a LEFTOVER browser from a previous run
// (child.kill() on Windows kills the launcher, not the browser tree), which
// is how this harness first hung. Randomise, and kill the tree on the way out.
const DEBUG_PORT = Number(args.port || 9400 + Math.floor(Math.random() * 400));

const CHROME_CANDIDATES = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
];

// Mbps -> bytes/second, as CDP wants it.
const mbps = (m) => (m * 1000 * 1000) / 8;

/* Throughput profiles. 5/2/1/0.6/0.3/0.15 Mbps as required, each with a
   latency that is plausible for a link of that speed rather than an
   idealised 0 ms. */
const PROFILES = [
  { name: '5 Mbps / 100 ms', up: mbps(5), down: mbps(10), latency: 100 },
  { name: '2 Mbps / 100 ms', up: mbps(2), down: mbps(4), latency: 100 },
  { name: '1 Mbps / 300 ms', up: mbps(1), down: mbps(2), latency: 300 },
  { name: '0.6 Mbps / 300 ms', up: mbps(0.6), down: mbps(1.2), latency: 300 },
  { name: '0.3 Mbps / 700 ms', up: mbps(0.3), down: mbps(0.6), latency: 700 },
  { name: '0.15 Mbps / 700 ms', up: mbps(0.15), down: mbps(0.3), latency: 700 },
];

// ------------------------------------------------------------------ CDP
let ws;
let nextId = 1;
const pending = new Map();
const sessions = {};

function send(method, params = {}, sessionId, timeoutMs = 300000) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    // A CDP command that never answers is the one failure mode that turns
    // this harness into a hang instead of a result. Always bound it.
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`CDP timeout after ${timeoutMs} ms: ${method}`));
    }, timeoutMs);
    pending.set(id, {
      resolve: (v) => { clearTimeout(timer); resolve(v); },
      reject: (e) => { clearTimeout(timer); reject(e); },
    });
    ws.send(JSON.stringify(sessionId ? { id, method, params, sessionId } : { id, method, params }));
  });
}

function log(line) {
  // Newline-terminated and flushed per line: a stalled run must still show
  // exactly how far it got.
  process.stderr.write(line + '\n');
}

async function connect(url) {
  ws = new WebSocket(url);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', reject, { once: true });
  });
  // A throw inside this listener silently swallows the reply and the caller
  // waits forever, which is exactly how this harness first appeared to hang.
  ws.addEventListener('message', (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (err) {
      log(`  !! undecodable CDP frame (${String(err.message).slice(0, 120)})`);
      return;
    }
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(`${msg.error.message} (${JSON.stringify(msg.error.data || '')})`));
      else resolve(msg.result);
    }
  });
  ws.addEventListener('close', (event) => {
    log(`  !! CDP socket closed (code=${event.code} reason=${event.reason || 'none'})`);
    for (const [id, { reject }] of pending) {
      pending.delete(id);
      reject(new Error('CDP connection closed while a command was in flight'));
    }
  });
}

/* Never hold a CDP command open for the length of a throttled upload.
   Chrome closes the DevTools socket out from under a long-awaited
   Runtime.evaluate (observed reliably once a run exceeded a few seconds),
   which stalls the whole harness. So: kick the run off without awaiting it,
   park the outcome on a page global, and poll for it with short commands.
   The polling doubles as WebSocket keepalive. */
async function evaluateBig(expression, sessionId, pollMs = 2000, maxWaitMs = 900000) {
  await evaluate(
    `(() => {
       window.__certResult = undefined;
       window.__certError = undefined;
       (${expression}).then(r => { window.__certResult = r; },
                            e => { window.__certError = String(e && e.stack || e); });
       return 'started';
     })()`,
    sessionId, 30000,
  );
  const deadline = Date.now() + maxWaitMs;
  while (Date.now() < deadline) {
    await sleep(pollMs);
    const probe = await evaluate(
      `window.__certError ? JSON.stringify({ __certError: window.__certError })
        : (window.__certResult === undefined ? null : JSON.stringify(window.__certResult))`,
      sessionId, 30000,
    );
    if (!probe) continue;
    const parsed = JSON.parse(probe);
    if (parsed && parsed.__certError) throw new Error(String(parsed.__certError).slice(0, 300));
    return parsed;
  }
  throw new Error(`in-page run did not finish within ${maxWaitMs} ms`);
}

async function evaluate(expression, sessionId, timeoutMs = 900000) {
  const result = await send('Runtime.evaluate', {
    expression, awaitPromise: true, returnByValue: true, timeout: timeoutMs,
  }, sessionId);
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description || JSON.stringify(result.exceptionDetails));
  }
  return result.result.value;
}

async function navigate(url, sessionId) {
  await send('Page.navigate', { url }, sessionId);
  // Poll readiness rather than racing lifecycle events: throttling makes
  // event ordering unreliable and this is simpler than getting it wrong.
  for (let i = 0; i < 200; i++) {
    try {
      const state = await evaluate('document.readyState', sessionId, 5000);
      if (state === 'complete') return;
    } catch (_err) { /* mid-navigation */ }
    await sleep(150);
  }
  throw new Error(`navigation to ${url} did not settle`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function throttle(profile, sessionId) {
  await send('Network.emulateNetworkConditions', {
    offline: !!profile.offline,
    latency: profile.latency || 0,
    downloadThroughput: profile.down ?? -1,
    uploadThroughput: profile.up ?? -1,
  }, sessionId);
}

// -------------------------------------------------- injected upload run
function buildRunner(policyJs) {
  // The runner deliberately uses the real policy helpers for chunk sizing
  // and retry classification. The loop around them mirrors
  // uploadResumableStream()'s decisions minus its DOM reporting.
  return `(async () => {
    const navigator_ = navigator;
    ${policyJs}
    // The page already has the token; ask it rather than re-deriving it.
    const CSRF = (typeof csrfHeader === 'function' && csrfHeader())
      || document.querySelector('input[name=csrf_token]')?.value
      || (document.documentElement.outerHTML.match(/csrf_token[^>]*value="([^"]+)"/) || [])[1];
    if (!CSRF) throw new Error('could not obtain a CSRF token from the uploader page');
    const stats = { chunkSizes: [], retries: 0, resyncs: 0, shrinks: 0, pauses: 0,
                    bytesSent: 0, bytesRetransmitted: 0, requests: 0, waitedMs: 0 };

    async function api(url, options) {
      const res = await fetch(url, {
        credentials: 'same-origin', cache: 'no-store', ...options,
        headers: { ...(options.headers || {}), 'X-CSRFToken': CSRF },
      });
      let payload = null;
      let raw = '';
      try { raw = await res.text(); payload = JSON.parse(raw); } catch (_e) {}
      if (!res.ok || payload?.success === false) {
        const err = new Error((payload?.code || res.status) + ' :: ' + raw.slice(0, 300));
        err.code = payload?.code; err.status = res.status; err.payload = payload;
        err.retryAfterMs = parseRetryAfterMs(res.headers.get('Retry-After'));
        throw err;
      }
      return payload || {};
    }

    const total = __TOTAL__;
    // A resume run reuses the session the pre-refresh run created, and
    // learns its offset from the SERVER, never from anything the page kept.
    const resumeId = __RESUME__;
    const session = resumeId
      ? (await api('/api/uploads/sessions/' + resumeId, { method: 'GET' })).session
      : (await api('/api/uploads/sessions', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            image_size: 0, video_size: total, project_name: 'Low bandwidth certification',
            original_video_name: 'certification.mp4', video_content_type: 'video/mp4',
            experience_type: 'direct_qr', playback_mode: 'direct',
          }),
        })).session;
    // Survives a reload, so a post-refresh run can find the session again.
    try { localStorage.setItem('__cert_session', String(session.id)); } catch (_e) {}
    const resumedFrom = resumeId ? session.current_offset : null;
    const serverMax = session.max_chunk_bytes;
    // One deterministic buffer, sliced like a real File would be.
    const buffer = new Uint8Array(total);
    for (let i = 0; i < total; i++) buffer[i] = i % 251;

    let offset = session.current_offset || 0;
    let chunkBytes = initialChunkBytes(serverMax);
    let smoothed = null;
    let attempt = 0;
    const startedAt = performance.now();

    while (offset < total) {
      const end = Math.min(offset + chunkBytes, total);
      const body = buffer.slice(offset, end);
      const t0 = performance.now();
      try {
        stats.requests++;
        const res = await api('/api/uploads/sessions/' + session.id + '/chunk', {
          method: 'POST',
          headers: { 'Content-Type': 'application/octet-stream', 'X-Chunk-Offset': String(offset) },
          body,
        });
        const duplicate = res.note === 'duplicate_chunk_ignored';
        stats.bytesSent += body.length;
        if (duplicate) stats.bytesRetransmitted += body.length;
        const advanced = res.current_offset - offset;
        offset = res.current_offset;
        attempt = 0;
        if (!duplicate && advanced > 0) {
          const seconds = Math.max((performance.now() - t0) / 1000, 0.001);
          const sample = body.length / seconds;
          smoothed = smoothed === null
            ? sample
            : (smoothed * (1 - RESUMABLE_THROUGHPUT_SMOOTHING)) + (sample * RESUMABLE_THROUGHPUT_SMOOTHING);
          const resized = nextChunkBytes(chunkBytes, smoothed, serverMax);
          if (resized !== chunkBytes) { stats.chunkSizes.push(resized); chunkBytes = resized; }
        }
      } catch (err) {
        const decision = uploadRetryDecision(err, attempt);
        if (decision.action === 'stop') {
          return { ok: false, stoppedWith: err.code || String(err.status), offset, total, stats };
        }
        if (decision.action === 'pause') {
          stats.pauses++;
          return { ok: false, paused: true, offset, total, stats,
                   serverOffset: (await api('/api/uploads/sessions/' + session.id, { method: 'GET' })).session.current_offset };
        }
        attempt++;
        if (decision.action === 'shrink') {
          stats.shrinks++;
          chunkBytes = roundChunkBytes(Math.max(RESUMABLE_CHUNK_MIN_BYTES, chunkBytes / 2), serverMax);
          continue;
        }
        if (decision.action === 'resync') {
          stats.resyncs++;
          offset = Number.isFinite(err.payload?.current_offset)
            ? err.payload.current_offset
            : (await api('/api/uploads/sessions/' + session.id, { method: 'GET' })).session.current_offset;
          continue;
        }
        stats.retries++;
        if (attempt >= RESUMABLE_SHRINK_AFTER_ATTEMPTS) {
          chunkBytes = roundChunkBytes(Math.max(RESUMABLE_CHUNK_MIN_BYTES, chunkBytes / 2), serverMax);
        }
        stats.waitedMs += decision.waitMs;
        if (decision.waitMs > 0) await new Promise(r => setTimeout(r, decision.waitMs));
      }
    }

    const durationMs = Math.round(performance.now() - startedAt);
    const finalStatus = await api('/api/uploads/sessions/' + session.id, { method: 'GET' });
    return {
      ok: true, sessionId: session.id, offset, total, durationMs, stats, resumedFrom,
      serverOffset: finalStatus.session.current_offset,
      serverStatus: finalStatus.session.status,
      measuredBytesPerSecond: Math.round(smoothed || 0),
      finalChunkBytes: chunkBytes,
      networkQuality: networkQualityLabel(smoothed),
    };
  })()`;
}

// --------------------------------------------------------------- driver
function policyBlock() {
  const templatePath = path.join(process.cwd(), 'templates', 'user', 'user_create_project.html');
  const source = readFileSync(templatePath, 'utf8');
  const start = source.indexOf('/* ============ Extreme-low-bandwidth resumable upload ============');
  const end = source.indexOf('/* ==== end of pure low-bandwidth policy helpers ====');
  if (start < 0 || end < 0) throw new Error('policy block markers not found in the uploader template');
  // `let activeResumableUpload` etc. are harmless here; the runner never
  // touches them.
  return source.slice(start, end);
}

async function main() {
  const chrome = CHROME_CANDIDATES.find(existsSync);
  if (!chrome) throw new Error('no Chrome/Edge binary found');
  const profileDir = mkdtempSync(path.join(tmpdir(), 'scanstory-cdp-'));
  const child = spawn(chrome, [
    '--headless=new', `--remote-debugging-port=${DEBUG_PORT}`, `--user-data-dir=${profileDir}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu', '--disable-extensions',
    'about:blank',
  ], { stdio: 'ignore' });

  let versionInfo = null;
  for (let i = 0; i < 60 && !versionInfo; i++) {
    try {
      versionInfo = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/version`)).json();
    } catch (_e) { await sleep(500); }
  }
  if (!versionInfo) { child.kill(); throw new Error('Chrome did not expose a CDP endpoint'); }

  await connect(versionInfo.webSocketDebuggerUrl);
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  await send('Page.enable', {}, sessionId);
  await send('Runtime.enable', {}, sessionId);
  await send('Network.enable', {}, sessionId);

  // Log in unthrottled - certifying the login page is not the job here.
  await navigate(`${ORIGIN}/login`, sessionId);
  await evaluate(`(() => {
    document.querySelector('input[name=email]').value = ${JSON.stringify(EMAIL)};
    document.querySelector('input[name=password]').value = ${JSON.stringify(PASSWORD)};
    document.querySelector('input[name=email]').form.submit();
    return true;
  })()`, sessionId);
  await sleep(2500);
  await navigate(`${ORIGIN}/create-project`, sessionId);
  const loggedIn = await evaluate(`!!document.querySelector('#projectForm')`, sessionId);
  if (!loggedIn) throw new Error('login/create-project did not render the uploader (check credentials)');

  const runnerTemplate = buildRunner(policyBlock());
  const runner = runnerTemplate.replace('__RESUME__', 'null');
  const resumeRunner = (id) => runnerTemplate.replace('__RESUME__', String(id));
  const results = { browser: versionInfo.Browser, origin: ORIGIN, payloadBytes: PAYLOAD_BYTES, throughput: [], interruptions: [] };

  const profiles = args.only ? PROFILES.slice(0, Number(args.only)) : PROFILES;
  for (const profile of profiles) {
    await throttle(profile, sessionId);
    process.stderr.write(`\n[throughput] ${profile.name} ... `);
    const started = Date.now();
    let outcome;
    try {
      outcome = await evaluateBig(runner.replace('__TOTAL__', String(PAYLOAD_BYTES)), sessionId);
    } catch (err) {
      outcome = { ok: false, error: String(err.message).slice(0, 300) };
    }
    outcome.profile = profile.name;
    outcome.wallMs = Date.now() - started;
    outcome.effectiveMbps = outcome.ok
      ? Number(((PAYLOAD_BYTES * 8) / (outcome.durationMs / 1000) / 1e6).toFixed(3))
      : null;
    results.throughput.push(outcome);
    log(outcome.ok ? `  ok in ${outcome.durationMs} ms (${outcome.effectiveMbps} Mbps effective, ${outcome.stats.requests} requests, final chunk ${outcome.finalChunkBytes} B)` : `  FAILED (${outcome.error || outcome.stoppedWith})`);
  }

  /* Interruption cases. The uplink is cut mid-transfer for a fixed window
     and then restored, which is the failure the whole design exists for. */
  const interruptions = args.skipInterruptions ? [] : [
    { name: '10 s disconnect mid-upload', offlineMs: 10000, base: PROFILES[3] },
    { name: '30 s disconnect mid-upload', offlineMs: 30000, base: PROFILES[3] },
  ];
  for (const scenario of interruptions) {
    await throttle(scenario.base, sessionId);
    process.stderr.write(`\n[interruption] ${scenario.name} ... `);
    const started = Date.now();
    const runPromise = evaluateBig(runner.replace('__TOTAL__', String(PAYLOAD_BYTES)), sessionId)
      .catch((err) => ({ ok: false, error: String(err.message).slice(0, 300) }));
    // Cut the link once the transfer is genuinely under way.
    await sleep(3000);
    await throttle({ offline: true, latency: 0, up: 0, down: 0 }, sessionId);
    await sleep(scenario.offlineMs);
    await throttle(scenario.base, sessionId);
    const outcome = await runPromise;
    outcome.profile = `${scenario.base.name} + ${scenario.name}`;
    outcome.wallMs = Date.now() - started;
    results.interruptions.push(outcome);
    log(outcome.ok
      ? `  recovered in ${outcome.wallMs} ms (retries=${outcome.stats.retries}, resyncs=${outcome.stats.resyncs}, retransmitted=${outcome.stats.bytesRetransmitted} B)`
      : `  ${outcome.paused ? 'paused, session intact at ' + outcome.serverOffset + ' B' : 'FAILED ' + (outcome.error || outcome.stoppedWith)}`);
  }

  /* Refresh mid-upload. The reload throws the page's own state away, so
     the only way the second half can continue is by asking the server
     where it got to - which is the whole point of the contract. */
  if (!args.skipInterruptions) {
    const base = PROFILES[3];
    await throttle(base, sessionId);
    process.stderr.write('\n[interruption] refresh mid-upload ... ');
    const started = Date.now();
    // Short deadline on purpose: this run is MEANT to be destroyed by the
    // reload, after which its page global is gone and polling for it would
    // otherwise just burn the full timeout.
    const abandoned = evaluateBig(runner.replace('__TOTAL__', String(PAYLOAD_BYTES)), sessionId, 1000, 20000)
      .catch(() => null);
    await sleep(6000);
    await navigate(`${ORIGIN}/create-project`, sessionId);   // hard reload mid-transfer
    await abandoned;
    const recoveredId = await evaluate(`localStorage.getItem('__cert_session')`, sessionId);
    let outcome;
    if (!recoveredId) {
      outcome = { ok: false, error: 'no session id survived the reload' };
    } else {
      const before = await evaluate(
        `fetch('/api/uploads/sessions/${recoveredId}', { credentials: 'same-origin', cache: 'no-store' })
           .then(r => r.json()).then(p => p.session.current_offset)`, sessionId);
      outcome = await evaluateBig(resumeRunner(recoveredId).replace('__TOTAL__', String(PAYLOAD_BYTES)), sessionId)
        .catch((err) => ({ ok: false, error: String(err.message).slice(0, 300) }));
      outcome.offsetSurvivingRefresh = before;
    }
    outcome.profile = `${base.name} + refresh mid-upload`;
    outcome.wallMs = Date.now() - started;
    results.interruptions.push(outcome);
    log(outcome.ok
      ? `  resumed from ${outcome.offsetSurvivingRefresh} B, finished ${outcome.offset}/${outcome.total} B, retransmitted ${outcome.stats.bytesRetransmitted} B`
      : `  FAILED ${outcome.error}`);
  }

  mkdirSync(OUT_DIR, { recursive: true });
  const outPath = path.join(OUT_DIR, 'throttled_upload_certification.json');
  writeFileSync(outPath, JSON.stringify(results, null, 2), 'utf8');
  process.stderr.write(`\n\nwrote ${outPath}\n`);
  console.log(JSON.stringify(results, null, 2));

  ws.close();
  killTree(child);
}

function killTree(child) {
  try { child.kill(); } catch (_e) {}
  if (process.platform === 'win32' && child.pid) {
    // /T so the browser's own child processes go too, otherwise the next
    // run finds a live CDP endpoint that is not the browser it launched.
    spawn('taskkill', ['/F', '/T', '/PID', String(child.pid)], { stdio: 'ignore' });
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
