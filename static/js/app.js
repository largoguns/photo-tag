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
    return String(value).normalize("NFKD").replace(/[\u0300-\u036f]/g, "").replace(/\s+/g, " ").trim().toLowerCase();
  }

  // Loose search: every typed word appears somewhere ("ari" → "María", "lop mar" → "María López").
  function looseMatch(query, text) {
    const haystack = nameKey(text);
    return nameKey(query).split(" ").filter(Boolean).every((word) => haystack.includes(word));
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

  // No page zoom (iOS ignores user-scalable=no); the app's own gestures zoom by themselves.
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

  // --- Event proposal: «Otro…» reveals the field for the new event (reviewed by an admin) --
  function syncEventForm(select) {
    const field = select.closest("form").querySelector("[data-event-name]");
    if (!field) return;
    const isNew = select.value === "new";
    field.hidden = !isNew;
    field.querySelector("input").required = isNew;
    if (isNew && document.activeElement === select) field.querySelector("input").focus();
  }

  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-event-type]")) syncEventForm(event.target);
  });

  // Without JS the field stays visible; with JS it only shows for «Otro…».
  function initEventForms(root) {
    root.querySelectorAll("[data-event-type]").forEach(syncEventForm);
  }
  ready(() => initEventForms(document));
  document.addEventListener("htmx:afterSwap", (event) => initEventForms(event.target));

  // Sheets marked data-autoopen (the person page after saving) open by themselves.
  function openAutoSheets() {
    document.querySelectorAll("dialog[data-autoopen]:not([open])").forEach((dialog) => {
      dialog.removeAttribute("data-autoopen");
      if (typeof dialog.showModal === "function") dialog.showModal();
    });
  }
  ready(openAutoSheets);
  document.addEventListener("htmx:afterSettle", openAutoSheets);

  // Relationship sheet: the activity field only for "activity mate" (visible without JS).
  function syncRelationForm(select) {
    const field = select.closest("form").querySelector("[data-relation-activity]");
    if (!field) return;
    const needed = select.value === "activity_mate";
    field.hidden = !needed;
    field.querySelector("input").required = needed;
  }
  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-relation-role]")) syncRelationForm(event.target);
  });
  function initRelationForms(root) {
    root.querySelectorAll("[data-relation-role]").forEach(syncRelationForm);
  }
  ready(() => initRelationForms(document));
  document.addEventListener("htmx:afterSwap", () => initRelationForms(document));

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

  // --- Autocomplete of persons and locations (accent-insensitive, prefers existing) ---
  // Persons: <input data-person-combobox>, data in #people-data ({name, shown, nicknames,
  // avatar}; `name` is the value sent). Locations: <input data-combobox="locations">,
  // data in #locations-data ({name}), kept as typed instead of normalized as a name.
  const COMBOBOX = "[data-person-combobox], [data-combobox]";
  const comboboxCache = {};
  function comboboxSource(input) { return input.dataset.combobox || "people"; }
  function comboboxItems(source) {
    if (comboboxCache[source]) return comboboxCache[source];
    const data = document.getElementById(`${source}-data`);
    let items = [];
    try { items = data ? JSON.parse(data.textContent) : []; } catch (error) { items = []; }
    items.forEach((item) => { item.key = nameKey(item.name); item.shownKey = nameKey(item.shown || item.name); });
    comboboxCache[source] = items;
    return items;
  }
  function comboboxNormalize(input, value) {
    return comboboxSource(input) === "people" ? normalizeName(value) : String(value).replace(/\s+/g, " ").trim();
  }
  // How an item matches: by its name (or shown name), by a nickname, or not at all.
  function comboboxMatch(query, item) {
    if (looseMatch(query, `${item.name} ${item.shown || ""}`)) return { item, hint: "" };
    const nickname = (item.nicknames || []).find((text) => looseMatch(query, text));
    return nickname ? { item, hint: nickname } : null;
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
    const source = comboboxSource(input);
    const items = comboboxItems(source);
    const startsWith = (m) => m.item.key.startsWith(query) || m.item.shownKey.startsWith(query);
    const matches = items
      .map((item) => comboboxMatch(query, item))
      .filter(Boolean)
      .sort((a, b) => Number(!startsWith(a)) - Number(!startsWith(b)) || a.item.shownKey.localeCompare(b.item.shownKey))
      .slice(0, 8);
    list.innerHTML = "";
    const add = (label, value, avatar, extraClass) => {
      const option = document.createElement("li");
      option.className = `combobox-option${extraClass ? " " + extraClass : ""}`;
      option.id = `${list.id}-${list.children.length}`;
      option.setAttribute("role", "option");
      option.dataset.value = value;
      if (avatar === null) {
        // Locations: no picture.
      } else if (avatar) {
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
      return option;
    };
    const avatars = source === "people";
    const texts = document.getElementById("combobox-texts");
    const text = (name, value) => (texts && texts.dataset[name] ? texts.dataset[name].replace("%s", value) : value);
    // Names repeat in a family: namesakes in the list show how many photos each one has.
    const shownCount = {};
    matches.forEach(({ item }) => { shownCount[item.shownKey] = (shownCount[item.shownKey] || 0) + 1; });
    matches.forEach(({ item, hint }) => {
      let label = (item.pet ? "🐾 " : "") + (item.shown || item.name) + (hint ? ` · ${hint}` : "");
      if (avatars && shownCount[item.shownKey] > 1) {
        label += ` · ${item.photos === 1 ? text("onePhoto", "1") : text("photos", String(item.photos || 0))}`;
      }
      const option = add(label, item.name, avatars ? item.avatar : null);
      if (item.id) option.dataset.personId = item.id;
    });
    const normalized = comboboxNormalize(input, input.value);
    const exists = normalized && items.some((item) => item.key === nameKey(normalized));
    if (normalized && !exists) {
      const template = document.getElementById("combobox-create-template");
      const label = template ? template.textContent.replace("%s", normalized) : normalized;
      add(label, normalized, avatars ? "" : null, "create");
    } else if (exists && avatars) {
      // Someone else with the same name: a new person on purpose.
      add(text("namesake", normalized), normalized, "", "create").dataset.newPerson = "1";
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

  // The person chosen travels as `person_id` (names repeat), or `new_person` for a namesake
  // created on purpose; typing again forgets both.
  function setPersonFields(input, personId, newPerson) {
    if (!input.form || comboboxSource(input) !== "people") return;
    [["person_id", personId], ["new_person", newPerson]].forEach(([name, value]) => {
      let field = input.form.querySelector(`input[type="hidden"][name="${name}"]`);
      if (!field && value) {
        field = document.createElement("input");
        field.type = "hidden"; field.name = name;
        input.form.appendChild(field);
      }
      if (field) field.value = value;
    });
  }

  function chooseOption(input, option) {
    input.value = option.dataset.value;
    setPersonFields(input, option.dataset.personId || "", option.dataset.newPerson || "");
    closeCombobox(input);
    input.focus();
  }

  document.addEventListener("input", (event) => {
    if (event.target.matches(COMBOBOX)) {
      setPersonFields(event.target, "", "");
      renderCombobox(event.target);
    }
  });

  document.addEventListener("keydown", (event) => {
    const input = event.target;
    if (!input.matches || !input.matches(COMBOBOX)) return;
    if (event.key === "ArrowDown") { event.preventDefault(); moveComboboxSelection(input, 1); }
    else if (event.key === "ArrowUp") { event.preventDefault(); moveComboboxSelection(input, -1); }
    else if (event.key === "Escape") closeCombobox(input);
    else if (event.key === "Enter") {
      const list = comboboxList(input);
      const selected = list && !list.hidden && list.querySelector('[aria-selected="true"]');
      if (selected) { event.preventDefault(); chooseOption(input, selected); }
    }
  });

  // Choose on the option's own click. Choosing on pointerdown closed the list at once and
  // the click then landed on whatever was underneath (a vote or reject button).
  // pointerdown only keeps the focus in the input, so the list stays open until then.
  document.addEventListener("pointerdown", (event) => {
    if (event.target.closest(".combobox-option")) event.preventDefault();
  });
  document.addEventListener("click", (event) => {
    const option = event.target.closest(".combobox-option");
    if (!option) return;
    event.preventDefault();
    event.stopPropagation();
    const input = document.querySelector(`[aria-controls="${option.parentElement.id}"]`);
    if (input) chooseOption(input, option);
  });

  document.addEventListener("focusout", (event) => {
    if (event.target.matches && event.target.matches(COMBOBOX)) {
      window.setTimeout(() => closeCombobox(event.target), 150);
    }
  });

  // Normalize the name before sending ("adri" → "Adri"); the server does it too.
  document.addEventListener("submit", (event) => {
    const inputs = event.target.querySelectorAll ? event.target.querySelectorAll(COMBOBOX) : [];
    inputs.forEach((input) => { input.value = comboboxNormalize(input, input.value); });
  }, true);

  // --- Gallery filter sheets ---------------------------------------------------------
  document.addEventListener("click", (event) => {
    const opener = event.target.closest("[data-open-sheet]");
    if (opener) {
      const sheet = document.getElementById(opener.dataset.openSheet);
      // Opened from a menu (the person's ⋮): the menu closes behind the sheet.
      const menu = opener.closest("details[open]");
      if (menu) menu.removeAttribute("open");
      if (sheet && typeof sheet.showModal === "function") sheet.showModal();
      // «+ Apodo»: straight to the field.
      const field = opener.dataset.sheetFocus && document.getElementById(opener.dataset.sheetFocus);
      if (field) field.focus();
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
    event.target.closest("dialog, [data-search-scope]").querySelectorAll("[data-option-label]").forEach((item) => {
      item.hidden = !!query && !looseMatch(query, item.dataset.optionLabel);
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

  // --- Gallery: blocks loaded near the screen and freed far from it ----------------------
  // Like Google Photos: the page only has the skeleton (one empty section per block with
  // the sum of its aspect ratios); blocks within ~2 screens are fetched and those beyond
  // ~4 screens are emptied, keeping their measured height so the scroll never jumps.
  let gallery = null;
  const GALLERY_ANCHOR_KEY = "phototag:gallery-anchor";

  function galleryBase(href) {
    const url = new URL(href, window.location.href);
    url.searchParams.delete("anchor");
    return url.pathname + url.search;
  }

  function initGallery(root) {
    if (!root || root.dataset.ready) return;
    root.dataset.ready = "1";
    const blocks = [...root.querySelectorAll(".gallery-block")];
    if (!blocks.length) { gallery = null; return; }
    const GAP = 3, MAX_IN_FLIGHT = 2;
    const queue = new Set();
    let inFlight = 0;
    let factor = 1; // learnt from loaded blocks: real height / estimate
    const samples = [];
    const viewport = () => window.innerHeight || 800;
    const isLoaded = (block) => block.hasAttribute("data-loaded");

    function estimate(block) {
      const width = root.clientWidth || 360;
      const row = currentRowHeight();
      const rows = Math.max(1, Math.ceil(((Number(block.dataset.ratio) || 1) * row) / width));
      return Math.round(rows * (row + GAP) * factor);
    }

    function reserve(block) {
      if (isLoaded(block)) { block.style.minHeight = ""; return; }
      block.style.minHeight = `${block.dataset.measured ? Number(block.dataset.measured) : estimate(block)}px`;
    }

    // The first block that starts on screen (below the top bar): whatever
    // grows above it, including a block half hidden at the top, is compensated.
    function screenAnchor() {
      const line = 80;
      // Blocks only: section titles are sticky and stay put while things move.
      return blocks.find((block) => block.getBoundingClientRect().top >= line)
        || blocks.find((block) => block.getBoundingClientRect().bottom > line) || null;
    }

    function distance(block) {
      const rect = block.getBoundingClientRect();
      if (rect.bottom < 0) return -rect.bottom;
      if (rect.top > viewport()) return rect.top - viewport();
      return 0;
    }

    function pump() {
      while (inFlight < MAX_IN_FLIGHT && queue.size) {
        const next = [...queue].sort((a, b) => distance(a) - distance(b))[0];
        queue.delete(next);
        if (!next.isConnected || isLoaded(next) || distance(next) > viewport() * 3) continue;
        load(next);
      }
    }

    function load(block) {
      if (isLoaded(block) || block.dataset.loading) return;
      block.dataset.loading = "1";
      inFlight += 1;
      fetch(root.dataset.blockUrl + block.dataset.block, { headers: { "HX-Request": "true" }, credentials: "same-origin" })
        .then((response) => (response.ok ? response.text() : Promise.reject(response.status)))
        .then((html) => {
          // Keep what is on screen in place while a block above it grows or shrinks.
          const anchor = screenAnchor();
          const anchorTop = anchor ? anchor.getBoundingClientRect().top : 0;
          const estimated = block.dataset.measured ? 0 : block.offsetHeight / factor;
          block.innerHTML = html;
          block.setAttribute("data-loaded", "");
          block.style.minHeight = "";
          if (anchor && anchor !== block) window.scrollBy(0, anchor.getBoundingClientRect().top - anchorTop);
          if (estimated) learn(block.offsetHeight / estimated);
          if (distance(block) > viewport() * 4) unload(block); // went far while loading
          renderSelection();
          scheduleRail();
          settleJump();
        })
        .catch(() => {})
        .finally(() => {
          delete block.dataset.loading;
          inFlight -= 1;
          pump();
        });
    }

    // Better estimates for blocks not loaded yet, only below the screen: nothing visible moves.
    function learn(ratio) {
      samples.push(ratio);
      if (samples.length > 12) samples.shift();
      factor = samples.reduce((a, b) => a + b, 0) / samples.length;
      blocks.forEach((block) => {
        if (!isLoaded(block) && !block.dataset.measured && block.getBoundingClientRect().top > viewport()) reserve(block);
      });
    }

    // After a fast scroll: free what ended up far and fetch what ended up near (the
    // observers only report edge crossings, and the queue skipped blocks passed by).
    function sweep() {
      blocks.forEach((block) => {
        if (isLoaded(block) && distance(block) > viewport() * 4) unload(block);
        else if (!isLoaded(block) && distance(block) <= viewport() * 2) queue.add(block);
      });
      pump();
    }
    let sweepTimer = null;
    window.addEventListener("scroll", () => {
      window.clearTimeout(sweepTimer);
      sweepTimer = window.setTimeout(() => { if (root.isConnected) sweep(); }, 250);
    }, { passive: true });

    function unload(block) {
      if (!isLoaded(block)) return;
      const height = block.offsetHeight;
      block.dataset.measured = String(height);
      block.style.minHeight = `${height}px`;
      block.innerHTML = ""; // the images go: the browser frees their memory
      block.removeAttribute("data-loaded");
    }

    // Placeholders follow the width and the row height; keep the top item in place.
    function relayout(keepAnchor = true) {
      const probe = keepAnchor && document.elementFromPoint(root.getBoundingClientRect().left + 10, 90);
      const anchor = probe && probe.closest ? probe.closest(".gallery-block, .gallery-section-title") : null;
      const before = anchor ? anchor.getBoundingClientRect().top : 0;
      blocks.forEach((block) => { delete block.dataset.measured; reserve(block); });
      if (anchor) window.scrollBy(0, anchor.getBoundingClientRect().top - before);
      scheduleRail();
    }

    blocks.forEach(reserve);
    const near = new IntersectionObserver((entries) => {
      entries.forEach((entry) => { if (entry.isIntersecting) queue.add(entry.target); else queue.delete(entry.target); });
      pump();
    }, { rootMargin: "200% 0px" });
    const far = new IntersectionObserver((entries) => {
      entries.forEach((entry) => { if (!entry.isIntersecting) unload(entry.target); });
    }, { rootMargin: "400% 0px" });
    blocks.forEach((block) => { near.observe(block); far.observe(block); });

    let lastWidth = root.clientWidth;
    let resizeTimer = null;
    window.addEventListener("resize", () => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => {
        if (!root.isConnected || root.clientWidth === lastWidth) return;
        lastWidth = root.clientWidth;
        relayout();
      }, 150);
    });

    // Back from a photo: the page came with that block (?anchor=i); put it where it was.
    const saved = (() => { try { return JSON.parse(sessionStorage.getItem(GALLERY_ANCHOR_KEY) || "null"); } catch (error) { return null; } })();
    if (saved && saved.base === galleryBase(window.location.href)) {
      if ("scrollRestoration" in history) history.scrollRestoration = "manual";
      const item = root.querySelector(`.jitem[data-photo-id="${saved.photo}"]`);
      if (item) window.requestAnimationFrame(() => window.scrollBy(0, item.getBoundingClientRect().top - saved.top));
    }

    gallery = { relayout, blocks, root };
    scheduleRail();
  }

  // Remember where a photo was opened from (block and position on screen).
  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-gallery] .jitem a");
    if (!link || document.body.classList.contains("selecting")) return;
    const item = link.closest(".jitem");
    const block = link.closest(".gallery-block");
    const url = new URL(window.location.href);
    url.searchParams.set("anchor", block.dataset.block);
    history.replaceState(history.state, "", url.pathname + url.search);
    try {
      sessionStorage.setItem(GALLERY_ANCHOR_KEY, JSON.stringify({
        base: galleryBase(url.href), photo: item.dataset.photoId, top: item.getBoundingClientRect().top,
      }));
    } catch (error) { /* private mode: no restore */ }
  });

  // --- Gallery: year rail to jump anywhere (Google Photos style fast scroller) --------------
  let railFrame = null;
  let railHideTimer = null;

  function scheduleRail() {
    if (railFrame) return;
    railFrame = window.requestAnimationFrame(() => { railFrame = null; positionRail(); });
  }

  function railParts() {
    const rail = document.querySelector("[data-year-rail]");
    return rail ? { rail, thumb: rail.querySelector(".year-rail-thumb"), bubble: rail.querySelector(".year-rail-bubble") } : null;
  }

  function scrollRange() {
    return Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
  }

  // Where a section starts on the page. Titles are sticky (a stuck title reports where it
  // is stuck), so measure the section's first block instead.
  function sectionTop(title) {
    const first = title.nextElementSibling || title;
    return first.getBoundingClientRect().top + window.scrollY - title.offsetHeight;
  }

  function sectionAt(y) {
    let current = null;
    document.querySelectorAll(".gallery-section-title").forEach((title) => {
      if (sectionTop(title) <= y + 100) current = title;
    });
    return current || document.querySelector(".gallery-section-title");
  }

  // Place each year on the rail where its section starts; hide labels that would overlap.
  function positionRail() {
    const parts = railParts();
    if (!parts) return;
    const height = parts.rail.clientHeight;
    let lastTop = -Infinity;
    parts.rail.querySelectorAll("[data-rail-section]").forEach((link) => {
      const title = document.getElementById(`section-${link.dataset.railSection}`);
      if (!title) return;
      const top = Math.min(1, sectionTop(title) / scrollRange()) * height; // same scale as dragging
      link.parentElement.style.top = `${top}px`;
      const visible = top - lastTop >= 22;
      link.parentElement.classList.toggle("crowded", !visible);
      if (visible) lastTop = top;
    });
    updateRailThumb(parts);
  }

  function updateRailThumb(parts) {
    const fraction = Math.min(1, window.scrollY / scrollRange());
    parts.thumb.style.top = `${fraction * (parts.rail.clientHeight - parts.thumb.offsetHeight)}px`;
    // The year you are in stays highlighted in the list (desktop hover).
    const current = sectionAt(window.scrollY);
    const key = current ? current.dataset.section : "";
    parts.rail.querySelectorAll("[data-rail-section]").forEach((link) => {
      link.toggleAttribute("aria-current", link.dataset.railSection === key);
    });
  }

  function showRail(parts, label) {
    parts.rail.classList.add("active");
    if (label) { parts.bubble.hidden = false; parts.bubble.textContent = label; parts.bubble.style.top = parts.thumb.style.top; }
    window.clearTimeout(railHideTimer);
    railHideTimer = window.setTimeout(() => { parts.rail.classList.remove("active"); parts.bubble.hidden = true; }, 1500);
  }

  window.addEventListener("scroll", () => {
    const parts = railParts();
    if (!parts) return;
    updateRailThumb(parts);
    const title = sectionAt(window.scrollY);
    showRail(parts, title ? title.textContent.trim() : "");
  }, { passive: true });

  // A tap on a year jumps to it; moving the pointer drags along the whole gallery.
  let railDrag = null;
  document.addEventListener("pointerdown", (event) => {
    const rail = event.target.closest("[data-year-rail]");
    if (!rail) return;
    railDrag = { rail, startY: event.clientY, moved: false, link: event.target.closest("[data-rail-section]") };
    rail.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  function dragRail(event) {
    const box = railDrag.rail.getBoundingClientRect();
    const fraction = Math.min(1, Math.max(0, (event.clientY - box.top) / box.height));
    window.scrollTo(0, fraction * scrollRange());
    railDrag.rail.classList.add("dragging");
  }
  // After a jump the blocks around load and their real heights replace the estimates;
  // keep the chosen year at the top until things settle or the user scrolls again.
  let jumpTarget = null;
  function settleJump() {
    if (!jumpTarget) return;
    if (Date.now() > jumpTarget.until || !jumpTarget.title.isConnected) { jumpTarget = null; return; }
    const top = sectionTop(jumpTarget.title) - 64;
    if (Math.abs(window.scrollY - top) > 2) window.scrollTo(0, top);
  }
  function jumpToSection(key) {
    const title = document.getElementById(`section-${key}`);
    if (!title) return;
    jumpTarget = { title, until: Date.now() + 2500 };
    settleJump();
  }
  ["wheel", "touchmove", "keydown"].forEach((type) => window.addEventListener(type, () => { jumpTarget = null; }, { passive: true }));
  document.addEventListener("pointermove", (event) => {
    if (!railDrag) return;
    if (!railDrag.moved && Math.abs(event.clientY - railDrag.startY) < 6) return;
    railDrag.moved = true;
    dragRail(event);
  });
  ["pointerup", "pointercancel"].forEach((type) => document.addEventListener(type, (event) => {
    if (!railDrag) return;
    if (type === "pointerup" && !railDrag.moved) {
      if (railDrag.link) jumpToSection(railDrag.link.dataset.railSection);
      else dragRail(event); // a tap on the bar itself
    }
    railDrag.rail.classList.remove("dragging");
    railDrag = null;
  }));
  // Keyboards (and the links' own click) still work: jump by script, not by anchor.
  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-rail-section]");
    if (!link) return;
    event.preventDefault();
    if (event.detail === 0) jumpToSection(link.dataset.railSection); // Enter key
  });

  // Hovering the rail (desktop): the list of years shows, the current one highlighted.
  document.addEventListener("pointerover", (event) => {
    const rail = event.target.closest && event.target.closest("[data-year-rail]");
    if (rail) { const parts = railParts(); parts.bubble.hidden = true; updateRailThumb(parts); }
  });

  ready(() => initGallery(document.querySelector("[data-gallery]")));
  document.addEventListener("htmx:afterSettle", () => initGallery(document.querySelector("[data-gallery]")));
  window.addEventListener("resize", scheduleRail);

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

  // Long press on a photo starts selecting (like Google Photos). Only with one finger:
  // a second finger means a pinch, which must not select.
  const pressPointers = new Set();
  function cancelPress() {
    if (pressTimer) { window.clearTimeout(pressTimer); pressTimer = null; }
  }
  document.addEventListener("pointerdown", (event) => {
    if (event.isPrimary) pressPointers.clear(); // no other finger is down
    pressPointers.add(event.pointerId);
    if (pressPointers.size > 1) { cancelPress(); return; }
    const item = event.target.closest(".jitem[data-photo-id]");
    if (!item || document.body.classList.contains("selecting")) return;
    pressStart = { x: event.clientX, y: event.clientY };
    pressTimer = window.setTimeout(() => {
      pressTimer = null;
      if (pressPointers.size !== 1) return;
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
  ["pointerup", "pointercancel"].forEach((type) => document.addEventListener(type, (event) => {
    pressPointers.delete(event.pointerId);
    cancelPress();
  }));
  document.addEventListener("gesturestart", cancelPress); // iOS pinch
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
    if (gallery) gallery.relayout(false); // placeholders follow the new row height
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

  // «Instalar la app» in the avatar menu: always reachable, even after «Ahora no».
  function installMode() {
    if (isInstalled()) return null;
    if (deferredInstall) return "prompt";
    return mobilePlatform();
  }

  function syncInstallMenu() {
    const item = document.querySelector("[data-install-open]");
    if (item) item.hidden = !installMode();
  }

  function promptInstall() {
    const prompt = deferredInstall;
    deferredInstall = null;
    prompt.prompt();
    prompt.userChoice.then((choice) => {
      hideInstallBanner(choice.outcome !== "accepted");
      syncInstallMenu();
    });
  }

  // Chromium fires this when the app is installable; keep it to prompt later.
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredInstall = event;
    syncInstallMenu();
    if (mobilePlatform() && !isInstalled() && !recentlyDismissed(INSTALL_DISMISS_KEY)) showInstallBanner("prompt");
  });

  window.addEventListener("appinstalled", () => {
    deferredInstall = null;
    hideInstallBanner(false);
    syncInstallMenu();
  });

  document.addEventListener("click", (event) => {
    const opener = event.target.closest("[data-install-open]");
    if (opener) {
      opener.closest("details")?.removeAttribute("open");
      const mode = installMode();
      if (mode === "prompt") promptInstall();
      else if (mode) showInstallBanner(mode); // the steps, even if dismissed before
      return;
    }
    const action = event.target.closest("[data-install-action]");
    if (!action) return;
    if (action.dataset.installAction === "dismiss") {
      hideInstallBanner(true);
      return;
    }
    if (deferredInstall) promptInstall();
  });

  ready(() => {
    syncInstallMenu();
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

  // --- Crop editor: move the edges over the original scan and straighten -------------
  // Same geometry as scanner/services/recrop.py: view = photo as shown (already turned);
  // scan point = centre + R(angle)·(u, v) with (u, v) relative to the crop centre.
  // Perspective mode instead shows the whole original (turned like the photo) with four
  // free corners, kept in scan pixels and in on-screen order (TL, TR, BR, BL).
  function initCropEditor(root) {
    const data = JSON.parse(document.getElementById("crop-data").textContent);
    const canvas = root.querySelector("[data-crop-canvas]");
    const image = root.querySelector("[data-crop-preview]");
    const slider = root.querySelector("[data-crop-angle]");
    const angleOut = root.querySelector("[data-crop-angle-value]");
    const outside = root.querySelector("[data-crop-outside]");
    const save = root.querySelector("[data-crop-save]");
    const form = root.querySelector("form");
    const ctx = canvas.getContext("2d");
    const W = data.width, H = data.height, r = ((data.rotation % 360) + 360) % 360;
    const Wu = r % 180 ? H : W, Hu = r % 180 ? W : H;
    const margin = 0.25 * Math.max(W, H);
    const HANDLE = 22; // px on screen
    let box = { x0: 0, y0: 0, x1: W, y1: H };
    let base = { ...box }; // box chosen by hand; straightening shrinks a copy of it
    let view = { k: 1, ox: 0, oy: 0 };
    let drag = null;
    // Zoom on top of the fitted view: screen = centre + (fitted - centre) * zoom + pan.
    let zoom = 1, pan = [0, 0], panning = null, pinch = null;
    const pointers = new Map();
    const MAX_ZOOM = 8;
    const SW = data.scanWidth || image.naturalWidth, SH = data.scanHeight || image.naturalHeight;
    const TW = r % 180 ? SH : SW, TH = r % 180 ? SW : SH; // the original, turned
    const initialQuad = data.quad ? data.quad.map((p) => [...p]) : [[0, 0], [SW, 0], [SW, SH], [0, SH]];
    let quad = initialQuad.map((p) => [...p]);
    let full = { k: 1, ox: 0, oy: 0 };
    let mode = root.dataset.mode === "quad" ? "quad" : "box";
    const badQuad = root.querySelector("[data-crop-bad-quad]");
    const detectNone = root.querySelector("[data-crop-detect-none]");

    const angle = () => data.angle + Number(slider.value);
    const rad = (deg) => (deg * Math.PI) / 180;

    function unturn(x, y) {
      if (r === 90) return [y, W - x];
      if (r === 180) return [W - x, H - y];
      if (r === 270) return [H - y, x];
      return [x, y];
    }

    function toScan(x, y, deg) {
      const [ux, uy] = unturn(x, y);
      const u = ux + data.inset - data.w / 2, v = uy + data.inset - data.h / 2;
      const a = rad(deg);
      return [data.cx + u * Math.cos(a) - v * Math.sin(a), data.cy + u * Math.sin(a) + v * Math.cos(a)];
    }

    function valid(b, deg) {
      if (b.x1 - b.x0 < data.minSide || b.y1 - b.y0 < data.minSide) return false;
      // Stay within what the canvas shows, so every edge can still be grabbed.
      const { k, ox, oy, width, height } = view;
      if (b.x0 < -ox / k || b.y0 < -oy / k || b.x1 > (width - ox) / k || b.y1 > (height - oy) / k) return false;
      if (!data.scanWidth) return true;
      return [[b.x0, b.y0], [b.x1, b.y0], [b.x1, b.y1], [b.x0, b.y1]].every(([x, y]) => {
        const [sx, sy] = toScan(x, y, deg);
        return sx >= -1 && sy >= -1 && sx <= data.scanWidth + 1 && sy <= data.scanHeight + 1;
      });
    }

    function resize() {
      const dpr = window.devicePixelRatio || 1;
      const width = canvas.clientWidth, height = canvas.clientHeight;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      const k = Math.min(width / (W + 2 * margin), height / (H + 2 * margin));
      view = { k, ox: (width - k * W) / 2, oy: (height - k * H) / 2, dpr, width, height };
      const pad = 16;
      const kf = Math.min((width - 2 * pad) / TW, (height - 2 * pad) / TH);
      full = { k: kf, ox: (width - kf * TW) / 2, oy: (height - kf * TH) / 2 };
      draw();
    }

    // --- Perspective mode -----------------------------------------------------------
    function turnScan(X, Y) {
      if (r === 90) return [SH - Y, X];
      if (r === 180) return [SW - X, SH - Y];
      if (r === 270) return [Y, SW - X];
      return [X, Y];
    }

    function unturnScan(tx, ty) {
      if (r === 90) return [ty, SH - tx];
      if (r === 180) return [SW - tx, SH - ty];
      if (r === 270) return [SW - ty, tx];
      return [tx, ty];
    }

    function eff(fitted) {
      const cx = view.width / 2, cy = view.height / 2;
      return { k: fitted.k * zoom, ox: cx + (fitted.ox - cx) * zoom + pan[0], oy: cy + (fitted.oy - cy) * zoom + pan[1] };
    }

    function zoomAt(factor, px, py) {
      const next = Math.max(1, Math.min(MAX_ZOOM, zoom * factor));
      const cx = view.width / 2, cy = view.height / 2;
      pan = next === 1 ? [0, 0] : [
        px - cx - (px - cx - pan[0]) * (next / zoom),
        py - cy - (py - cy - pan[1]) * (next / zoom),
      ];
      zoom = next;
      draw();
    }

    const toCanvas = ([X, Y]) => {
      const [tx, ty] = turnScan(X, Y), f = eff(full);
      return [f.ox + tx * f.k, f.oy + ty * f.k];
    };
    const fromCanvas = (cx, cy) => { const f = eff(full); return unturnScan((cx - f.ox) / f.k, (cy - f.oy) / f.k); };

    function drawOriginal(transform) {
      ctx.save();
      transform();
      if (r === 90) { ctx.translate(SH, 0); ctx.rotate(Math.PI / 2); }
      else if (r === 180) { ctx.translate(SW, SH); ctx.rotate(Math.PI); }
      else if (r === 270) { ctx.translate(0, SW); ctx.rotate(-Math.PI / 2); }
      const s = image.naturalWidth / SW;
      ctx.scale(1 / s, 1 / s);
      ctx.drawImage(image, 0, 0);
      ctx.restore();
    }

    function validQuad(points) {
      for (let i = 0; i < 4; i++) {
        const [ax, ay] = points[i], [bx, by] = points[(i + 1) % 4], [cx, cy] = points[(i + 2) % 4];
        if ((bx - ax) * (cy - ay) - (by - ay) * (cx - ax) <= 0) return false; // convex, clockwise
        if (Math.hypot(bx - ax, by - ay) < data.minSide) return false;
      }
      return points.every(([x, y]) => x >= -1 && y >= -1 && x <= SW + 1 && y <= SH + 1);
    }

    function drawQuad() {
      const { dpr, width, height } = view;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      drawOriginal(() => { const f = eff(full); ctx.translate(f.ox, f.oy); ctx.scale(f.k, f.k); });
      const corners = quad.map(toCanvas);
      ctx.fillStyle = "rgba(0, 0, 0, 0.55)";
      ctx.beginPath();
      ctx.rect(0, 0, width, height);
      corners.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.closePath();
      ctx.fill("evenodd");
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 2;
      ctx.beginPath();
      corners.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.closePath();
      ctx.stroke();
      ctx.fillStyle = "#fff";
      for (const [x, y] of corners) { ctx.beginPath(); ctx.arc(x, y, 9, 0, 2 * Math.PI); ctx.fill(); }
      if (drag && drag.corner !== undefined) drawLoupe(corners[drag.corner]);
    }

    // Magnified view around the corner being dragged, away from the finger.
    function drawLoupe([px, py]) {
      const radius = 64, zoom = 3;
      const lx = px < view.width / 2 ? view.width - radius - 12 : radius + 12, ly = radius + 12;
      ctx.save();
      ctx.beginPath();
      ctx.arc(lx, ly, radius, 0, 2 * Math.PI);
      ctx.clip();
      ctx.fillStyle = "#111";
      ctx.fillRect(lx - radius, ly - radius, 2 * radius, 2 * radius);
      drawOriginal(() => {
        ctx.translate(lx, ly); ctx.scale(zoom, zoom); ctx.translate(-px, -py);
        const f = eff(full);
        ctx.translate(f.ox, f.oy); ctx.scale(f.k, f.k);
      });
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(lx - 10, ly); ctx.lineTo(lx + 10, ly); ctx.moveTo(lx, ly - 10); ctx.lineTo(lx, ly + 10);
      ctx.stroke();
      ctx.restore();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(lx, ly, radius, 0, 2 * Math.PI); ctx.stroke();
    }

    function hitCorner(cx, cy) {
      let best = -1, distance = HANDLE * 1.5;
      quad.map(toCanvas).forEach(([x, y], i) => {
        const d = Math.hypot(x - cx, y - cy);
        if (d < distance) { best = i; distance = d; }
      });
      return best;
    }

    function draw() { if (mode === "quad") drawQuad(); else drawBox(); }

    function drawBox() {
      const { dpr, width, height } = view;
      const { k, ox, oy } = eff(view);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      ctx.save();
      ctx.translate(ox, oy);
      ctx.scale(k, k);
      if (r === 90) { ctx.translate(Hu, 0); ctx.rotate(Math.PI / 2); }
      else if (r === 180) { ctx.translate(Wu, Hu); ctx.rotate(Math.PI); }
      else if (r === 270) { ctx.translate(0, Wu); ctx.rotate(-Math.PI / 2); }
      ctx.translate(data.w / 2 - data.inset, data.h / 2 - data.inset);
      ctx.rotate(-rad(angle()));
      ctx.translate(-data.cx, -data.cy);
      const s = image.naturalWidth / (data.scanWidth || image.naturalWidth);
      ctx.scale(1 / s, 1 / s);
      ctx.drawImage(image, 0, 0);
      ctx.restore();

      // Dim everything outside the crop, then the frame, thirds and handles.
      const x = ox + box.x0 * k, y = oy + box.y0 * k;
      const w = (box.x1 - box.x0) * k, h = (box.y1 - box.y0) * k;
      ctx.fillStyle = "rgba(0, 0, 0, 0.55)";
      ctx.beginPath();
      ctx.rect(0, 0, width, height);
      ctx.rect(x, y, w, h);
      ctx.fill("evenodd");
      ctx.strokeStyle = "rgba(255, 255, 255, 0.45)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (const f of [1 / 3, 2 / 3]) {
        ctx.moveTo(x + w * f, y); ctx.lineTo(x + w * f, y + h);
        ctx.moveTo(x, y + h * f); ctx.lineTo(x + w, y + h * f);
      }
      ctx.stroke();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 2;
      ctx.strokeRect(x, y, w, h);
      ctx.fillStyle = "#fff";
      for (const [hx, hy] of [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]) {
        ctx.beginPath(); ctx.arc(hx, hy, 7, 0, 2 * Math.PI); ctx.fill();
      }
      for (const [hx, hy, horizontal] of [[x + w / 2, y, true], [x + w / 2, y + h, true], [x, y + h / 2, false], [x + w, y + h / 2, false]]) {
        if (horizontal) ctx.fillRect(hx - 14, hy - 3, 28, 6);
        else ctx.fillRect(hx - 3, hy - 14, 6, 28);
      }
    }

    // Largest copy of the hand-picked box, scaled about its centre, that fits the scan.
    function fitToAngle() {
      if (valid(base, angle())) { box = { ...base }; return; }
      const mx = (base.x0 + base.x1) / 2, my = (base.y0 + base.y1) / 2;
      const scaled = (f) => ({
        x0: mx - (mx - base.x0) * f, x1: mx + (base.x1 - mx) * f,
        y0: my - (my - base.y0) * f, y1: my + (base.y1 - my) * f,
      });
      let lo = 0, hi = 1;
      for (let i = 0; i < 14; i++) {
        const mid = (lo + hi) / 2;
        if (valid(scaled(mid), angle())) lo = mid; else hi = mid;
      }
      if (valid(scaled(lo), angle())) box = scaled(lo);
    }

    function sync() {
      for (const el of root.querySelectorAll("[data-crop-for]")) el.hidden = el.dataset.cropFor !== mode;
      if (mode === "quad") {
        const ok = validQuad(quad);
        outside.hidden = true;
        badQuad.hidden = ok;
        save.disabled = !ok;
        draw();
        return;
      }
      badQuad.hidden = true;
      const ok = valid(box, angle());
      outside.hidden = ok;
      save.disabled = !ok;
      const delta = Number(slider.value);
      angleOut.textContent = `${delta.toLocaleString(document.documentElement.lang || undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}°`;
      draw();
    }

    function pointerView(event) {
      const rect = canvas.getBoundingClientRect();
      const v = eff(view);
      return [(event.clientX - rect.left - v.ox) / v.k, (event.clientY - rect.top - v.oy) / v.k];
    }

    function hit(x, y) {
      const tol = HANDLE / eff(view).k;
      const inY = y > box.y0 - tol && y < box.y1 + tol, inX = x > box.x0 - tol && x < box.x1 + tol;
      const edges = {
        x0: inY && Math.abs(x - box.x0) < tol,
        x1: inY && Math.abs(x - box.x1) < tol,
        y0: inX && Math.abs(y - box.y0) < tol,
        y1: inX && Math.abs(y - box.y1) < tol,
      };
      if (edges.x0 && edges.x1) edges[Math.abs(x - box.x0) < Math.abs(x - box.x1) ? "x1" : "x0"] = false;
      if (edges.y0 && edges.y1) edges[Math.abs(y - box.y0) < Math.abs(y - box.y1) ? "y1" : "y0"] = false;
      if (Object.values(edges).some(Boolean)) return edges;
      if (x > box.x0 && x < box.x1 && y > box.y0 && y < box.y1) return { move: true };
      return null;
    }

    function moved(start, edges, dx, dy) {
      const b = { ...start };
      if (edges.move) { b.x0 += dx; b.x1 += dx; b.y0 += dy; b.y1 += dy; return b; }
      if (edges.x0) b.x0 = Math.min(start.x0 + dx, start.x1 - data.minSide);
      if (edges.x1) b.x1 = Math.max(start.x1 + dx, start.x0 + data.minSide);
      if (edges.y0) b.y0 = Math.min(start.y0 + dy, start.y1 - data.minSide);
      if (edges.y1) b.y1 = Math.max(start.y1 + dy, start.y0 + data.minSide);
      return b;
    }

    function canvasPoint(event) {
      const rect = canvas.getBoundingClientRect();
      return [event.clientX - rect.left, event.clientY - rect.top];
    }

    function startPan(event) {
      if (zoom <= 1) return;
      panning = { x: event.clientX, y: event.clientY, pan: [...pan] };
      canvas.setPointerCapture(event.pointerId);
      canvas.style.cursor = "grabbing";
    }

    canvas.addEventListener("pointerdown", (event) => {
      pointers.set(event.pointerId, [event.clientX, event.clientY]);
      if (pointers.size === 2) { // two fingers: zoom, whatever was being dragged
        const [a, b] = [...pointers.values()];
        pinch = { distance: Math.hypot(a[0] - b[0], a[1] - b[1]), zoom };
        drag = null;
        panning = null;
        return;
      }
      if (mode === "quad") {
        const corner = hitCorner(...canvasPoint(event));
        if (corner < 0) { startPan(event); return; }
        drag = { corner };
        canvas.setPointerCapture(event.pointerId);
        event.preventDefault();
        draw();
        return;
      }
      const [x, y] = pointerView(event);
      const edges = hit(x, y);
      if (!edges) { startPan(event); return; }
      drag = { edges, x, y, start: { ...box } };
      canvas.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    canvas.addEventListener("pointermove", (event) => {
      if (pointers.has(event.pointerId)) pointers.set(event.pointerId, [event.clientX, event.clientY]);
      if (pinch && pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        const rect = canvas.getBoundingClientRect();
        const distance = Math.hypot(a[0] - b[0], a[1] - b[1]);
        zoomAt((pinch.zoom * distance) / pinch.distance / zoom, (a[0] + b[0]) / 2 - rect.left, (a[1] + b[1]) / 2 - rect.top);
        return;
      }
      if (panning) {
        pan = [panning.pan[0] + event.clientX - panning.x, panning.pan[1] + event.clientY - panning.y];
        draw();
        return;
      }
      if (mode === "quad") {
        if (!drag) {
          canvas.style.cursor = hitCorner(...canvasPoint(event)) >= 0 ? "grab" : zoom > 1 ? "move" : "default";
          return;
        }
        const [X, Y] = fromCanvas(...canvasPoint(event));
        quad[drag.corner] = [Math.max(0, Math.min(SW, X)), Math.max(0, Math.min(SH, Y))];
        sync();
        return;
      }
      if (!drag) {
        const edges = hit(...pointerView(event));
        const cursor = !edges ? "default" : edges.move ? "move"
          : (edges.x0 && edges.y0) || (edges.x1 && edges.y1) ? "nwse-resize"
          : (edges.x1 && edges.y0) || (edges.x0 && edges.y1) ? "nesw-resize"
          : edges.x0 || edges.x1 ? "ew-resize" : "ns-resize";
        canvas.style.cursor = cursor;
        return;
      }
      const [x, y] = pointerView(event);
      const dx = x - drag.x, dy = y - drag.y;
      // Go as far as possible towards the pointer without leaving the scan.
      let lo = 0, hi = 1;
      if (valid(moved(drag.start, drag.edges, dx, dy), angle())) lo = 1;
      else for (let i = 0; i < 10; i++) {
        const mid = (lo + hi) / 2;
        if (valid(moved(drag.start, drag.edges, dx * mid, dy * mid), angle())) lo = mid; else hi = mid;
      }
      const candidate = moved(drag.start, drag.edges, dx * lo, dy * lo);
      if (valid(candidate, angle()) || !valid(drag.start, angle())) box = candidate;
      sync();
    });
    const endDrag = (event) => {
      pointers.delete(event.pointerId);
      if (pointers.size < 2) pinch = null;
      panning = null;
      if (drag && mode === "box") base = { ...box };
      drag = null;
      draw();
    };
    canvas.addEventListener("wheel", (event) => {
      event.preventDefault();
      const [x, y] = canvasPoint(event);
      zoomAt(event.deltaY < 0 ? 1.15 : 1 / 1.15, x, y);
    }, { passive: false });
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);

    slider.addEventListener("input", () => { fitToAngle(); sync(); });
    root.addEventListener("click", (event) => {
      const step = event.target.closest("[data-crop-step]");
      if (step) {
        const value = Number(slider.value) + Number(step.dataset.cropStep);
        slider.value = Math.max(Number(slider.min), Math.min(Number(slider.max), value)).toFixed(1);
        fitToAngle();
        sync();
      }
      if (event.target.closest("[data-crop-reset]")) {
        box = { x0: 0, y0: 0, x1: W, y1: H };
        base = { ...box };
        slider.value = "0";
        quad = initialQuad.map((p) => [...p]);
        sync();
      }
      const zoomButton = event.target.closest("[data-crop-zoom]");
      if (zoomButton) {
        const action = zoomButton.dataset.cropZoom;
        if (action === "fit") { zoom = 1; pan = [0, 0]; draw(); }
        else zoomAt(action === "in" ? 1.5 : 1 / 1.5, view.width / 2, view.height / 2);
      }
      const detect = event.target.closest("[data-crop-detect]");
      if (detect) {
        detect.disabled = true;
        detectNone.hidden = true;
        fetch(data.detectUrl, { headers: { Accept: "application/json" } })
          .then((response) => response.json())
          .then((result) => {
            if (result.quad) quad = result.quad;
            else detectNone.hidden = false;
            sync();
          })
          .catch(() => { detectNone.hidden = false; })
          .finally(() => { detect.disabled = false; });
      }
    });
    root.addEventListener("change", (event) => {
      if (!event.target.matches("[data-crop-mode]")) return;
      mode = event.target.value;
      zoom = 1;
      pan = [0, 0];
      drag = null;
      sync();
    });
    form.addEventListener("submit", () => {
      form.querySelector('[data-crop-field="mode"]').value = mode;
      quad.forEach(([x, y], i) => {
        form.querySelector(`[data-crop-field="q${i}x"]`).value = x.toFixed(1);
        form.querySelector(`[data-crop-field="q${i}y"]`).value = y.toFixed(1);
      });
      for (const key of ["x0", "y0", "x1", "y1"]) {
        form.querySelector(`[data-crop-field="${key}"]`).value = box[key].toFixed(1);
      }
      form.querySelector('[data-crop-field="angle"]').value = angle().toFixed(2);
    });
    window.addEventListener("resize", resize);
    resize();
    sync();
  }

  // --- Profile: on wide windows every section is open, with an index that follows -----
  ready(() => {
    const root = document.querySelector("[data-settings]");
    if (!root) return;
    const sections = [...root.querySelectorAll("details.setting")];
    const links = [...root.querySelectorAll("[data-settings-link]")];
    const wide = window.matchMedia("(min-width: 840px)");
    const remembered = new Map(sections.map((s) => [s, s.open]));

    function apply() {
      root.classList.toggle("settings-wide", wide.matches);
      for (const section of sections) section.open = wide.matches ? true : remembered.get(section);
    }
    // Wide: the headers are titles, not toggles. Narrow: remember what the user opened.
    root.addEventListener("click", (event) => {
      if (wide.matches && event.target.closest("details.setting > summary")) event.preventDefault();
    });
    for (const section of sections) {
      section.addEventListener("toggle", () => { if (!wide.matches) remembered.set(section, section.open); });
    }
    wide.addEventListener("change", apply);
    apply();

    if ("IntersectionObserver" in window && links.length) {
      const observer = new IntersectionObserver((items) => {
        const visible = items.filter((item) => item.isIntersecting);
        if (!visible.length) return;
        const id = visible[0].target.id;
        for (const link of links) link.setAttribute("aria-current", link.hash === `#${id}` ? "true" : "false");
      }, { rootMargin: "-20% 0px -60% 0px" });
      sections.forEach((section) => observer.observe(section));
    }
  });

  ready(() => {
    const root = document.querySelector("[data-crop-editor]");
    if (!root) return;
    const image = root.querySelector("[data-crop-preview]");
    if (image.complete && image.naturalWidth) initCropEditor(root);
    else image.addEventListener("load", () => initCropEditor(root), { once: true });
  });

  // --- Family tree / relationship network: pan and zoom of the drawing (5.12) -----------
  // Same model as the photo viewer: a stage with a transformed content, pinch and wheel
  // zoom around a point, drag to move. Tapping a node opens its sheet; dragging does not.
  const PAN_MIN = 0.25;
  const PAN_MAX = 3;
  const panZoom = { root: null, scale: 1, x: 0, y: 0, pointers: new Map(), pinch: null, moved: 0 };

  function panApply() {
    const content = panZoom.root && panZoom.root.querySelector("[data-pan-content]");
    if (content) content.style.transform = `translate(${panZoom.x}px, ${panZoom.y}px) scale(${panZoom.scale})`;
  }

  function panZoomAt(factor, cx, cy) {
    const next = Math.min(PAN_MAX, Math.max(PAN_MIN, panZoom.scale * factor));
    const ratio = next / panZoom.scale;
    panZoom.x = cx - (cx - panZoom.x) * ratio;
    panZoom.y = cy - (cy - panZoom.y) * ratio;
    panZoom.scale = next;
    panApply();
  }

  // Start readable (never below 60 %), with the central person as close to the middle as
  // possible while keeping the drawing inside the canvas: a drawing that fits is centred.
  function panAxis(canvas, size, focus, scale) {
    const drawn = size * scale;
    if (drawn <= canvas) return (canvas - drawn) / 2;
    return Math.min(0, Math.max(canvas - drawn, canvas / 2 - focus * scale));
  }

  function panCenter() {
    const root = panZoom.root;
    if (!root) return;
    const svg = root.querySelector("svg");
    const focus = root.querySelector("[data-node-focus]");
    const width = Number(svg.getAttribute("width"));
    const height = Number(svg.getAttribute("height"));
    const fit = Math.min(root.clientWidth / width, root.clientHeight / height);
    panZoom.scale = Math.min(1, Math.max(0.6, fit));
    const fx = focus ? Number(focus.dataset.nodeX) : width / 2;
    const fy = focus ? Number(focus.dataset.nodeY) : height / 2;
    panZoom.x = panAxis(root.clientWidth, width, fx, panZoom.scale);
    panZoom.y = panAxis(root.clientHeight, height, fy, panZoom.scale);
    panApply();
  }

  function initPanZoom() {
    const root = document.querySelector("[data-pan-zoom]");
    if (root === panZoom.root) return;
    panZoom.root = root;
    panZoom.pointers.clear();
    panZoom.pinch = null;
    if (root) panCenter();
  }
  ready(initPanZoom);
  document.addEventListener("htmx:afterSettle", initPanZoom);
  window.addEventListener("resize", () => { if (panZoom.root) panCenter(); });

  function panPoint(event) {
    const r = panZoom.root.getBoundingClientRect();
    return { x: event.clientX - r.left, y: event.clientY - r.top };
  }

  document.addEventListener("pointerdown", (event) => {
    if (!panZoom.root || !panZoom.root.contains(event.target) || event.target.closest(".tree-controls, .tree-legend")) return;
    panZoom.pointers.set(event.pointerId, panPoint(event));
    panZoom.moved = 0;
    panZoom.root.classList.add("dragging");
    if (panZoom.pointers.size === 2) {
      const [a, b] = [...panZoom.pointers.values()];
      panZoom.pinch = {
        distance: Math.hypot(a.x - b.x, a.y - b.y),
        scale: panZoom.scale,
        mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
        origin: { x: panZoom.x, y: panZoom.y },
      };
    }
  });

  window.addEventListener("pointermove", (event) => {
    if (!panZoom.pointers.has(event.pointerId)) return;
    const previous = panZoom.pointers.get(event.pointerId);
    const point = panPoint(event);
    panZoom.pointers.set(event.pointerId, point);
    panZoom.moved += Math.hypot(point.x - previous.x, point.y - previous.y);
    if (panZoom.pointers.size === 2 && panZoom.pinch) {
      const [a, b] = [...panZoom.pointers.values()];
      const p = panZoom.pinch;
      const scale = Math.min(PAN_MAX, Math.max(PAN_MIN, p.scale * Math.hypot(a.x - b.x, a.y - b.y) / (p.distance || 1)));
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      panZoom.x = mid.x - (p.mid.x - p.origin.x) * (scale / p.scale);
      panZoom.y = mid.y - (p.mid.y - p.origin.y) * (scale / p.scale);
      panZoom.scale = scale;
      panApply();
    } else if (panZoom.pointers.size === 1) {
      panZoom.x += point.x - previous.x;
      panZoom.y += point.y - previous.y;
      panApply();
    }
  });

  function endPan(event) {
    if (!panZoom.pointers.delete(event.pointerId)) return;
    if (panZoom.pointers.size < 2) panZoom.pinch = null;
    if (panZoom.pointers.size === 0 && panZoom.root) panZoom.root.classList.remove("dragging");
  }
  window.addEventListener("pointerup", endPan);
  window.addEventListener("pointercancel", endPan);

  document.addEventListener("wheel", (event) => {
    if (!panZoom.root || !panZoom.root.contains(event.target)) return;
    event.preventDefault();
    const point = panPoint(event);
    panZoomAt(Math.exp(-event.deltaY * 0.0015), point.x, point.y);
  }, { passive: false });

  document.addEventListener("click", (event) => {
    const control = event.target.closest("[data-pan-zoom-action]");
    if (control && panZoom.root) {
      const action = control.dataset.panZoomAction;
      const cx = panZoom.root.clientWidth / 2;
      const cy = panZoom.root.clientHeight / 2;
      if (action === "in") panZoomAt(1.4, cx, cy);
      if (action === "out") panZoomAt(1 / 1.4, cx, cy);
      if (action === "center") panCenter();
      return;
    }
    const node = event.target.closest("[data-node]");
    if (!node) return;
    // A drag that ends on a node is not a tap; a tap opens the node sheet (the link,
    // which centres the tree on that person, is the fallback without JS).
    event.preventDefault();
    event.stopPropagation();
    if (event.detail !== 0 && panZoom.moved > TAP_MAX_MOVE) return;  // detail 0: keyboard
    const sheet = document.getElementById("sheet-node");
    if (!sheet) { window.location.href = node.getAttribute("href"); return; }
    sheet.querySelector('[data-node-field="name"]').textContent = node.dataset.nodeName;
    const role = sheet.querySelector('[data-node-field="role"]');
    role.textContent = node.dataset.nodeRole || "";
    role.hidden = !node.dataset.nodeRole;
    sheet.querySelector('[data-node-link="center"]').setAttribute("href", node.getAttribute("href"));
    sheet.querySelector('[data-node-link="detail"]').setAttribute("href", node.dataset.nodeDetail);
    sheet.querySelector('[data-node-link="add"]').setAttribute("href", node.dataset.nodeAdd);
    sheet.showModal();
  }, true);


  // ------------------------------------------------ Quiz: photo that sharpens (5.14)
  // The server only serves a pixelation level once its time has come, so this just
  // asks for the next level on schedule (and retries a moment later if it is early).
  const reveal = { timer: null };

  function stopReveal() {
    clearTimeout(reveal.timer);
    reveal.timer = null;
  }

  function startReveal() {
    stopReveal();
    const box = document.querySelector("[data-quiz-reveal]");
    if (!box) return;
    const img = box.querySelector("img");
    const bar = document.querySelector("[data-quiz-reveal-bar]");
    const step = Number(box.dataset.step) * 1000;
    const levels = Number(box.dataset.levels);
    const started = performance.now() - Number(box.dataset.elapsed) * 1000;
    let level = Number(box.dataset.level);
    let loading = false;

    function showTier() {
      if (!bar) return;
      const seconds = (performance.now() - started) / 1000;
      const tiers = Array.from(bar.querySelectorAll(".quiz-reveal-points"));
      const current = tiers.find((tier) => !tier.dataset.until || seconds < Number(tier.dataset.until));
      tiers.forEach((tier) => { tier.hidden = tier !== current; });
    }

    function next() {
      if (!document.body.contains(box)) { stopReveal(); return; }
      showTier();
      const due = Math.min(levels, Math.floor((performance.now() - started) / step));
      if (due > level && !loading) {
        loading = true;
        const url = box.dataset.template.replace("{level}", String(due));
        const loader = new Image();
        loader.onload = () => {
          loading = false;
          if (!document.body.contains(box)) return;
          img.src = url;
          level = due;
          const progress = bar && bar.querySelector("progress");
          if (progress) progress.value = level;
          if (level >= levels && bar) bar.classList.add("done");
        };
        loader.onerror = () => { loading = false; };  // too early: the next tick retries
        loader.src = url;
      }
      if (level < levels || (bar && bar.querySelector(".quiz-reveal-points[data-until]:not([hidden])"))) {
        reveal.timer = setTimeout(next, 500);
      }
    }
    next();
  }

  ready(startReveal);
  document.addEventListener("htmx:afterSettle", startReveal);

  // Emojis quiz: tapping a numbered emoji on the photo opens its name picker.
  document.addEventListener("click", (event) => {
    const cover = event.target.closest("[data-quiz-match]");
    if (!cover) return;
    const select = document.getElementById(`match-${cover.dataset.quizMatch}`);
    if (!select) return;
    select.focus();
    if (typeof select.showPicker === "function") {
      try { select.showPicker(); } catch (error) { /* needs a user gesture: focus is enough */ }
    }
  });

})();
