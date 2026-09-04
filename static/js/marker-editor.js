/* Shared marker preparation engine (SCANSTORY V1.1 master stabilization pass).
 *
 * This is the SAME crop/rotate/reset/full-image/export logic the Creator wizard
 * has always used (extracted from templates/user/user_create_project.html,
 * unchanged math) - now the single source of truth so target REPLACEMENT
 * (templates/user/edit_project.html) can use it too, instead of a second,
 * diverging implementation.
 *
 * Two layers:
 *   - Pure functions (geometry/draw/export/quality) - no DOM state, no globals.
 *     Both consumers call these directly for math that must be identical.
 *   - createController(...) - the actual pointer/touch/keyboard interaction
 *     wiring, factored out once so both consumers get full interaction parity
 *     (drag-resize handles, move, keyboard nudge, pointer capture) without
 *     re-implementing it. Callers own their own state storage via
 *     getState()/setState() - the controller never assumes a global.
 *
 * Runs ONLY while an editor is open (creation or replacement) - never touches
 * the scanner runtime, never runs per-frame/per-scan. See marker-editor
 * performance notes in the master stabilization handoff.
 */
(function (global) {
  'use strict';

  var DEFAULT_CROP = Object.freeze({ x: 0.1, y: 0.1, width: 0.8, height: 0.8 });
  var MIN_CROP_FRACTION = 0.08;
  var NUDGE_STEP = 0.01;

  function defaultCrop() {
    return { x: DEFAULT_CROP.x, y: DEFAULT_CROP.y, width: DEFAULT_CROP.width, height: DEFAULT_CROP.height };
  }

  function finiteNumber(value) {
    var n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  /* Pure: never mutates the crop passed in. markerMode 'full_image' always
   * normalizes to the full frame; 'crop' clamps to the image bounds and a
   * minimum usable size. Falls back to defaultCrop() for anything malformed -
   * the same recovery a bad/stale saved state gets in the wizard today. */
  function sanitizeCrop(crop, markerMode, naturalWidth, naturalHeight) {
    naturalWidth = Number(naturalWidth || 0);
    naturalHeight = Number(naturalHeight || 0);
    if (!naturalWidth || !naturalHeight) return defaultCrop();
    if (markerMode === 'full_image') return { x: 0, y: 0, width: 1, height: 1 };

    var source = crop || defaultCrop();
    var x = finiteNumber(source.x);
    var y = finiteNumber(source.y);
    var width = finiteNumber(source.width);
    var height = finiteNumber(source.height);
    var minWidth = Math.min(0.5, Math.max(MIN_CROP_FRACTION, 1 / naturalWidth));
    var minHeight = Math.min(0.5, Math.max(MIN_CROP_FRACTION, 1 / naturalHeight));

    if (x === null || y === null || width === null || height === null || width < minWidth || height < minHeight) {
      return defaultCrop();
    }
    width = Math.min(1, Math.max(minWidth, width));
    height = Math.min(1, Math.max(minHeight, height));
    x = Math.min(Math.max(0, x), 1 - width);
    y = Math.min(Math.max(0, y), 1 - height);
    return { x: x, y: y, width: width, height: height };
  }

  /* Where the (possibly rotated) image and the crop rectangle land in CANVAS
   * pixel space, letterboxed to fit. Returns null when the canvas/image have
   * no usable size yet (still loading, container hidden, etc). */
  function computeDrawRect(crop, naturalWidth, naturalHeight, canvasWidth, canvasHeight) {
    if (!naturalWidth || !naturalHeight || !canvasWidth || !canvasHeight) return null;
    var scale = Math.min(canvasWidth / naturalWidth, canvasHeight / naturalHeight);
    if (!Number.isFinite(scale) || scale <= 0) return null;
    var drawW = naturalWidth * scale;
    var drawH = naturalHeight * scale;
    var offsetX = (canvasWidth - drawW) / 2;
    var offsetY = (canvasHeight - drawH) / 2;
    return {
      imageX: offsetX, imageY: offsetY, imageW: drawW, imageH: drawH,
      x: offsetX + crop.x * drawW, y: offsetY + crop.y * drawH,
      w: crop.width * drawW, h: crop.height * drawH
    };
  }

  function cropHandles(r) {
    return [
      { name: 'nw', x: r.x, y: r.y }, { name: 'n', x: r.x + r.w / 2, y: r.y },
      { name: 'ne', x: r.x + r.w, y: r.y }, { name: 'e', x: r.x + r.w, y: r.y + r.h / 2 },
      { name: 'se', x: r.x + r.w, y: r.y + r.h }, { name: 's', x: r.x + r.w / 2, y: r.y + r.h },
      { name: 'sw', x: r.x, y: r.y + r.h }, { name: 'w', x: r.x, y: r.y + r.h / 2 }
    ];
  }

  function hitTestDragMode(point, rect, tolerance) {
    var handle = cropHandles(rect).find(function (h) {
      return Math.abs(point.x - h.x) <= tolerance && Math.abs(point.y - h.y) <= tolerance;
    });
    if (handle) return handle.name;
    var inside = point.x >= rect.x && point.x <= rect.x + rect.w && point.y >= rect.y && point.y <= rect.y + rect.h;
    return inside ? 'move' : null;
  }

  /* Pure drag-math: given the crop at drag-start, the drag mode (a resize
   * handle name or 'move'), and the pointer delta in NORMALIZED (0-1) image
   * space, returns the new sanitized crop. Identical for mouse, touch, and
   * keyboard-nudge callers - only how dx/dy get computed differs upstream. */
  function applyDragDelta(startCrop, mode, dx, dy, naturalWidth, naturalHeight) {
    var minSize = MIN_CROP_FRACTION;
    var x1 = startCrop.x, y1 = startCrop.y;
    var x2 = startCrop.x + startCrop.width, y2 = startCrop.y + startCrop.height;
    if (mode === 'move') {
      var width = Math.max(minSize, startCrop.width);
      var height = Math.max(minSize, startCrop.height);
      x1 = Math.max(0, Math.min(startCrop.x + dx, 1 - width));
      y1 = Math.max(0, Math.min(startCrop.y + dy, 1 - height));
      x2 = x1 + width; y2 = y1 + height;
    } else {
      if (mode.indexOf('w') !== -1) x1 += dx;
      if (mode.indexOf('e') !== -1) x2 += dx;
      if (mode.indexOf('n') !== -1) y1 += dy;
      if (mode.indexOf('s') !== -1) y2 += dy;
      if (mode.indexOf('w') !== -1 && x2 - x1 < minSize) x1 = x2 - minSize;
      if (mode.indexOf('e') !== -1 && x2 - x1 < minSize) x2 = x1 + minSize;
      if (mode.indexOf('n') !== -1 && y2 - y1 < minSize) y1 = y2 - minSize;
      if (mode.indexOf('s') !== -1 && y2 - y1 < minSize) y2 = y1 + minSize;
      if (x1 < 0) { x2 -= x1; x1 = 0; }
      if (y1 < 0) { y2 -= y1; y1 = 0; }
      if (x2 > 1) { x1 -= x2 - 1; x2 = 1; }
      if (y2 > 1) { y1 -= y2 - 1; y2 = 1; }
    }
    x1 = Math.max(0, Math.min(x1, 1 - minSize));
    y1 = Math.max(0, Math.min(y1, 1 - minSize));
    x2 = Math.max(x1 + minSize, Math.min(x2, 1));
    y2 = Math.max(y1 + minSize, Math.min(y2, 1));
    return sanitizeCrop({ x: x1, y: y1, width: x2 - x1, height: y2 - y1 }, 'crop', naturalWidth, naturalHeight);
  }

  function drawCanvas(ctx, canvas, image, crop, rotation) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    var r = computeDrawRect(crop, image.naturalWidth, image.naturalHeight, canvas.width, canvas.height);
    if (!r) return null;
    ctx.save();
    ctx.translate(r.imageX + r.imageW / 2, r.imageY + r.imageH / 2);
    ctx.rotate((rotation * Math.PI) / 180);
    ctx.drawImage(image, -r.imageW / 2, -r.imageH / 2, r.imageW, r.imageH);
    ctx.restore();
    ctx.fillStyle = 'rgba(0,0,0,.58)';
    ctx.fillRect(r.imageX, r.imageY, r.imageW, r.imageH);
    ctx.clearRect(r.x, r.y, r.w, r.h);
    ctx.save();
    ctx.beginPath();
    ctx.rect(r.x, r.y, r.w, r.h);
    ctx.clip();
    ctx.translate(r.imageX + r.imageW / 2, r.imageY + r.imageH / 2);
    ctx.rotate((rotation * Math.PI) / 180);
    ctx.drawImage(image, -r.imageW / 2, -r.imageH / 2, r.imageW, r.imageH);
    ctx.restore();
    ctx.strokeStyle = '#00d4ff';
    ctx.lineWidth = 3;
    ctx.strokeRect(r.x, r.y, r.w, r.h);
    ctx.fillStyle = '#fff';
    cropHandles(r).forEach(function (h) { ctx.fillRect(h.x - 6, h.y - 6, 12, 12); });
    return r;
  }

  /* The final marker export: source-crop the ORIGINAL image at full
   * resolution (never the letterboxed on-screen canvas), rotate, downscale
   * to longEdge, and draw onto the given output canvas. Same function backs
   * both the small live preview and the final upload - only longEdge/quality
   * differ at the call site. */
  function drawCroppedToOutputCanvas(canvas, image, crop, rotation, markerMode, longEdge) {
    var effectiveCrop = markerMode === 'full_image' ? { x: 0, y: 0, width: 1, height: 1 } : crop;
    var sx = Math.round(effectiveCrop.x * image.naturalWidth);
    var sy = Math.round(effectiveCrop.y * image.naturalHeight);
    var sw = Math.max(1, Math.round(effectiveCrop.width * image.naturalWidth));
    var sh = Math.max(1, Math.round(effectiveCrop.height * image.naturalHeight));
    var scale = Math.min(1, longEdge / Math.max(sw, sh));
    var outW = Math.max(1, Math.round(sw * scale));
    var outH = Math.max(1, Math.round(sh * scale));
    var rotated = ((rotation || 0) % 180) !== 0;
    canvas.width = rotated ? outH : outW;
    canvas.height = rotated ? outW : outH;
    var ctx = canvas.getContext('2d');
    ctx.save();
    ctx.translate(canvas.width / 2, canvas.height / 2);
    ctx.rotate(((rotation || 0) * Math.PI) / 180);
    ctx.drawImage(image, sx, sy, sw, sh, -outW / 2, -outH / 2, outW, outH);
    ctx.restore();
    return { width: canvas.width, height: canvas.height };
  }

  async function exportDataUrl(image, crop, rotation, markerMode, longEdge, quality) {
    var canvas = document.createElement('canvas');
    drawCroppedToOutputCanvas(canvas, image, crop, rotation, markerMode, longEdge);
    return canvas.toDataURL('image/jpeg', quality);
  }

  async function exportBlob(image, crop, rotation, markerMode, longEdge, quality, filename) {
    var canvas = document.createElement('canvas');
    var size = drawCroppedToOutputCanvas(canvas, image, crop, rotation, markerMode, longEdge);
    var blob = await new Promise(function (resolve) { canvas.toBlob(resolve, 'image/jpeg', quality); });
    return {
      file: new File([blob], filename || 'marker.jpg', { type: 'image/jpeg' }),
      width: size.width, height: size.height
    };
  }

  function evaluateQuality(crop, rotation, markerMode, originalWidth, originalHeight) {
    var effectiveCrop = markerMode === 'full_image' ? { width: 1, height: 1 } : crop;
    var markerW = Math.round((originalWidth || 0) * effectiveCrop.width);
    var markerH = Math.round((originalHeight || 0) * effectiveCrop.height);
    var guidance = [];
    var score = 2;
    if (markerW < 360 || markerH < 360) { score--; guidance.push('Marker is too small.'); }
    if (effectiveCrop.width * effectiveCrop.height > 0.82 && markerMode === 'crop') guidance.push('Crop closer to the intended marker.');
    var aspect = Math.max(markerW, markerH) / Math.max(Math.min(markerW, markerH), 1);
    if (aspect > 4) { score--; guidance.push('Large blank or narrow areas may cause unstable tracking.'); }
    if (!guidance.length) guidance.push(markerMode === 'full_image' ? 'The scanner will use the whole image, including its background.' : 'Marker has enough size for local testing.');
    return { status: score >= 2 ? 'Good marker' : (score >= 1 ? 'Usable marker' : 'Weak marker'), guidance: guidance };
  }

  /* Interaction controller: wires pointer/touch/keyboard events on `canvas`
   * once, calling back into the CALLER'S own state storage. Never assumes a
   * global "current pair" - opts.getState()/setState() are the only contract,
   * so the wizard's per-pair currentFiles map and the replacement flow's
   * single local state object both work unmodified.
   *
   * opts:
   *   canvas       - the crop <canvas> element
   *   getState()   -> { crop, rotation, markerMode, image }  (image = HTMLImageElement)
   *   setState(next partial state) - merge into the caller's state
   *   onChange()   - called after any crop/rotation change (redraw/preview hook)
   *   isActive()   - optional; return false to ignore events (e.g. modal closed)
   *
   * Returns { destroy() } to remove all listeners - call when the editor
   * instance is torn down (e.g. replacement modal closed) so a page that
   * opens/closes the editor repeatedly never accumulates listeners.
   */
  function createController(opts) {
    var canvas = opts.canvas;
    var drag = null;
    var isActive = opts.isActive || function () { return true; };

    function pointerToCanvas(event) {
      var rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return null;
      if (!Number.isFinite(event.clientX) || !Number.isFinite(event.clientY)) return null;
      var scaleX = canvas.width / rect.width;
      var scaleY = canvas.height / rect.height;
      return { x: (event.clientX - rect.left) * scaleX, y: (event.clientY - rect.top) * scaleY };
    }

    function hitTolerance() {
      var rect = canvas.getBoundingClientRect();
      if (!rect.width) return 24;
      return 24 * (canvas.width / rect.width);
    }

    function currentDrawRect(state) {
      if (!state.image) return null;
      return computeDrawRect(state.crop, state.image.naturalWidth, state.image.naturalHeight, canvas.width, canvas.height);
    }

    function onPointerDown(event) {
      if (!isActive()) return;
      event.preventDefault();
      var point = pointerToCanvas(event);
      if (!point) return;
      var state = opts.getState();
      var r = currentDrawRect(state);
      if (!r) return;
      var mode = hitTestDragMode(point, r, hitTolerance());
      if (!mode) return;
      drag = { pointerId: event.pointerId, point: point, mode: mode, start: state.crop };
      if (event.currentTarget.setPointerCapture) {
        try { event.currentTarget.setPointerCapture(event.pointerId); } catch (_e) { /* best-effort */ }
      }
    }

    function onPointerMove(event) {
      if (!drag || drag.pointerId !== event.pointerId) return;
      event.preventDefault();
      var point = pointerToCanvas(event);
      if (!point) return;
      var state = opts.getState();
      var r = currentDrawRect(state);
      if (!r || !r.imageW || !r.imageH) return;
      var dx = (point.x - drag.point.x) / r.imageW;
      var dy = (point.y - drag.point.y) / r.imageH;
      var nextCrop = applyDragDelta(drag.start, drag.mode, dx, dy, state.image.naturalWidth, state.image.naturalHeight);
      opts.setState({ crop: nextCrop, markerMode: 'crop' });
      opts.onChange();
    }

    function onPointerUp(event) {
      if (!drag || drag.pointerId !== event.pointerId) return;
      if (canvas.releasePointerCapture) {
        try { canvas.releasePointerCapture(event.pointerId); } catch (_e) { /* best-effort */ }
      }
      drag = null;
    }

    function onKeydown(event) {
      if (!isActive() || document.activeElement !== canvas) return;
      var deltas = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
      var delta = deltas[event.key];
      if (!delta) return;
      var state = opts.getState();
      if (state.markerMode !== 'crop') return;
      event.preventDefault();
      var step = NUDGE_STEP * (event.altKey ? 5 : 1);
      var next = { x: state.crop.x, y: state.crop.y, width: state.crop.width, height: state.crop.height };
      if (event.shiftKey) {
        next.width = Math.min(1, Math.max(MIN_CROP_FRACTION, next.width + delta[0] * step));
        next.height = Math.min(1, Math.max(MIN_CROP_FRACTION, next.height + delta[1] * step));
      } else {
        next.x += delta[0] * step;
        next.y += delta[1] * step;
      }
      var sanitized = sanitizeCrop(next, 'crop', state.image.naturalWidth, state.image.naturalHeight);
      opts.setState({ crop: sanitized });
      opts.onChange();
    }

    canvas.addEventListener('pointerdown', onPointerDown);
    canvas.addEventListener('pointermove', onPointerMove);
    canvas.addEventListener('pointerup', onPointerUp);
    canvas.addEventListener('pointercancel', onPointerUp);
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    window.addEventListener('pointercancel', onPointerUp);
    canvas.addEventListener('keydown', onKeydown);
    var wheelHandler = function (event) { event.preventDefault(); };
    canvas.addEventListener('wheel', wheelHandler, { passive: false });

    return {
      destroy: function () {
        canvas.removeEventListener('pointerdown', onPointerDown);
        canvas.removeEventListener('pointermove', onPointerMove);
        canvas.removeEventListener('pointerup', onPointerUp);
        canvas.removeEventListener('pointercancel', onPointerUp);
        window.removeEventListener('pointermove', onPointerMove);
        window.removeEventListener('pointerup', onPointerUp);
        window.removeEventListener('pointercancel', onPointerUp);
        canvas.removeEventListener('keydown', onKeydown);
        canvas.removeEventListener('wheel', wheelHandler);
        drag = null;
      }
    };
  }

  /* Camera capture: shared between creation and replacement so "Take a
   * photo" behaves identically everywhere (section 2). Rear/environment
   * camera preferred; caller owns the <video> element and modal chrome,
   * this only owns the MediaStream lifecycle and the capture-to-blob step -
   * the two things that must never diverge (a stream leaked in one page but
   * not the other is exactly the kind of drift this module exists to
   * prevent). Always stop the returned stream's tracks when done (capture,
   * cancel, or the host modal closing) - see stopCameraStream(). */
  function cameraSupported() {
    return !!(global.navigator && navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }

  async function requestCameraStream() {
    if (!cameraSupported()) {
      var err = new Error('Camera API unavailable');
      err.code = 'UNSUPPORTED';
      throw err;
    }
    return navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false });
  }

  function stopCameraStream(stream) {
    if (stream) stream.getTracks().forEach(function (track) { track.stop(); });
  }

  async function captureFrameAsBlob(videoEl, quality) {
    var canvas = document.createElement('canvas');
    canvas.width = videoEl.videoWidth || 1280;
    canvas.height = videoEl.videoHeight || 720;
    canvas.getContext('2d').drawImage(videoEl, 0, 0, canvas.width, canvas.height);
    return new Promise(function (resolve) { canvas.toBlob(resolve, 'image/jpeg', quality || 0.92); });
  }

  global.ScanStoryMarkerEditor = {
    MIN_CROP_FRACTION: MIN_CROP_FRACTION,
    defaultCrop: defaultCrop,
    sanitizeCrop: sanitizeCrop,
    computeDrawRect: computeDrawRect,
    cropHandles: cropHandles,
    hitTestDragMode: hitTestDragMode,
    applyDragDelta: applyDragDelta,
    drawCanvas: drawCanvas,
    drawCroppedToOutputCanvas: drawCroppedToOutputCanvas,
    exportDataUrl: exportDataUrl,
    exportBlob: exportBlob,
    evaluateQuality: evaluateQuality,
    createController: createController,
    cameraSupported: cameraSupported,
    requestCameraStream: requestCameraStream,
    stopCameraStream: stopCameraStream,
    captureFrameAsBlob: captureFrameAsBlob
  };
})(window);
