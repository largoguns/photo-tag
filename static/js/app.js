// PhotoTag — small vanilla JS layer on top of server-rendered pages and HTMX.
// Pointer Events make mouse, pen and touch behave the same. Event delegation keeps
// everything working after HTMX swaps parts of the page.
(function () {
  "use strict";

  const SWIPE_MIN_X = 60;
  const SWIPE_MAX_Y = 50;
  const TAP_MAX_MOVE = 10;
  const MIN_BOX = 12; // px on screen
  const DISMISS_DAYS = 30;

  function ready(fn) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
    else fn();
  }

  function storageGet(key) {
    try { return window.localStorage.getItem(key); } catch (error) { return null; }
  }

  function storageSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (error) { /* private mode */ }
  }

  function recentlyDismissed(key) {
    const at = Number(storageGet(key));
    return at > 0 && Date.now() - at < DISMISS_DAYS * 86400000;
  }

  function csrfToken() {
    const field = document.querySelector("[name=csrfmiddlewaretoken]");
    if (field) return field.value;
    try { return JSON.parse(document.body.getAttribute("hx-headers"))["X-CSRFToken"]; } catch (error) { return ""; }
  }

  // Accent- and case-insensitive comparison key (mirrors catalog/services/names.py).
  function nameKey(value) {
    return String(value).normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/\s+/g, " ").trim().toLowerCase();
  }

  const PARTICLES = new Set(["de", "del", "la", "las", "los", "y", "e"]);
  function capitalizeWord(word) {
    if (!word) return word;
    const letters = word.replace(/[^\p{L}]/gu, "");
    const mixed = /\p{Lu}/u.test(letters.slice(1)) && letters !== letters.toUpperCase();
    return word[0].toUpperCase() + (mixed ? word.slice(1) : word.slice(1).toLowerCase());
  }
  function normalizeName(value) {
    return String(value).split(/\s+/).filter(Boolean).map((word, index) => {
      if (index > 0 && PARTICLES.has(word.toLowerCase())) return word.toLowerCase();
      return word.split(/([-'’])/).map((part, i) => (i % 2 ? part : capitalizeWord(part))).join("");
    }).join(" ");
  }

  // No page zoom (iOS ignores user-scalable=no); the photo viewer zooms on its own.
  document.addEventListener("gesturestart", (event) => event.preventDefault());

  // --- App shell: elevated top bar and floating nav that hides on scroll -------
  let lastScrollY = window.scrollY;
  let scrollTicking = false;
  window.addEventListener("scroll", () => {
    if (scrollTicking) return;
    scrollTicking = true;
    window.requestAnimationFrame(() => {
      const y = window.scrollY;
      document.body.classList.toggle("scrolled", y > 4);
      if (y > lastScrollY + 8 && y > 80) document.body.classList.add("nav-hidden");
      else if (y < lastScrollY - 8) document.body.classList.remove("nav-hidden");
      lastScrollY = y;
      scrollTicking = false;
    });
  }, { passive: true });

  // User menu: close when tapping outside or pressing Escape.
  document.addEventListener("click", (event) => {
    document.querySelectorAll(".user-menu[open]").forEach((menu) => {
      if (!menu.contains(event.target)) menu.removeAttribute("open");
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    document.querySelectorAll(".user-menu[open]").forEach((menu) => {
      menu.removeAttribute("open");
      menu.querySelector("summary").focus();
    });
  });

  // --- Photo page: face selection -----------------------------------------------
  function workspace() { return document.getElementById("photo-workspace"); }
  function frameOf(el) { return el && el.closest ? el.closest(".photo-frame") : null; }

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
      const faceBox = workspace().querySelector(`.face-box[data-face="${panel.dataset.face}"]`);
      if (faceBox) faceBox.classList.toggle("selected", !panel.open);
    }

    const action = event.target.closest("[data-action]");
    if (!action) return;
    if (action.dataset.action === "draw-face") toggleDrawMode();
    if (action.dataset.action === "cancel-draw") cancelDraw();
    if (action.dataset.action === "open-viewer") openViewer();
  });

  // --- Photo page: drawing a new face, swipe and tap-to-zoom ---------------------
  let draw = null;
  let gesture = null;

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
    gesture = { frame, x: event.clientX, y: event.clientY, pointerId: event.pointerId };
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
    if (!gesture || event.pointerId !== gesture.pointerId) return;
    const dx = event.clientX - gesture.x;
    const dy = event.clientY - gesture.y;
    const frame = gesture.frame;
    gesture = null;
    if (event.target.closest(".face-box")) return;
    if (Math.abs(dx) >= SWIPE_MIN_X && Math.abs(dy) <= SWIPE_MAX_Y) {
      const url = dx < 0 ? frame.dataset.nextUrl : frame.dataset.prevUrl;
      if (url) window.location.href = url;
    } else if (Math.abs(dx) < TAP_MAX_MOVE && Math.abs(dy) < TAP_MAX_MOVE) {
      openViewer(); // tap on the photo (not on a face): full screen with zoom
    }
  });

  document.addEventListener("pointercancel", () => {
    if (draw) hideRect(draw.frame);
    draw = null;
    gesture = null;
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

  // --- Photo page: full-screen viewer with its own pinch/pan zoom -----------------
  const viewer = { el: null, scale: 1, fit: 1, x: 0, y: 0, pointers: new Map(), pinch: null, lastTap: 0 };
  const MAX_ZOOM = 8;

  function viewerParts() {
    const el = document.getElementById("photo-viewer");
    if (!el) return null;
    return { el, stage: el.querySelector("[data-viewer-stage]"), content: el.querySelector("[data-viewer-content]") };
  }

  function applyTransform() {
    const parts = viewerParts();
    if (!parts) return;
    const frame = document.getElementById("photo-frame");
    const w = Number(frame.dataset.width) * viewer.scale;
    const h = Number(frame.dataset.height) * viewer.scale;
    const sw = parts.stage.clientWidth;
    const sh = parts.stage.clientHeight;
    // Keep the photo centred when smaller than the screen, and its edges in view otherwise.
    viewer.x = w <= sw ? (sw - w) / 2 : Math.min(0, Math.max(sw - w, viewer.x));
    viewer.y = h <= sh ? (sh - h) / 2 : Math.min(0, Math.max(sh - h, viewer.y));
    parts.content.style.transform = `translate(${viewer.x}px, ${viewer.y}px) scale(${viewer.scale})`;
  }

  function resetViewer() {
    const parts = viewerParts();
    const frame = document.getElementById("photo-frame");
    if (!parts || !frame) return;
    const pw = Number(frame.dataset.width);
    const ph = Number(frame.dataset.height);
    parts.content.style.width = `${pw}px`;
    parts.content.style.height = `${ph}px`;
    viewer.fit = Math.min(parts.stage.clientWidth / pw, parts.stage.clientHeight / ph);
    viewer.scale = viewer.fit;
    viewer.x = 0;
    viewer.y = 0;
    applyTransform();
  }

  function zoomAt(factor, cx, cy) {
    const next = Math.min(viewer.fit * MAX_ZOOM, Math.max(viewer.fit, viewer.scale * factor));
    const ratio = next / viewer.scale;
    viewer.x = cx - (cx - viewer.x) * ratio;
    viewer.y = cy - (cy - viewer.y) * ratio;
    viewer.scale = next;
    applyTransform();
  }

  function openViewer() {
    const parts = viewerParts();
    const frame = document.getElementById("photo-frame");
    if (!parts || !frame || frame.classList.contains("drawing") || parts.el.open) return;
    parts.el.showModal();
    resetViewer();
    history.pushState({ photoViewer: true }, "");
    const img = parts.content.querySelector("img");
    if (frame.dataset.fullUrl && !img.dataset.full) {
      const full = new Image();
      full.onload = () => { img.src = full.src; img.dataset.full = "1"; };
      full.src = frame.dataset.fullUrl;
    }
  }

  function closeViewer(fromHistory) {
    const parts = viewerParts();
    if (!parts || !parts.el.open) return;
    parts.el.close();
    viewer.pointers.clear();
    if (!fromHistory && history.state && history.state.photoViewer) history.back();
  }

  window.addEventListener("popstate", () => closeViewer(true));
  window.addEventListener("resize", () => { const p = viewerParts(); if (p && p.el.open) resetViewer(); });

  document.addEventListener("click", (event) => {
    const control = event.target.closest("[data-viewer]");
    const parts = viewerParts();
    if (!control || !parts) return;
    const centre = [parts.stage.clientWidth / 2, parts.stage.clientHeight / 2];
    const action = control.dataset.viewer;
    if (action === "close") closeViewer(false);
    if (action === "zoom-in") zoomAt(1.6, ...centre);
    if (action === "zoom-out") zoomAt(1 / 1.6, ...centre);
    if (action === "faces") {
      const on = !parts.el.classList.contains("show-faces");
      parts.el.classList.toggle("show-faces", on);
      control.setAttribute("aria-pressed", on ? "true" : "false");
    }
  });

  document.addEventListener("cancel", (event) => {
    if (event.target.id === "photo-viewer") { event.preventDefault(); closeViewer(false); }
  }, true);

  function stagePoint(stage, event) {
    const r = stage.getBoundingClientRect();
    return { x: event.clientX - r.left, y: event.clientY - r.top };
  }

  document.addEventListener("pointerdown", (event) => {
    const stage = event.target.closest("[data-viewer-stage]");
    if (!stage) return;
    viewer.pointers.set(event.pointerId, { ...stagePoint(stage, event), startX: event.clientX, startY: event.clientY });
    stage.classList.add("dragging");
    if (viewer.pointers.size === 2) {
      const [a, b] = [...viewer.pointers.values()];
      viewer.pinch = {
        distance: Math.hypot(a.x - b.x, a.y - b.y),
        scale: viewer.scale,
        mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
        origin: { x: viewer.x, y: viewer.y },
      };
    }
  });

  window.addEventListener("pointermove", (event) => {
    if (!viewer.pointers.has(event.pointerId)) return;
    const parts = viewerParts();
    const previous = viewer.pointers.get(event.pointerId);
    const point = stagePoint(parts.stage, event);
    viewer.pointers.set(event.pointerId, { ...previous, ...point });
    if (viewer.pointers.size === 2 && viewer.pinch) {
      const [a, b] = [...viewer.pointers.values()];
      const p = viewer.pinch;
      const scale = Math.min(viewer.fit * MAX_ZOOM, Math.max(viewer.fit,
        p.scale * Math.hypot(a.x - b.x, a.y - b.y) / (p.distance || 1)));
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      // Keep the photo point under the fingers' midpoint fixed while scaling.
      viewer.x = mid.x - (p.mid.x - p.origin.x) * (scale / p.scale);
      viewer.y = mid.y - (p.mid.y - p.origin.y) * (scale / p.scale);
      viewer.scale = scale;
      applyTransform();
    } else if (viewer.pointers.size === 1) {
      viewer.x += point.x - previous.x;
      viewer.y += point.y - previous.y;
      applyTransform();
    }
  });

  function endViewerPointer(event) {
    const pointer = viewer.pointers.get(event.pointerId);
    if (!pointer) return;
    viewer.pointers.delete(event.pointerId);
    if (viewer.pointers.size < 2) viewer.pinch = null;
    const parts = viewerParts();
    if (viewer.pointers.size === 0 && parts) parts.stage.classList.remove("dragging");
    if (event.type !== "pointerup") return;
    const moved = Math.hypot(event.clientX - pointer.startX, event.clientY - pointer.startY);
    if (moved > TAP_MAX_MOVE) return;
    const face = document.elementFromPoint(event.clientX, event.clientY);
    const faceButton = face && face.closest(".viewer-face");
    if (faceButton && parts.el.classList.contains("show-faces")) {
      closeViewer(false);
      selectFace(faceButton.dataset.face);
      return;
    }
    // Double tap toggles between "fit" and 2.5×.
    const now = Date.now();
    if (now - viewer.lastTap < 300) {
      const point = stagePoint(parts.stage, event);
      if (viewer.scale > viewer.fit * 1.05) resetViewer();
      else zoomAt(2.5, point.x, point.y);
      viewer.lastTap = 0;
    } else {
      viewer.lastTap = now;
    }
  }
  window.addEventListener("pointerup", endViewerPointer);
  window.addEventListener("pointercancel", endViewerPointer);

  document.addEventListener("wheel", (event) => {
    const stage = event.target.closest && event.target.closest("[data-viewer-stage]");
    if (!stage) return;
    event.preventDefault();
    const point = stagePoint(stage, event);
    zoomAt(Math.exp(-event.deltaY * 0.0015), point.x, point.y);
  }, { passive: false });

  // --- Date proposal form ----------------------------------------------------------
  document.addEventListener("change", (event) => {
    if (!event.target.matches("[data-date-precision]")) return;
    const form = event.target.closest("form");
    const exact = event.target.value === "exact";
    form.querySelector('[data-date-field="year"]').hidden = exact;
    form.querySelector('[data-date-field="exact"]').hidden = !exact;
  });

  // --- Event proposal: "Other…" reveals the new event name field --------------------
  function syncEventForm(select) {
    const field = select.closest("form").querySelector("[data-event-name]");
    if (!field) return;
    const isNew = select.value === "new";
    field.hidden = !isNew;
    field.querySelector("input").required = isNew;
    if (isNew) field.querySelector("input").focus();
  }

  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-event-type]")) syncEventForm(event.target);
  });

  // Without JS the name field stays visible; with JS it only shows for "Other…".
  function initEventForms(root) {
    root.querySelectorAll("[data-event-name]").forEach((field) => {
      const select = field.closest("form").querySelector("[data-event-type]");
      field.hidden = !select || select.value !== "new";
    });
  }
  ready(() => initEventForms(document));
  document.addEventListener("htmx:afterSwap", (event) => initEventForms(event.target));

  // --- Keyboard navigation on desktop ----------------------------------------------
  document.addEventListener("keydown", (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.target.closest("input, select, textarea, [contenteditable], dialog")) return;
    const frame = document.querySelector(".photo-frame");
    if (!frame) return;
    if (event.key === "Escape") cancelDraw();
    const url = event.key === "ArrowRight" ? frame.dataset.nextUrl
      : event.key === "ArrowLeft" ? frame.dataset.prevUrl : null;
    if (url) window.location.href = url;
  });

  // --- Person name autocomplete (accent-insensitive, prefers existing people) ------
  let peopleCache = null;
  function people() {
    if (peopleCache) return peopleCache;
    const data = document.getElementById("people-data");
    try { peopleCache = data ? JSON.parse(data.textContent) : []; } catch (error) { peopleCache = []; }
    peopleCache.forEach((person) => { person.key = nameKey(person.name); });
    return peopleCache;
  }

  function comboboxList(input) { return document.getElementById(input.getAttribute("aria-controls")); }

  function closeCombobox(input) {
    const list = comboboxList(input);
    if (list) { list.hidden = true; list.innerHTML = ""; }
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }

  function renderCombobox(input) {
    const list = comboboxList(input);
    const query = nameKey(input.value);
    if (!list || !query) { closeCombobox(input); return; }
    const matches = people()
      .filter((person) => person.key.includes(query))
      .sort((a, b) => Number(!a.key.startsWith(query)) - Number(!b.key.startsWith(query)) || a.key.localeCompare(b.key))
      .slice(0, 8);
    list.innerHTML = "";
    const add = (label, value, avatar, extraClass) => {
      const option = document.createElement("li");
      option.className = `combobox-option${extraClass ? " " + extraClass : ""}`;
      option.id = `${list.id}-${list.children.length}`;
      option.setAttribute("role", "option");
      option.dataset.value = value;
      if (avatar) {
        const img = document.createElement("img");
        img.src = avatar; img.alt = ""; img.loading = "lazy";
        option.appendChild(img);
      } else {
        const initial = document.createElement("span");
        initial.className = "avatar-mini";
        initial.setAttribute("aria-hidden", "true");
        initial.textContent = value.slice(0, 1).toUpperCase();
        option.appendChild(initial);
      }
      const text = document.createElement("span");
      text.textContent = label;
      option.appendChild(text);
      list.appendChild(option);
    };
    matches.forEach((person) => add(person.name, person.name, person.avatar));
    const normalized = normalizeName(input.value);
    if (normalized && !people().some((person) => person.key === nameKey(normalized))) {
      const template = document.getElementById("combobox-create-template");
      const label = template ? template.textContent.replace("%s", normalized) : normalized;
      add(label, normalized, "", "create");
    }
    list.hidden = list.children.length === 0;
    input.setAttribute("aria-expanded", list.hidden ? "false" : "true");
  }

  function moveComboboxSelection(input, step) {
    const list = comboboxList(input);
    if (!list || list.hidden) return;
    const options = [...list.children];
    const current = options.findIndex((o) => o.getAttribute("aria-selected") === "true");
    const next = (current + step + options.length) % options.length;
    options.forEach((o, i) => o.setAttribute("aria-selected", i === next ? "true" : "false"));
    input.setAttribute("aria-activedescendant", options[next].id);
    options[next].scrollIntoView({ block: "nearest" });
  }

  function chooseOption(input, option) {
    input.value = option.dataset.value;
    closeCombobox(input);
    input.focus();
  }

  document.addEventListener("input", (event) => {
    if (event.target.matches("[data-person-combobox]")) renderCombobox(event.target);
  });

  document.addEventListener("keydown", (event) => {
    const input = event.target;
    if (!input.matches || !input.matches("[data-person-combobox]")) return;
    if (event.key === "ArrowDown") { event.preventDefault(); moveComboboxSelection(input, 1); }
    else if (event.key === "ArrowUp") { event.preventDefault(); moveComboboxSelection(input, -1); }
    else if (event.key === "Escape") closeCombobox(input);
    else if (event.key === "Enter") {
      const list = comboboxList(input);
      const selected = list && !list.hidden && list.querySelector('[aria-selected="true"]');
      if (selected) { event.preventDefault(); chooseOption(input, selected); }
    }
  });

  // pointerdown (not click) so the choice lands before the input loses focus.
  document.addEventListener("pointerdown", (event) => {
    const option = event.target.closest(".combobox-option");
    if (!option) return;
    event.preventDefault();
    const input = document.querySelector(`[aria-controls="${option.parentElement.id}"]`);
    if (input) chooseOption(input, option);
  });

  document.addEventListener("focusout", (event) => {
    if (event.target.matches && event.target.matches("[data-person-combobox]")) {
      window.setTimeout(() => closeCombobox(event.target), 150);
    }
  });

  // Normalize the name before sending ("adri" → "Adri"); the server does it too.
  document.addEventListener("submit", (event) => {
    const input = event.target.querySelector && event.target.querySelector("[data-person-combobox]");
    if (input) input.value = normalizeName(input.value);
  }, true);

  // --- Gallery filter sheets ---------------------------------------------------------
  document.addEventListener("click", (event) => {
    const opener = event.target.closest("[data-open-sheet]");
    if (opener) {
      const sheet = document.getElementById(opener.dataset.openSheet);
      if (sheet && typeof sheet.showModal === "function") sheet.showModal();
      return;
    }
    // Tapping the backdrop closes sheets and the confirmation dialog.
    const dialog = event.target;
    if (dialog.tagName === "DIALOG" && dialog.open && !dialog.classList.contains("viewer")) {
      const r = dialog.getBoundingClientRect();
      const inside = event.clientX >= r.left && event.clientX <= r.right && event.clientY >= r.top && event.clientY <= r.bottom;
      if (!inside) dialog.close("cancel");
    }
  });

  document.addEventListener("input", (event) => {
    if (!event.target.matches("[data-sheet-search]")) return;
    const query = nameKey(event.target.value);
    event.target.closest("dialog").querySelectorAll("[data-option-label]").forEach((item) => {
      item.hidden = !!query && !nameKey(item.dataset.optionLabel).includes(query);
    });
  });

  // --- Sharing photos (device share sheet, or download as a fallback) -------------------
  // Files are fetched first; if that takes long enough for the browser to drop the tap's
  // "user activation", sharing is refused, so the button turns into "Share now" and the
  // second tap shares the files that are already downloaded.
  const preparedShares = new WeakMap();

  async function photoFiles(ids) {
    return Promise.all(ids.map(async (id) => {
      const response = await fetch(`/photos/${id}/image/full/`, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return new File([await response.blob()], `PhotoTag-${id}.jpg`, { type: "image/jpeg" });
    }));
  }

  function canShareFiles() {
    if (!navigator.canShare || !window.File) return false;
    try {
      return navigator.canShare({ files: [new File([""], "x.jpg", { type: "image/jpeg" })] });
    } catch (error) {
      return false;
    }
  }

  function logShare(url, ids) {
    const body = new URLSearchParams({ ids: ids.join(",") });
    fetch(url, { method: "POST", credentials: "same-origin", headers: { "X-CSRFToken": csrfToken() }, body }).catch(() => {});
  }

  async function sharePhotos(button, ids, downloadUrl, logUrl) {
    if (!ids.length) return;
    if (!canShareFiles()) {
      window.location.href = downloadUrl; // desktop browsers: JPEG or ZIP download
      return;
    }
    const label = button.querySelector("span");
    const originalText = label ? label.textContent : "";
    const key = ids.join(",");
    let files = preparedShares.get(button);
    if (!files || files.key !== key) {
      button.disabled = true;
      try {
        files = await photoFiles(ids);
        files.key = key;
        preparedShares.set(button, files);
      } catch (error) {
        button.disabled = false;
        window.location.href = downloadUrl;
        return;
      }
      button.disabled = false;
    }
    try {
      await navigator.share({ files });
      logShare(logUrl, ids);
      preparedShares.delete(button);
      button.classList.remove("ready");
      if (label) label.textContent = originalText;
    } catch (error) {
      if (error.name === "NotAllowedError") {
        // Files are ready now: the next tap shares them straight away.
        const holder = button.closest("[data-ready-text]");
        const ready = button.dataset.readyText || (holder && holder.dataset.readyText) || "";
        if (label) label.textContent = ready;
        else if (ready) { button.title = ready; button.setAttribute("aria-label", ready); }
        button.classList.add("ready");
      } else if (error.name !== "AbortError") {
        window.location.href = downloadUrl;
      }
    }
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-share-photos]");
    if (!button) return;
    sharePhotos(button, [button.dataset.sharePhotos], button.dataset.downloadUrl, button.dataset.logUrl);
  });

  // --- Gallery selection mode ------------------------------------------------------------
  const selection = new Set();
  const LONG_PRESS_MS = 500;
  let pressTimer = null;
  let pressStart = null;
  let suppressClick = false;

  function selectionBar() { return document.getElementById("selection-bar"); }

  function renderSelection(message) {
    const bar = selectionBar();
    if (!bar) return;
    document.querySelectorAll(".jitem[data-photo-id]").forEach((item) => {
      item.classList.toggle("selected", selection.has(item.dataset.photoId));
    });
    const count = selection.size;
    bar.querySelector("[data-selection-count]").textContent = message
      || (count === 1 ? bar.dataset.one : bar.dataset.many.replace("%s", count));
    bar.querySelectorAll('[data-selection="share"], [data-selection="download"]').forEach((b) => { b.disabled = count === 0; });
  }

  function startSelection(firstId) {
    const bar = selectionBar();
    if (!bar) return;
    document.body.classList.add("selecting");
    bar.hidden = false;
    if (firstId) selection.add(firstId);
    renderSelection();
  }

  function endSelection() {
    selection.clear();
    document.body.classList.remove("selecting");
    const bar = selectionBar();
    if (bar) bar.hidden = true;
    renderSelection();
  }

  function toggleSelected(id) {
    const bar = selectionBar();
    if (selection.has(id)) selection.delete(id);
    else if (selection.size >= Number(bar.dataset.max)) { renderSelection(bar.dataset.limit); return; }
    else selection.add(id);
    renderSelection();
  }

  function selectionUrls() {
    const bar = selectionBar();
    const ids = [...selection];
    return { ids, download: `${bar.dataset.downloadUrl}?ids=${ids.join(",")}`, log: bar.dataset.logUrl };
  }

  document.addEventListener("click", (event) => {
    const control = event.target.closest("[data-selection]");
    if (control) {
      const action = control.dataset.selection;
      if (action === "start") startSelection();
      if (action === "cancel") endSelection();
      if (action === "download") window.location.href = selectionUrls().download;
      if (action === "share") {
        const { ids, download, log } = selectionUrls();
        sharePhotos(control, ids, download, log);
      }
      return;
    }
    const item = event.target.closest(".jitem[data-photo-id]");
    if (!item) return;
    if (suppressClick) { event.preventDefault(); suppressClick = false; return; }
    if (document.body.classList.contains("selecting")) {
      event.preventDefault();
      toggleSelected(item.dataset.photoId);
    }
  });

  // Long press on a photo starts selecting (like Google Photos).
  document.addEventListener("pointerdown", (event) => {
    const item = event.target.closest(".jitem[data-photo-id]");
    if (!item || document.body.classList.contains("selecting")) return;
    pressStart = { x: event.clientX, y: event.clientY };
    pressTimer = window.setTimeout(() => {
      pressTimer = null;
      suppressClick = true;
      startSelection(item.dataset.photoId);
      if (navigator.vibrate) navigator.vibrate(20);
    }, LONG_PRESS_MS);
  });
  document.addEventListener("pointermove", (event) => {
    if (pressTimer && pressStart && Math.hypot(event.clientX - pressStart.x, event.clientY - pressStart.y) > 10) {
      window.clearTimeout(pressTimer);
      pressTimer = null;
    }
  });
  ["pointerup", "pointercancel"].forEach((type) => document.addEventListener(type, () => {
    if (pressTimer) { window.clearTimeout(pressTimer); pressTimer = null; }
  }));
  document.addEventListener("contextmenu", (event) => {
    if (event.target.closest(".jitem")) event.preventDefault();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.classList.contains("selecting")) endSelection();
  });
  // New pages from infinite scroll keep the marks; a new filter starts over.
  document.addEventListener("htmx:afterSwap", (event) => {
    if (event.target.id === "gallery-body") endSelection();
    else renderSelection();
  });

  // --- Gallery zoom: pinch to change the row height (Google Photos style) -------------
  const ROW_KEY = "phototag:gallery-row-h";
  const ROW_MIN = 64;
  const ROW_MAX = 360;
  const galleryPointers = new Map();
  let galleryPinch = null;

  function currentRowHeight() {
    return parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--row-h")) || 120;
  }

  // Keep the photo under the fingers in place while the rows reflow.
  function setRowHeight(px, anchorX, anchorY) {
    const height = Math.round(Math.min(ROW_MAX, Math.max(ROW_MIN, px)));
    const anchor = anchorY !== undefined && document.elementFromPoint(anchorX, anchorY);
    const item = anchor && anchor.closest ? anchor.closest(".jitem") : null;
    const before = item ? item.getBoundingClientRect().top : 0;
    document.documentElement.style.setProperty("--row-h", `${height}px`);
    if (item) window.scrollBy(0, item.getBoundingClientRect().top - before);
    storageSet(ROW_KEY, String(height));
  }

  ready(() => {
    const saved = Number(storageGet(ROW_KEY));
    if (saved >= ROW_MIN && saved <= ROW_MAX) document.documentElement.style.setProperty("--row-h", `${saved}px`);
  });

  const nativeGestures = "ongesturechange" in window; // iOS Safari reports the scale itself

  document.addEventListener("pointerdown", (event) => {
    if (nativeGestures || event.pointerType !== "touch" || !event.target.closest("[data-gallery-zoom]")) return;
    galleryPointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (galleryPointers.size === 2) {
      const [a, b] = [...galleryPointers.values()];
      galleryPinch = { distance: Math.hypot(a.x - b.x, a.y - b.y), row: currentRowHeight() };
      if (pressTimer) { window.clearTimeout(pressTimer); pressTimer = null; } // not a long press
    }
  });
  document.addEventListener("pointermove", (event) => {
    if (!galleryPointers.has(event.pointerId)) return;
    galleryPointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (galleryPinch && galleryPointers.size === 2) {
      const [a, b] = [...galleryPointers.values()];
      const scale = Math.hypot(a.x - b.x, a.y - b.y) / (galleryPinch.distance || 1);
      setRowHeight(galleryPinch.row * scale, (a.x + b.x) / 2, (a.y + b.y) / 2);
      suppressClick = true;
    }
  });
  ["pointerup", "pointercancel"].forEach((type) => document.addEventListener(type, (event) => {
    galleryPointers.delete(event.pointerId);
    if (galleryPointers.size < 2) galleryPinch = null;
  }));

  let gestureRow = null;
  document.addEventListener("gesturestart", (event) => {
    if (event.target.closest && event.target.closest("[data-gallery-zoom]")) gestureRow = currentRowHeight();
  });
  document.addEventListener("gesturechange", (event) => {
    if (gestureRow === null) return;
    event.preventDefault();
    setRowHeight(gestureRow * event.scale, event.clientX, event.clientY);
  });
  document.addEventListener("gestureend", () => { gestureRow = null; });

  // Desktop: Ctrl + wheel (also what a trackpad pinch sends).
  document.addEventListener("wheel", (event) => {
    if (!event.ctrlKey || !event.target.closest || !event.target.closest("[data-gallery-zoom]")) return;
    event.preventDefault();
    setRowHeight(currentRowHeight() * Math.exp(-event.deltaY * 0.01), event.clientX, event.clientY);
  }, { passive: false });

  // --- Confirmation dialog (replaces window.confirm) -------------------------------
  // Plain forms use data-confirm="…"; HTMX forms use hx-confirm="…". Both may add
  // data-confirm-ok (button label) and data-confirm-variant="danger".
  function askConfirmation(message, source) {
    const dialog = document.getElementById("confirm-dialog");
    if (!dialog || typeof dialog.showModal !== "function") {
      return Promise.resolve(window.confirm(message));
    }
    const ok = dialog.querySelector("[data-confirm-ok]");
    if (!ok.dataset.defaultLabel) ok.dataset.defaultLabel = ok.textContent;
    const settings = source ? source.closest("[data-confirm-ok], [data-confirm-variant]") : null;
    ok.textContent = (settings && settings.dataset.confirmOk) || ok.dataset.defaultLabel;
    ok.classList.toggle("danger-fill", !!settings && settings.dataset.confirmVariant === "danger");
    dialog.querySelector("#confirm-message").textContent = message;
    dialog.returnValue = "";

    const opener = document.activeElement;
    dialog.showModal();
    ok.focus();
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => {
        if (opener && typeof opener.focus === "function") opener.focus({ preventScroll: true });
        resolve(dialog.returnValue === "ok");
      }, { once: true });
    });
  }

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form.matches("form[data-confirm]") || form.hasAttribute("hx-confirm")) return;
    if (form.dataset.confirmed === "true") { delete form.dataset.confirmed; return; }
    event.preventDefault();
    const submitter = event.submitter;
    askConfirmation(form.dataset.confirm, form).then((accepted) => {
      if (!accepted) return;
      form.dataset.confirmed = "true";
      if (typeof form.requestSubmit === "function") form.requestSubmit(submitter || undefined);
      else form.submit();
    });
  }, true);

  document.addEventListener("htmx:confirm", (event) => {
    if (!event.detail.question) return;
    event.preventDefault();
    askConfirmation(event.detail.question, event.detail.elt).then((accepted) => {
      if (accepted) event.detail.issueRequest(true);
    });
  });

  // --- Upload page ----------------------------------------------------------------
  function formatSize(bytes) {
    return bytes > 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.ceil(bytes / 1024)} KB`;
  }

  function listFiles(form, files) {
    const list = form.querySelector("[data-file-list]");
    list.innerHTML = "";
    [...files].forEach((file) => {
      const item = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = file.name;
      const status = document.createElement("span");
      status.className = "muted";
      status.textContent = formatSize(file.size);
      item.append(name, status);
      list.appendChild(item);
    });
    list.hidden = files.length === 0;
  }

  ready(() => {
    const form = document.getElementById("upload-form");
    if (!form) return;
    const input = form.querySelector('input[type="file"]');
    const zone = form.querySelector(".dropzone");
    const progress = form.querySelector("[data-upload-progress]");
    const message = form.querySelector("[data-upload-message]");

    form.addEventListener("change", (event) => {
      if (event.target === input) listFiles(form, input.files);
      if (event.target.name === "kind") {
        form.querySelectorAll("[data-kind-hint]").forEach((hint) => {
          hint.hidden = hint.dataset.kindHint !== event.target.value;
        });
      }
    });
    ["dragenter", "dragover"].forEach((type) => zone.addEventListener(type, (event) => {
      event.preventDefault();
      zone.classList.add("dragover");
    }));
    ["dragleave", "drop"].forEach((type) => zone.addEventListener(type, () => zone.classList.remove("dragover")));
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      input.files = event.dataTransfer.files;
      listFiles(form, input.files);
    });

    form.addEventListener("submit", (event) => {
      if (!input.files.length) return;
      event.preventDefault();
      const button = form.querySelector('[type="submit"]');
      const request = new XMLHttpRequest();
      request.open("POST", form.action || window.location.href);
      request.setRequestHeader("Accept", "application/json");
      request.setRequestHeader("X-CSRFToken", csrfToken());
      request.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) progress.value = Math.round((100 * e.loaded) / e.total);
      });
      request.addEventListener("load", () => {
        let results = [];
        try { results = JSON.parse(request.responseText).results || []; } catch (error) { results = []; }
        if (request.status !== 200 || !results.length) {
          message.textContent = form.dataset.errorText;
          message.hidden = false;
          button.disabled = false;
          return;
        }
        const items = form.querySelectorAll("[data-file-list] li");
        results.forEach((result, i) => {
          if (items[i]) items[i].lastChild.textContent = result.message;
        });
        window.setTimeout(() => window.location.reload(), 1500);
      });
      request.addEventListener("error", () => {
        message.textContent = form.dataset.errorText;
        message.hidden = false;
        button.disabled = false;
      });
      button.disabled = true;
      progress.hidden = false;
      progress.value = 0;
      message.textContent = form.dataset.uploadingText;
      message.hidden = false;
      request.send(new FormData(form));
    });
  });

  // --- Progressive Web App: service worker and install prompt -----------------------
  // Service workers need a secure context (HTTPS via Tailscale Funnel, or localhost).
  if ("serviceWorker" in navigator && window.isSecureContext) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    });
  }

  const INSTALL_DISMISS_KEY = "phototag:install-dismissed-at";
  const PUSH_DISMISS_KEY = "phototag:push-prompt-dismissed-at";
  const INSTALL_PROMPT_WAIT_MS = 2500;
  let deferredInstall = null;

  function isInstalled() {
    return window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  }

  // Which install flow the user agent supports: "ios", "android" or null (desktop).
  function mobilePlatform() {
    const ua = navigator.userAgent || "";
    // iPadOS reports itself as a Mac; touch support gives it away.
    if (/iPhone|iPad|iPod/i.test(ua) || (/Macintosh/i.test(ua) && navigator.maxTouchPoints > 1)) {
      return "ios";
    }
    const mobile = navigator.userAgentData ? navigator.userAgentData.mobile : /Mobi/i.test(ua);
    return /Android/i.test(ua) || mobile ? "android" : null;
  }

  function showBanner(id) {
    const banner = document.getElementById(id);
    if (!banner) return null;
    banner.hidden = false;
    document.body.classList.add("has-banner");
    return banner;
  }

  function hideBanner(id) {
    const banner = document.getElementById(id);
    if (banner) banner.hidden = true;
    if (!document.querySelector(".banner:not([hidden])")) document.body.classList.remove("has-banner");
  }

  function showInstallBanner(mode) {
    const banner = showBanner("install-banner");
    if (!banner) return;
    banner.querySelectorAll("[data-install-hint]").forEach((hint) => {
      hint.hidden = hint.dataset.installHint !== mode;
    });
    banner.querySelector('[data-install-action="install"]').hidden = mode !== "prompt";
  }

  function hideInstallBanner(remember) {
    hideBanner("install-banner");
    if (remember) storageSet(INSTALL_DISMISS_KEY, String(Date.now()));
  }

  // Chromium fires this when the app is installable; keep it to prompt later.
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredInstall = event;
    if (mobilePlatform() && !isInstalled() && !recentlyDismissed(INSTALL_DISMISS_KEY)) showInstallBanner("prompt");
  });

  window.addEventListener("appinstalled", () => {
    deferredInstall = null;
    hideInstallBanner(false);
  });

  document.addEventListener("click", (event) => {
    const action = event.target.closest("[data-install-action]");
    if (!action) return;
    if (action.dataset.installAction === "dismiss") {
      hideInstallBanner(true);
      return;
    }
    if (!deferredInstall) return;
    const prompt = deferredInstall;
    deferredInstall = null;
    prompt.prompt();
    prompt.userChoice.then((choice) => hideInstallBanner(choice.outcome !== "accepted"));
  });

  ready(() => {
    const platform = mobilePlatform();
    if (!platform || isInstalled() || recentlyDismissed(INSTALL_DISMISS_KEY)) return;
    if (platform === "ios") {
      showInstallBanner("ios"); // iOS has no install prompt: explain the Share menu.
      return;
    }
    // Android: wait for Chromium's prompt; otherwise (e.g. Firefox) explain the menu.
    window.setTimeout(() => {
      if (!deferredInstall && !isInstalled()) showInstallBanner("android");
    }, INSTALL_PROMPT_WAIT_MS);
  });

  // --- Push notifications -------------------------------------------------------------
  function base64UrlToBytes(value) {
    const padded = value + "=".repeat((4 - (value.length % 4)) % 4);
    const binary = window.atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(binary, (char) => char.charCodeAt(0));
  }

  function pushSupported() {
    return "serviceWorker" in navigator && "PushManager" in window
      && "Notification" in window && window.isSecureContext;
  }

  async function pushRegistration() {
    return (await navigator.serviceWorker.getRegistration("/"))
      || navigator.serviceWorker.register("/sw.js");
  }

  async function postJSON(url, body) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
      body: JSON.stringify(body || {}),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  // Ask permission (must follow a tap: iOS requires it) and register this device.
  async function subscribeDevice(publicKey, subscribeUrl) {
    if (await Notification.requestPermission() !== "granted") return false;
    const registration = await pushRegistration();
    const subscription = (await registration.pushManager.getSubscription())
      || await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: base64UrlToBytes(publicKey) });
    await postJSON(subscribeUrl, subscription.toJSON());
    return true;
  }

  async function currentPushState() {
    if (!pushSupported()) return mobilePlatform() === "ios" && !isInstalled() ? "ios-install" : "unsupported";
    if (Notification.permission === "denied") return "denied";
    const registration = await pushRegistration();
    return (await registration.pushManager.getSubscription()) ? "on" : "off";
  }

  // Installed app without notifications on this device: offer to turn them on.
  ready(async () => {
    const { pushKey, subscribeUrl } = document.body.dataset;
    if (!pushKey || !isInstalled() || !pushSupported() || recentlyDismissed(PUSH_DISMISS_KEY)) return;
    if (document.getElementById("push-card")) return; // the profile card already offers it
    try {
      if (await currentPushState() !== "off") return;
    } catch (error) {
      return;
    }
    const banner = showBanner("push-banner");
    if (!banner) return;
    banner.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-push-banner]");
      if (!button) return;
      if (button.dataset.pushBanner === "dismiss") {
        storageSet(PUSH_DISMISS_KEY, String(Date.now()));
        hideBanner("push-banner");
        return;
      }
      button.disabled = true;
      try {
        if (await subscribeDevice(pushKey, subscribeUrl)) hideBanner("push-banner");
        else { storageSet(PUSH_DISMISS_KEY, String(Date.now())); hideBanner("push-banner"); }
      } catch (error) {
        banner.querySelector("[data-push-banner-error]").hidden = false;
        button.disabled = false;
      }
    });
  });

  function showPushState(card, state, message) {
    card.querySelectorAll("[data-push-state]").forEach((el) => { el.hidden = el.dataset.pushState !== state; });
    card.querySelector('[data-push-action="enable"]').hidden = state !== "off";
    card.querySelector('[data-push-action="disable"]').hidden = state !== "on";
    card.querySelector('[data-push-action="test"]').hidden = state !== "on";
    const note = card.querySelector("[data-push-message]");
    note.textContent = message || "";
    note.hidden = !message;
  }

  ready(async () => {
    const card = document.getElementById("push-card");
    if (!card) return;
    try {
      showPushState(card, await currentPushState());
    } catch (error) {
      showPushState(card, "unsupported");
    }

    card.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-push-action]");
      if (!button) return;
      button.disabled = true;
      try {
        const action = button.dataset.pushAction;
        if (action === "enable") {
          if (!await subscribeDevice(card.dataset.vapidKey, card.dataset.subscribeUrl)) {
            showPushState(card, "denied");
            return;
          }
          window.location.reload(); // refresh the device list
        } else if (action === "disable") {
          const registration = await pushRegistration();
          const subscription = await registration.pushManager.getSubscription();
          if (subscription) {
            await postJSON(card.dataset.unsubscribeUrl, { endpoint: subscription.endpoint });
            await subscription.unsubscribe();
          }
          window.location.reload();
        } else if (action === "test") {
          const result = await postJSON(card.dataset.testUrl);
          showPushState(card, "on", result.message);
        }
      } catch (error) {
        showPushState(card, await currentPushState().catch(() => "unsupported"), card.dataset.errorText);
      } finally {
        button.disabled = false;
      }
    });
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
