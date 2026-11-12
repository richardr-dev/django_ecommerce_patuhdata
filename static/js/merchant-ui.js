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


  /* ── Soft navigation for the sidebar ────────────────────────────────────────
     Clicking a sidebar item loads the page in the background and swaps only <main>: the sidebar
     is never re-rendered, so its scroll position and every group you opened stay exactly as
     they are, and nothing flashes. Anything unexpected (login redirect, error, non-HTML,
     modifier keys, new tab) falls back to a normal page load. */
  var navBox = document.getElementById("pd-nav"), mainBox = document.querySelector(".pd-main");
  var softToken = 0;
  function samePath(a, b) {
    try { var x = new URL(a, location.origin), y = new URL(b, location.origin); return x.pathname === y.pathname && x.search.replace(/[?&]store=\d+/, "") === y.search.replace(/[?&]store=\d+/, ""); }
    catch (e) { return false; }
  }
  function markActive(doc) {
    var wanted = doc.querySelector("#pd-nav .nav-link.is-active"), home = doc.querySelector("#pd-nav .nav-home.is-active");
    navBox.querySelectorAll(".is-active").forEach(function (el) { el.classList.remove("is-active"); el.removeAttribute("aria-current"); });
    var target = null;
    if (wanted) {
      var href = wanted.getAttribute("href");
      navBox.querySelectorAll(".nav-link").forEach(function (a) { if (!target && samePath(a.getAttribute("href"), href)) target = a; });
    } else if (home) target = navBox.querySelector(".nav-home");
    if (target) {
      target.classList.add("is-active"); target.setAttribute("aria-current", "page");
      var group = target.closest("details"); if (group) group.open = true;   // open the group, never close the others
      target.scrollIntoView({block: "nearest"});
    }
  }
  function runScripts(root) {
    root.querySelectorAll("script").forEach(function (old) {
      var fresh = document.createElement("script");
      Array.prototype.forEach.call(old.attributes, function (attr) { fresh.setAttribute(attr.name, attr.value); });
      fresh.textContent = old.textContent;
      old.parentNode.replaceChild(fresh, old);
    });
  }
  function softLoad(url, push) {
    var token = ++softToken;
    progress(true);
    fetch(url, {credentials: "same-origin", headers: {"X-Requested-With": "pd-soft-nav"}}).then(function (r) {
      var type = r.headers.get("content-type") || "";
      if (!r.ok || type.indexOf("text/html") === -1 || r.url.indexOf("/login") > -1) throw new Error("hard");
      return r.text().then(function (html) { return {html: html, url: r.url}; });
    }).then(function (res) {
      if (token !== softToken) return;
      var doc = new DOMParser().parseFromString(res.html, "text/html"), fresh = doc.querySelector(".pd-main");
      if (!fresh || !doc.querySelector("#pd-nav")) throw new Error("hard");
      doc.querySelectorAll("head link[rel=stylesheet]").forEach(function (link) {   // page-specific css
        var href = link.getAttribute("href");
        if (!document.querySelector('link[rel=stylesheet][href="' + href + '"]')) document.head.appendChild(link.cloneNode());
      });
      mainBox.innerHTML = fresh.innerHTML;
      runScripts(mainBox);
      document.title = doc.title;
      markActive(doc);
      if (push) history.pushState({soft: 1}, "", res.url);
      window.scrollTo(0, 0);
      document.body.classList.remove("nav-open");
      wireRupiah(mainBox);
      wireToasts();
      if (window.htmx) window.htmx.process(mainBox);
      progress(false);
    }).catch(function () { progress(false); location.href = url; });
  }
  if (navBox && mainBox && window.fetch && window.DOMParser) {
    var SKIP = /(pdf|print|csv|xlsx|export|download|label|logout|toggle|\.json|[?&]format=)/i;
    document.addEventListener("click", function (event) {
      var a = event.target.closest && event.target.closest(".pd-nav a[href], .pd-main a[href], .pd-brand, .create-menu a[href]");
      if (!a || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      if (a.target && a.target !== "_self" || a.hasAttribute("download") || a.hasAttribute("data-no-soft")) return;
      if (a.closest("[hx-get],[hx-post],[hx-boost]") || a.hasAttribute("data-confirm") || a.hasAttribute("data-open")
          || a.hasAttribute("onclick") || a.hasAttribute("hx-get")) return;
      var href = a.getAttribute("href");
      if (!href || href.charAt(0) !== "/" || href.indexOf("/merchant/") !== 0 || SKIP.test(href)) return;
      if (href.indexOf("#") > -1 && samePath(href.split("#")[0] || location.pathname, location.href)) return;
      event.preventDefault();
      if (samePath(href, location.href)) { window.scrollTo(0, 0); return; }
      softLoad(href, true);
    });
    window.addEventListener("popstate", function () { softLoad(location.href, false); });
    history.replaceState({soft: 1}, "", location.href);
  }

  function ready() { wireRupiah(document); wireToasts(); ensureDialog(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready); else ready();
})();
