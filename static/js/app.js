// PhotoTag — minimal JS: face box selection, drawing new faces and swipe navigation.
// Uses Pointer Events so mouse, pen and touch behave the same. Event delegation keeps it
// working after HTMX swaps the photo workspace.
(function () {
  "use strict";

  const SWIPE_MIN_X = 60;
  const SWIPE_MAX_Y = 50;
  const MIN_BOX = 12; // px on screen

  let draw = null; // { frame, startX, startY, pointerId }
  let swipe = null;

  function frameOf(el) { return el && el.closest ? el.closest(".photo-frame") : null; }
  function workspace() { return document.getElementById("photo-workspace"); }

  // --- Face selection -------------------------------------------------------
  function selectFace(faceId) {
    const ws = workspace();
    if (!ws) return;
    ws.querySelectorAll(".face-box.selected").forEach((b) => b.classList.remove("selected"));
    ws.querySelectorAll(".face-panel.highlight").forEach((p) => p.classList.remove("highlight"));
    const box = ws.querySelector(`.face-box[data-face="${faceId}"]`);
    const panel = document.getElementById(`face-panel-${faceId}`);
    if (box) box.classList.add("selected");
    if (panel) {
      panel.open = true;
      panel.classList.add("highlight");
      panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
      const input = panel.querySelector("input[name=name]");
      if (input && window.matchMedia("(pointer: fine)").matches) input.focus({ preventScroll: true });
    }
  }

  document.addEventListener("click", (event) => {
    const box = event.target.closest(".face-box");
    if (box) { selectFace(box.dataset.face); return; }

    const summary = event.target.closest(".face-panel > summary");
    if (summary) {
      const panel = summary.parentElement;
      const box = workspace().querySelector(`.face-box[data-face="${panel.dataset.face}"]`);
      if (box) box.classList.toggle("selected", !panel.open);
    }

    const action = event.target.closest("[data-action]");
    if (!action) return;
    if (action.dataset.action === "draw-face") toggleDrawMode();
    if (action.dataset.action === "cancel-draw") cancelDraw();
  });

  // --- Drawing a new face ---------------------------------------------------
  function toggleDrawMode(force) {
    const ws = workspace();
    const frame = ws && ws.querySelector(".photo-frame");
    if (!frame) return;
    const on = force !== undefined ? force : !frame.classList.contains("drawing");
    frame.classList.toggle("drawing", on);
    const button = ws.querySelector('[data-action="draw-face"]');
    if (button) button.setAttribute("aria-pressed", on ? "true" : "false");
    const hint = ws.querySelector(".draw-hint");
    if (hint) hint.hidden = !on;
    if (!on) hideRect(frame);
  }

  function hideRect(frame) {
    const rect = frame.querySelector(".draw-rect");
    if (rect) rect.hidden = true;
  }

  function cancelDraw() {
    const ws = workspace();
    const form = ws && ws.querySelector("#add-face-form");
    if (form) form.hidden = true;
    toggleDrawMode(false);
  }

  function localPoint(frame, event) {
    const r = frame.getBoundingClientRect();
    return {
      x: Math.min(Math.max(event.clientX - r.left, 0), r.width),
      y: Math.min(Math.max(event.clientY - r.top, 0), r.height),
    };
  }

  function updateRect(frame, a, b) {
    const rect = frame.querySelector(".draw-rect");
    rect.hidden = false;
    rect.style.left = Math.min(a.x, b.x) + "px";
    rect.style.top = Math.min(a.y, b.y) + "px";
    rect.style.width = Math.abs(a.x - b.x) + "px";
    rect.style.height = Math.abs(a.y - b.y) + "px";
  }

  document.addEventListener("pointerdown", (event) => {
    const frame = frameOf(event.target);
    if (!frame) return;
    if (frame.classList.contains("drawing")) {
      event.preventDefault();
      frame.setPointerCapture(event.pointerId);
      const p = localPoint(frame, event);
      draw = { frame, start: p, pointerId: event.pointerId };
      updateRect(frame, p, p);
      return;
    }
    swipe = { frame, x: event.clientX, y: event.clientY, pointerId: event.pointerId };
  });

  document.addEventListener("pointermove", (event) => {
    if (!draw || event.pointerId !== draw.pointerId) return;
    event.preventDefault();
    updateRect(draw.frame, draw.start, localPoint(draw.frame, event));
  });

  document.addEventListener("pointerup", (event) => {
    if (draw && event.pointerId === draw.pointerId) {
      finishDraw(draw.frame, draw.start, localPoint(draw.frame, event));
      draw = null;
      return;
    }
    if (swipe && event.pointerId === swipe.pointerId) {
      const dx = event.clientX - swipe.x;
      const dy = event.clientY - swipe.y;
      const frame = swipe.frame;
      swipe = null;
      if (Math.abs(dx) >= SWIPE_MIN_X && Math.abs(dy) <= SWIPE_MAX_Y && !event.target.closest(".face-box")) {
        const url = dx < 0 ? frame.dataset.nextUrl : frame.dataset.prevUrl;
        if (url) window.location.href = url;
      }
    }
  });

  document.addEventListener("pointercancel", () => {
    if (draw) hideRect(draw.frame);
    draw = null;
    swipe = null;
  });

  function finishDraw(frame, a, b) {
    const r = frame.getBoundingClientRect();
    const w = Math.abs(a.x - b.x);
    const h = Math.abs(a.y - b.y);
    if (w < MIN_BOX || h < MIN_BOX) { hideRect(frame); return; }
    // Screen pixels → photo pixels (the frame keeps the photo's aspect ratio).
    const sx = Number(frame.dataset.width) / r.width;
    const sy = Number(frame.dataset.height) / r.height;
    const form = workspace().querySelector("#add-face-form");
    form.elements.x.value = Math.round(Math.min(a.x, b.x) * sx);
    form.elements.y.value = Math.round(Math.min(a.y, b.y) * sy);
    form.elements.w.value = Math.round(w * sx);
    form.elements.h.value = Math.round(h * sy);
    form.hidden = false;
    form.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // --- Date proposal form ---------------------------------------------------
  function syncDateForm(select) {
    const form = select.closest("form");
    const exact = select.value === "exact";
    form.querySelector('[data-date-field="year"]').hidden = exact;
    form.querySelector('[data-date-field="exact"]').hidden = !exact;
  }

  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-date-precision]")) syncDateForm(event.target);
  });

  // --- Keyboard navigation on desktop --------------------------------------
  document.addEventListener("keydown", (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.target.closest("input, select, textarea, [contenteditable]")) return;
    const frame = document.querySelector(".photo-frame");
    if (!frame) return;
    if (event.key === "Escape") cancelDraw();
    const url = event.key === "ArrowRight" ? frame.dataset.nextUrl
      : event.key === "ArrowLeft" ? frame.dataset.prevUrl : null;
    if (url) window.location.href = url;
  });

  // Re-select the highlighted face after HTMX swaps the workspace.
  document.addEventListener("htmx:afterSwap", () => {
    const selected = document.querySelector(".face-box.selected");
    if (selected) {
      const panel = document.getElementById(`face-panel-${selected.dataset.face}`);
      if (panel) panel.classList.add("highlight");
    }
  });
})();
