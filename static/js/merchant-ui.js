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
      if (!input.hasAttribute("data-rupiah-ready")) {
        // A pre-filled suggested amount (a counted float, a balance) is meant to be typed over,
        // not typed onto: select it so the first keystroke replaces it instead of appending.
        input.addEventListener("focus", function () { input.select(); });
      }
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
  // Amounts in the company's book currency (apps/shops/currency.py, read from <body data-currency-*>):
  // pdMoney(1250000) -> "Rp1.250.000"; a real minus sign. New scripts use this instead of typing "Rp".
  window.pdMoney = function (value) {
    var d = document.body.dataset, places = parseInt(d.currencyDecimals || "0", 10) || 0;
    var n = Number(value) || 0, fixed = Math.abs(n).toFixed(places).split(".");
    var whole = fixed[0].replace(/\B(?=(\d{3})+(?!\d))/g, d.currencyThousands || ".");
    var text = whole + (places ? (d.currencyDecimal || ",") + fixed[1] : "");
    return (n < 0 && Number(fixed.join(".")) ? "\u2212" : "") + (d.currencySymbol || "Rp") + text;
  };

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
  window.pdAsk = function (text, ok, run, danger) { ask(text, ok, run, danger); };
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
    if (!tabStore || !href || href.charAt(0) !== "/" || href.indexOf("/books/") !== 0) return href;
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
      if (!form.hasAttribute("hx-get") && !form.hasAttribute("hx-post") && action.indexOf("/books/") === 0
          && !form.querySelector("input[name=store]")) {
        var field = document.createElement("input");
        field.type = "hidden"; field.name = "store"; field.value = tabStore;
        form.appendChild(field);
      }
    }, true);
    document.addEventListener("htmx:configRequest", function (event) {
      if (event.detail.path && event.detail.path.indexOf("/books/") === 0) event.detail.parameters.store = tabStore;
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
      rememberVisit();
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
      if (href && href.charAt(0) === "?") href = location.pathname + href;   // pagination links
      if (!href || href.charAt(0) !== "/" || href.indexOf("/books/") !== 0 || SKIP.test(href)) return;
      if (href.indexOf("#") > -1 && samePath(href.split("#")[0] || location.pathname, location.href)) return;
      event.preventDefault();
      if (samePath(href, location.href)) { window.scrollTo(0, 0); return; }
      leaveIfClean(function () { softLoad(href, true); });
    });
    // Capture phase: also takes over history entries htmx pushed for filters, so Back restores the
    // whole page (list, filter form, tab counts) from the server instead of a stale snapshot.
    window.addEventListener("popstate", function (event) { event.stopImmediatePropagation(); softLoad(location.href, false); }, true);
    history.replaceState({soft: 1}, "", location.href);
  }
  window.pdNavigate = function (url) {
    leaveIfClean(function () {
      if (navBox && mainBox && window.fetch && url.indexOf("/books/") === 0) softLoad(withStore(url), true);
      else location.href = url;
    });
  };

  /* ── Unsaved changes: <form data-unsaved> warns before you leave with edits ──── */
  var dirty = false;
  document.addEventListener("input", function (event) {
    if (event.target.closest && event.target.closest("form[data-unsaved]")) dirty = true;
  });
  document.addEventListener("submit", function (event) {
    if (event.target.matches && event.target.matches("form[data-unsaved]")) dirty = false;
  });
  function leaveIfClean(go) {
    if (!dirty || !document.querySelector(".pd-main form[data-unsaved]")) { dirty = false; go(); return; }
    ask("Perubahan di halaman ini belum disimpan. Tinggalkan halaman?", "Tinggalkan", function () {
      dirty = false; go();
    }, true);
  }
  window.addEventListener("beforeunload", function (event) {
    if (dirty && document.querySelector(".pd-main form[data-unsaved]")) { event.preventDefault(); event.returnValue = ""; }
  });

  /* ── Recent records: detail pages you opened, offered again in the search box ── */
  var RECENT = "pd-recent";
  function rememberVisit() {
    var path = location.pathname;
    if (!/\/(\d+|[0-9a-f]{8}-[0-9a-f-]{27})\/(edit\/)?$/.test(path)) return;
    var heading = document.querySelector(".pd-main h1"), label = document.title.split(" · ")[0];
    if (heading) {   // the title without its status pill
      var copy = heading.cloneNode(true);
      copy.querySelectorAll(".pill, .badge, small").forEach(function (el) { el.remove(); });
      label = copy.textContent || label;
    }
    label = label.replace(/\s+/g, " ").trim().slice(0, 80);
    var crumb = document.querySelector(".pd-main .dp-crumb a");
    var item = {href: path, label: label, kind: crumb ? crumb.textContent.trim() : ""};
    try {
      var list = JSON.parse(window.localStorage.getItem(RECENT) || "[]").filter(function (r) { return r.href !== path; });
      list.unshift(item);
      window.localStorage.setItem(RECENT, JSON.stringify(list.slice(0, 8)));
    } catch (e) { /* private mode */ }
  }
  window.pdRecent = function () {
    try { return JSON.parse(window.localStorage.getItem(RECENT) || "[]"); } catch (e) { return []; }
  };

  /* ── Whole table rows open their record (tables with .tbl or [data-rows-link]) ─── */
  document.addEventListener("click", function (event) {
    var row = event.target.closest && event.target.closest(".pd-main table.tbl tbody tr, .pd-main table[data-rows-link] tbody tr");
    if (!row || event.target.closest("a, button, input, select, textarea, label, summary, details, [data-no-row]")) return;
    if (String(window.getSelection && window.getSelection()) !== "") return;
    var link = row.querySelector("a.rowlink[href], a[href]");
    if (!link) return;
    if (event.metaKey || event.ctrlKey) { window.open(withStore(link.getAttribute("href")), "_blank"); return; }
    link.click();
  });

  /* ── Density: comfortable or compact tables, remembered per device ───────────── */
  function applyDensity(value) { document.documentElement.dataset.density = value === "compact" ? "compact" : ""; }
  try { applyDensity(window.localStorage.getItem("pd-density")); } catch (e) { /* default */ }
  document.addEventListener("click", function (event) {
    var toggle = event.target.closest && event.target.closest("[data-density-toggle]");
    if (!toggle) return;
    var next = document.documentElement.dataset.density === "compact" ? "" : "compact";
    applyDensity(next);
    try { window.localStorage.setItem("pd-density", next); } catch (e) { /* private mode */ }
    pdToast(next ? "Tampilan rapat aktif." : "Tampilan nyaman aktif.", "success");
  });

  /* ── Filter URLs stay readable: htmx-pushed addresses drop empty parameters ──── */
  document.addEventListener("htmx:pushedIntoHistory", function () {
    var url = new URL(location.href), changed = false;
    Array.from(url.searchParams.keys()).forEach(function (key) {
      if (url.searchParams.get(key) === "") { url.searchParams.delete(key); changed = true; }
    });
    if (changed) history.replaceState({soft: 1}, "", url.pathname + (url.search ? url.search : "") + url.hash);
  });

  /* ── Keyboard: ? shows the shortcuts, n opens "Buat baru", g + key jumps to a page ── */
  var goKeys = {}, waitingForG = false, gTimer;
  try { goKeys = JSON.parse((document.getElementById("pd-go") || {}).textContent || "{}"); } catch (e) { goKeys = {}; }
  document.addEventListener("keydown", function (event) {
    var el = document.activeElement, typing = el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable);
    if (typing || event.metaKey || event.ctrlKey || event.altKey || document.querySelector("dialog[open]")) return;
    if (waitingForG) {
      waitingForG = false; window.clearTimeout(gTimer);
      if (goKeys[event.key]) { event.preventDefault(); window.pdNavigate(goKeys[event.key].url); }
      return;
    }
    if (event.key === "?") { event.preventDefault(); var keys = document.getElementById("pd-keys"); if (keys) keys.showModal(); }
    else if (event.key === "g") { waitingForG = true; gTimer = window.setTimeout(function () { waitingForG = false; }, 1200); }
    else if (event.key === "n") {
      var menu = document.querySelector(".create-menu");
      if (menu) { event.preventDefault(); menu.open = true; var first = menu.querySelector("a"); if (first) first.focus(); }
    }
  });

  function ready() { wireRupiah(document); wireToasts(); ensureDialog(); rememberVisit(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready); else ready();
})();
