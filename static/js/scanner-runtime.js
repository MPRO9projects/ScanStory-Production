(function (root) {
  const STATES = [
    "idle", "loading_shell", "checking_capabilities", "requesting_camera", "initializing_camera",
    "loading_opencv", "loading_wasm", "initializing_scanner", "ready_to_scan", "detecting",
    "tracking", "target_lost", "recovering", "fallback", "paused", "failed"
  ];
  const TRANSITIONS = {
    idle: ["loading_shell"],
    loading_shell: ["checking_capabilities", "failed"],
    checking_capabilities: ["requesting_camera", "fallback", "failed"],
    requesting_camera: ["initializing_camera", "fallback", "failed"],
    initializing_camera: ["loading_opencv", "fallback", "failed"],
    loading_opencv: ["loading_wasm", "fallback", "failed"],
    loading_wasm: ["initializing_scanner", "fallback", "failed"],
    initializing_scanner: ["ready_to_scan", "fallback", "failed"],
    ready_to_scan: ["detecting", "paused", "fallback"],
    detecting: ["tracking", "ready_to_scan", "target_lost", "fallback", "paused", "failed"],
    tracking: ["target_lost", "detecting", "paused", "fallback"],
    target_lost: ["recovering", "fallback", "paused"],
    recovering: ["tracking", "detecting", "fallback", "failed"],
    fallback: ["requesting_camera", "paused"],
    paused: ["ready_to_scan", "requesting_camera", "fallback", "failed"],
    failed: ["fallback"]
  };
  const TIMEOUTS = {
    loading_shell: 5000, checking_capabilities: 3000, requesting_camera: 15000,
    initializing_camera: 10000, loading_opencv: 15000, loading_wasm: 15000,
    initializing_scanner: 8000, detecting: 8000, target_lost: 3000, recovering: 7000
  };
  const MODES = {
    full: { frameWidth: 960, detectIntervalMs: 250, trackingPoints: 260, requestTimeoutMs: 7000 },
    standard: { frameWidth: 720, detectIntervalMs: 350, trackingPoints: 180, requestTimeoutMs: 8000 },
    lightweight: { frameWidth: 480, detectIntervalMs: 650, trackingPoints: 90, requestTimeoutMs: 9000 },
    fallback: { frameWidth: 0, detectIntervalMs: 0, trackingPoints: 0, requestTimeoutMs: 0 }
  };
  const ERRORS = {
    CAMERA_PERMISSION_DENIED: "Camera access is needed to recognize the image. Allow camera access, then tap Try Again.",
    CAMERA_UNAVAILABLE: "Camera is unavailable on this device. You can still view the fallback video.",
    SECURE_CONTEXT_REQUIRED: "Camera scanning needs a secure browser connection.",
    OPENCV_LOAD_FAILED: "The vision engine did not load. Try again or use fallback.",
    WASM_LOAD_FAILED: "The scanner engine is not supported by this browser.",
    SCANNER_INIT_TIMEOUT: "Scanner setup took too long. Try again or use fallback.",
    DETECTION_TIMEOUT: "Recognition is taking too long. Move closer or use fallback.",
    INVALID_DETECTION_RESPONSE: "The scanner could not read this response safely.",
    TARGET_LOST_TIMEOUT: "The image marker was lost. Point the camera at the image again.",
    VIDEO_LOAD_FAILED: "The video could not play on this browser. Try fallback playback.",
    UNSUPPORTED_DEVICE: "This device cannot run live tracking. Use fallback playback."
  };

  function createStateMachine(onChange) {
    let state = "idle";
    let enteredAt = performance.now();
    let initCount = 0;
    return {
      get state() { return state; },
      transition(next) {
        if (!STATES.includes(next) || !(TRANSITIONS[state] || []).includes(next)) {
          throw new Error(`invalid transition ${state}->${next}`);
        }
        if (state === "idle" && next === "loading_shell") initCount += 1;
        if (next === "loading_shell" && initCount > 1) throw new Error("duplicate initialization blocked");
        state = next;
        enteredAt = performance.now();
        if (onChange) onChange(state);
        return state;
      },
      timedOut(now) {
        const timeout = TIMEOUTS[state];
        return Boolean(timeout && (now || performance.now()) - enteredAt >= timeout);
      }
    };
  }

  function detectCapabilities(nav, win) {
    nav = nav || navigator;
    win = win || window;
    const ua = nav.userAgent || "";
    return {
      secureContext: Boolean(win.isSecureContext),
      cameraApi: Boolean(nav.mediaDevices && nav.mediaDevices.getUserMedia),
      webassembly: typeof WebAssembly !== "undefined",
      canvas: (() => { try { return Boolean(document.createElement("canvas").getContext("2d")); } catch (_) { return false; } })(),
      webgl: (() => { try { const c = document.createElement("canvas"); return Boolean(c.getContext("webgl") || c.getContext("experimental-webgl")); } catch (_) { return false; } })(),
      deviceMemory: nav.deviceMemory || 4,
      hardwareConcurrency: nav.hardwareConcurrency || 2,
      screenWidth: (win.screen && win.screen.width) || win.innerWidth || 360,
      isIOS: /iPhone|iPad|iPod/i.test(ua),
      isAndroid: /Android/i.test(ua),
      reducedMotion: Boolean(win.matchMedia && win.matchMedia("(prefers-reduced-motion: reduce)").matches),
      pageVisible: !document.hidden
    };
  }

  function selectRuntimeMode(caps, override, priorFailure) {
    if (override && MODES[override]) return override;
    if (priorFailure || !caps.secureContext || !caps.cameraApi || !caps.webassembly || !caps.canvas) return "fallback";
    const memory = Number(caps.deviceMemory || 4);
    const cores = Number(caps.hardwareConcurrency || 2);
    const width = Number(caps.screenWidth || 360);
    if (memory <= 2 || cores <= 2 || width < 360 || caps.reducedMotion) return "lightweight";
    if (memory >= 6 && cores >= 6 && width >= 720 && caps.webgl) return "full";
    return "standard";
  }

  function createRequestPolicy(mode) {
    const cfg = MODES[mode] || MODES.standard;
    let inFlight = null;
    let latestSeq = 0;
    let lastStarted = -Infinity;
    return {
      canStart(now, pageVisible, cameraActive, tracking) {
        if (!pageVisible || !cameraActive || inFlight) return false;
        return now - lastStarted >= cfg.detectIntervalMs * (tracking ? 2 : 1);
      },
      start(now) {
        if (inFlight) throw new Error("recognition request already in flight");
        latestSeq += 1;
        inFlight = latestSeq;
        lastStarted = now;
        return inFlight;
      },
      finish(id) {
        if (id !== inFlight) return "stale";
        inFlight = null;
        return "accepted";
      },
      timedOut(now) {
        return Boolean(inFlight && now - lastStarted >= cfg.requestTimeoutMs);
      }
    };
  }

  function validateDetectionResponse(payload) {
    if (!payload || typeof payload !== "object") return { ok: false, code: "INVALID_DETECTION_RESPONSE" };
    if (!payload.detected) return { ok: true, code: "NO_MATCH" };
    if (!Array.isArray(payload.corners) || payload.corners.length !== 4) return { ok: false, code: "INVALID_DETECTION_RESPONSE" };
    for (const point of payload.corners) {
      if (!point || Number.isNaN(Number(point.x)) || Number.isNaN(Number(point.y))) return { ok: false, code: "INVALID_DETECTION_RESPONSE" };
    }
    if (!payload.video_url) return { ok: false, code: "PUBLISHED_MEDIA_MISSING" };
    return { ok: true, code: "MATCH" };
  }

  function createDiagnostics(enabled, sink) {
    const active = Boolean(enabled);
    const limit = 80;
    const events = [];
    return {
      push(name, data) {
        if (!active) return;
        const safe = Object.assign({ event: name, at: Math.round(performance.now()) }, data || {});
        delete safe.frame;
        delete safe.image;
        delete safe.blob;
        events.push(safe);
        if (events.length > limit) events.shift();
        if (sink) sink(safe);
      },
      snapshot() {
        return events.slice();
      },
      reset() {
        events.length = 0;
      }
    };
  }

  function quadArea(points) {
    let area = 0;
    for (let i = 0; i < points.length; i++) {
      const a = points[i];
      const b = points[(i + 1) % points.length];
      area += a.x * b.y - b.x * a.y;
    }
    return Math.abs(area / 2);
  }

  function isValidQuad(points, frameWidth, frameHeight) {
    if (!Array.isArray(points) || points.length !== 4 || frameWidth <= 0 || frameHeight <= 0) return false;
    const pad = 0.2;
    for (const point of points) {
      if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) return false;
      if (point.x < -frameWidth * pad || point.x > frameWidth * (1 + pad)) return false;
      if (point.y < -frameHeight * pad || point.y > frameHeight * (1 + pad)) return false;
    }
    const area = quadArea(points);
    const frameArea = frameWidth * frameHeight;
    if (area < frameArea * 0.01 || area > frameArea * 0.95) return false;
    const edges = points.map((point, index) => {
      const next = points[(index + 1) % points.length];
      return Math.hypot(next.x - point.x, next.y - point.y);
    });
    const minEdge = Math.min.apply(null, edges);
    const maxEdge = Math.max.apply(null, edges);
    return minEdge >= Math.min(frameWidth, frameHeight) * 0.03 && maxEdge / Math.max(minEdge, 1) <= 12;
  }

  root.ScanStoryScannerRuntime = {
    STATES, TRANSITIONS, TIMEOUTS, MODES, ERRORS,
    createStateMachine, detectCapabilities, selectRuntimeMode, createRequestPolicy,
    validateDetectionResponse, createDiagnostics, isValidQuad
  };
})(window);
