/* Back-office behaviour shared by every page: rupiah inputs, toasts, styled confirm dialogs,
   submit loading states and the htmx progress bar. No dependencies. */
(function () {
  "use strict";

  /* ── Rupiah inputs ─────────────────────────────────────────────────────────
     <input data-rupiah> shows 1.250.000 while typing and posts the plain digits (1250000), so
     every view can keep parsing a number. */
  function digits(text) { return String(text || "").replace(/[^\d]/g, ""); }
  function group(text) {
    var d = digits(text);
    return d ? d.replace(/\B(?=(\d{3})+(?!\d))/g, ".") : "";
  }
  function formatField(input) {
    var before = input.value, caret = input.selectionStart, left = digits(before.slice(0, caret)).length;
    input.value = group(before);
    // keep the caret after the same digit it was after
    var pos = 0, seen = 0;
    while (pos < input.value.length && seen < left) { if (/\d/.test(input.value[pos])) seen++; pos++; }
    try { input.setSelectionRange(pos, pos); } catch (e) { /* number inputs */ }
  }
  // One delegated listener, so rows added later (cloned or swapped in by htmx) just work.
  document.addEventListener("input", function (event) {
    if (event.target.matches && event.target.matches("input[data-rupiah]")) formatField(event.target);
  });
  function wireRupiah(root) {
    root.querySelectorAll("input[data-rupiah]").forEach(function (input) {
      if (input.type === "number") input.type = "text";
      input.setAttribute("inputmode", "numeric");
      input.setAttribute("autocomplete", "off");
      if (input.value && !input.hasAttribute("data-rupiah-ready")) input.value = group(input.value.split(/[.,]\d{1,2}$/)[0]);
      input.setAttribute("data-rupiah-ready", "1");
    });
  }
  document.addEventListener("submit", function (event) {
    event.target.querySelectorAll("input[data-rupiah]").forEach(function (input) {
      input.value = digits(input.value);
    });
  }, true);

  /* ── Toasts: the server-rendered messages become a dismissible stack ─────── */
  function wireToasts() {
    document.querySelectorAll(".pd-messages .pd-message:not([data-ready])").forEach(function (toast, i) {
      toast.setAttribute("data-ready", "1");
      var close = document.createElement("button");
      close.type = "button"; close.className = "toast-close"; close.setAttribute("aria-label", "Tutup");
      close.innerHTML = "&times;";
      close.addEventListener("click", function () { dismiss(toast); });
      toast.appendChild(close);
      if (!toast.classList.contains("error")) setTimeout(function () { dismiss(toast); }, 5000 + i * 700);
    });
  }
  function dismiss(toast) {
    toast.classList.add("is-leaving");
    setTimeout(function () { toast.remove(); }, 220);
  }
  window.pdToast = function (text, kind) {
    var stack = document.querySelector(".pd-messages");
    if (!stack) {
      stack = document.createElement("div"); stack.className = "pd-messages";
      document.querySelector(".pd-main").prepend(stack);
    }
    var toast = document.createElement("div");
    toast.className = "pd-message " + (kind || "success"); toast.setAttribute("role", "status");
    var span = document.createElement("span"); span.textContent = text; toast.appendChild(span);
    stack.appendChild(toast); wireToasts();
  };

  /* ── Styled confirm: <form data-confirm="Batalkan?"> or <button data-confirm=…> ───── */
  var dialog, pending;
  function ensureDialog() {
    dialog = document.getElementById("pd-confirm");
    if (!dialog) return;
    dialog.addEventListener("close", function () {
      if (dialog.returnValue === "ok" && pending) {
        var run = pending; pending = null; run();
      } else { pending = null; }
    });
  }
  function ask(text, ok, run, danger) {
    if (!dialog || !dialog.showModal) { if (window.confirm(text)) run(); return; }
    dialog.querySelector("[data-confirm-text]").textContent = text;
    var button = dialog.querySelector("[data-confirm-ok]");
    button.textContent = ok || "Ya, lanjutkan";
    button.classList.toggle("is-danger", danger !== false);
    dialog.returnValue = ""; pending = run; dialog.showModal();
  }
  document.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-confirm], a[data-confirm]");
    if (!button || button.__confirmed) return;
    event.preventDefault(); event.stopPropagation();
    ask(button.dataset.confirm, button.dataset.confirmOk, function () {
      button.__confirmed = true;
      if (button.tagName === "A") window.location = button.href; else button.click();
      button.__confirmed = false;
    }, button.dataset.confirmTone !== "neutral");
  }, true);
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.dataset || !form.dataset.confirm || form.__confirmed) return;
    event.preventDefault();
    ask(form.dataset.confirm, form.dataset.confirmOk, function () {
      form.__confirmed = true;
      if (form.requestSubmit) form.requestSubmit(event.submitter || undefined); else form.submit();
    }, form.dataset.confirmTone !== "neutral");
  }, true);

  /* ── Submit buttons show progress and cannot be double-clicked ─────────────── */
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (event.defaultPrevented || form.hasAttribute("hx-post") || form.hasAttribute("hx-get")
        || form.method === "get" || form.method === "dialog" || form.hasAttribute("data-no-loading")) return;
    var button = event.submitter || form.querySelector("button[type=submit], button:not([type])");
    if (!button) return;
    button.classList.add("is-loading");
    setTimeout(function () { button.disabled = true; }, 0);
    setTimeout(function () { button.disabled = false; button.classList.remove("is-loading"); }, 15000);
  });
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) document.querySelectorAll(".is-loading").forEach(function (b) {
      b.disabled = false; b.classList.remove("is-loading");
    });
  });

  /* ── Top progress bar: htmx requests and page navigation ───────────────────── */
  var bar = document.getElementById("pd-progress");
  function progress(on) { if (bar) bar.classList.toggle("on", on); }
  document.addEventListener("htmx:beforeRequest", function () { progress(true); });
  document.addEventListener("htmx:afterRequest", function () { progress(false); });
  document.addEventListener("htmx:afterSwap", function (event) { wireRupiah(event.target); });
  window.addEventListener("beforeunload", function () { progress(true); });

  /* ── Close dialogs on a backdrop click ─────────────────────────────────────── */
  document.addEventListener("click", function (event) {
    if (event.target.tagName === "DIALOG" && event.target.open) event.target.close();
  });

  /* ── Brand per tab ──────────────────────────────────────────────────────────
     With several brands the chosen one lives in the session, which every tab shares. So when
     <body data-store> is set (the user has more than one brand), every back-office link, form
     and htmx request carries ?store=<this tab's brand>, and a tab keeps working on its own brand
     whatever another tab switched to. */
  var tabStore = document.body.dataset.store || "";
  function withStore(href) {
    if (!tabStore || !href || href.charAt(0) !== "/" || href.indexOf("/merchant/") !== 0) return href;
    if (/[?&]store=/.test(href)) return href;
    var hash = "", i = href.indexOf("#");
    if (i > -1) { hash = href.slice(i); href = href.slice(0, i); }
    return href + (href.indexOf("?") > -1 ? "&" : "?") + "store=" + encodeURIComponent(tabStore) + hash;
  }
  window.pdWithStore = withStore;
  if (tabStore) {
    document.addEventListener("click", function (event) {
      var a = event.target.closest && event.target.closest("a[href]");
      if (a) { var fixed = withStore(a.getAttribute("href")); if (fixed !== a.getAttribute("href")) a.setAttribute("href", fixed); }
    }, true);
    document.addEventListener("submit", function (event) {
      var form = event.target;
      var action = form.getAttribute("action") || (window.location.pathname);
      if (!form.hasAttribute("hx-get") && !form.hasAttribute("hx-post") && action.indexOf("/merchant/") === 0
          && !form.querySelector("input[name=store]")) {
        var field = document.createElement("input");
        field.type = "hidden"; field.name = "store"; field.value = tabStore;
        form.appendChild(field);
      }
    }, true);
    document.addEventListener("htmx:configRequest", function (event) {
      if (event.detail.path && event.detail.path.indexOf("/merchant/") === 0) event.detail.parameters.store = tabStore;
    });
  }

  function ready() { wireRupiah(document); wireToasts(); ensureDialog(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready); else ready();
})();
