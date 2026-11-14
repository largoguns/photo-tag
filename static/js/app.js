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

  // --- Confirmation dialog (replaces window.confirm) ----------------------
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

  // Clicking the backdrop cancels, like pressing Escape.
  document.addEventListener("click", (event) => {
    const dialog = event.target;
    if (dialog.id === "confirm-dialog" && dialog.open) dialog.close("cancel");
  });

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

  // --- Progressive Web App ---------------------------------------------------
  // Service workers need a secure context (HTTPS via Cloudflare, or localhost).
  if ("serviceWorker" in navigator && window.isSecureContext) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    });
  }

  const INSTALL_DISMISS_KEY = "phototag:install-dismissed-at";
  const INSTALL_DISMISS_DAYS = 30;
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

  function recentlyDismissed() {
    try {
      const at = Number(window.localStorage.getItem(INSTALL_DISMISS_KEY));
      return at > 0 && Date.now() - at < INSTALL_DISMISS_DAYS * 86400000;
    } catch (error) {
      return false;
    }
  }

  function showInstallBanner(mode) {
    const banner = document.getElementById("install-banner");
    if (!banner) return;
    banner.querySelectorAll("[data-install-hint]").forEach((hint) => {
      hint.hidden = hint.dataset.installHint !== mode;
    });
    banner.querySelector('[data-install-action="install"]').hidden = mode !== "prompt";
    banner.hidden = false;
    document.body.classList.add("has-install-banner");
  }

  function hideInstallBanner(remember) {
    const banner = document.getElementById("install-banner");
    if (banner) banner.hidden = true;
    document.body.classList.remove("has-install-banner");
    if (remember) {
      try { window.localStorage.setItem(INSTALL_DISMISS_KEY, String(Date.now())); } catch (error) { /* private mode */ }
    }
  }

  // Chromium fires this when the app is installable; keep it to prompt later.
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredInstall = event;
    if (mobilePlatform() && !isInstalled() && !recentlyDismissed()) showInstallBanner("prompt");
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

  function offerInstall() {
    const platform = mobilePlatform();
    if (!platform || isInstalled() || recentlyDismissed()) return;
    if (platform === "ios") {
      showInstallBanner("ios"); // iOS has no install prompt: explain the Share menu.
      return;
    }
    // Android: wait for Chromium's prompt; otherwise (e.g. Firefox) explain the menu.
    window.setTimeout(() => {
      if (!deferredInstall && !isInstalled()) showInstallBanner("android");
    }, INSTALL_PROMPT_WAIT_MS);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", offerInstall);
  else offerInstall();

  // --- Push notifications (profile card) -------------------------------------
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

  async function postJSON(card, url, body) {
    const token = card.querySelector("[name=csrfmiddlewaretoken]").value;
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": token },
      body: JSON.stringify(body || {}),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  function showPushState(card, state, message) {
    card.querySelectorAll("[data-push-state]").forEach((el) => { el.hidden = el.dataset.pushState !== state; });
    card.querySelector('[data-push-action="enable"]').hidden = state !== "off";
    card.querySelector('[data-push-action="disable"]').hidden = state !== "on";
    card.querySelector('[data-push-action="test"]').hidden = state !== "on";
    const note = card.querySelector("[data-push-message]");
    note.textContent = message || "";
    note.hidden = !message;
  }

  async function currentPushState() {
    if (!pushSupported()) return mobilePlatform() === "ios" && !isInstalled() ? "ios-install" : "unsupported";
    if (Notification.permission === "denied") return "denied";
    const registration = await pushRegistration();
    return (await registration.pushManager.getSubscription()) ? "on" : "off";
  }

  async function initPushCard() {
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
        const registration = await pushRegistration();
        const action = button.dataset.pushAction;
        if (action === "enable") {
          // The permission prompt must come from this tap (required on iOS).
          if (await Notification.requestPermission() !== "granted") {
            showPushState(card, "denied");
            return;
          }
          const subscription = await registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: base64UrlToBytes(card.dataset.vapidKey),
          });
          await postJSON(card, card.dataset.subscribeUrl, subscription.toJSON());
          window.location.reload(); // refresh the device list
        } else if (action === "disable") {
          const subscription = await registration.pushManager.getSubscription();
          if (subscription) {
            await postJSON(card, card.dataset.unsubscribeUrl, { endpoint: subscription.endpoint });
            await subscription.unsubscribe();
          }
          window.location.reload();
        } else if (action === "test") {
          const result = await postJSON(card, card.dataset.testUrl);
          showPushState(card, "on", result.message);
        }
      } catch (error) {
        showPushState(card, await currentPushState().catch(() => "unsupported"), card.dataset.errorText);
      } finally {
        button.disabled = false;
      }
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initPushCard);
  else initPushCard();

  // Re-select the highlighted face after HTMX swaps the workspace.
  document.addEventListener("htmx:afterSwap", () => {
    const selected = document.querySelector(".face-box.selected");
    if (selected) {
      const panel = document.getElementById(`face-panel-${selected.dataset.face}`);
      if (panel) panel.classList.add("highlight");
    }
  });
})();
