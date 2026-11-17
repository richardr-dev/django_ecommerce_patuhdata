/* Kasir (POS) selling screen: cart, totals, payment sheet. The server is the authority on prices,
   stock and totals (pos/services.py:sell); this file only mirrors its arithmetic for the display. */
(function () {
  "use strict";
  var setup = JSON.parse(document.getElementById("pos-setup").textContent);
  var products = setup.products, cart = [], category = "", idem = newKey(), busy = false;
  var $ = function (id) { return document.getElementById(id); };
  var grid = $("grid"), linesBox = $("lines"), q = $("q"), disc = $("disc"), payDialog = $("pay-dialog"), doneDialog = $("done-dialog");

  function newKey() { return (window.crypto && crypto.randomUUID) ? crypto.randomUUID().replace(/-/g, "") : String(Date.now()) + Math.random().toString(16).slice(2); }
  function rp(n) { n = Math.round(n); return (n < 0 ? "−" : "") + "Rp" + Math.abs(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, "."); }
  function digits(text) { return parseInt(String(text || "").replace(/[^\d]/g, ""), 10) || 0; }
  function esc(text) { var d = document.createElement("div"); d.textContent = text; return d.innerHTML; }
  function toast(text) { var t = $("toast"); t.textContent = text; t.hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(function () { t.hidden = true; }, 3200); }
  function money(el) { el.addEventListener("input", function () { var n = digits(el.value); el.value = n ? n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ".") : ""; }); }

  /* ---- catalog ---- */
  function categories() {
    var seen = {}, list = [];
    products.forEach(function (p) { if (p.category && !seen[p.category]) { seen[p.category] = 1; list.push(p.category); } });
    var box = $("cats"); box.innerHTML = "";
    if (!list.length) { box.hidden = true; return; }
    [""].concat(list.sort()).forEach(function (name) {
      var b = document.createElement("button"); b.type = "button"; b.textContent = name || "Semua"; b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", name === category ? "true" : "false");
      b.addEventListener("click", function () { category = name; categories(); draw(); });
      box.appendChild(b);
    });
  }
  function matches() {
    var words = q.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    return products.filter(function (p) {
      if (category && p.category !== category) return false;
      var hay = (p.name + " " + p.sku).toLowerCase();
      return words.every(function (w) { return hay.indexOf(w) >= 0; });
    });
  }
  function draw() {
    var found = matches(); grid.innerHTML = ""; $("none").hidden = found.length > 0;
    found.slice(0, 240).forEach(function (p) {
      var b = document.createElement("button"); b.type = "button"; b.className = "pos-item" + (p.stock === 0 ? " out" : "");
      var stock = p.stock === null ? "" : '<span class="' + (p.stock <= 3 ? "low" : "") + '">stok ' + p.stock + "</span>";
      b.innerHTML = "<b>" + esc(p.name) + '</b><small><span>' + esc(p.sku) + "</span>" + stock + '</small><span class="price">' + rp(p.price) + "</span>";
      b.addEventListener("click", function () { add(p); });
      grid.appendChild(b);
    });
  }
  function add(p) {
    var line = cart.filter(function (l) { return l.id === p.id; })[0];
    var want = (line ? line.qty : 0) + 1;
    if (p.stock !== null && want > p.stock) { toast("Stok " + p.name + " di outlet ini tinggal " + p.stock + "."); return; }
    if (line) line.qty = want; else cart.push({id: p.id, name: p.name, sku: p.sku, price: p.price, qty: 1, discount: 0, stock: p.stock});
    render();
  }

  /* ---- cart ---- */
  function figures() {
    var shelf = cart.reduce(function (s, l) { return s + l.qty * l.price - l.discount; }, 0), d = Math.min(digits(disc.value), shelf);
    var net = shelf - d, tax = 0, total = net;
    if (setup.taxRate) {
      if (setup.inclusive) tax = net - net / (1 + setup.taxRate / 100); else { tax = net * setup.taxRate / 100; total = net + tax; }
    }
    var payable = setup.rounding > 1 ? Math.floor(total / setup.rounding) * setup.rounding : Math.round(total);
    return {shelf: shelf, discount: d, tax: tax, total: total, payable: payable, rounding: payable - total};
  }
  function render() {
    linesBox.innerHTML = "";
    if (!cart.length) linesBox.innerHTML = '<p class="pos-empty">Pilih produk di kiri atau scan SKU.</p>';
    cart.forEach(function (l, i) {
      var row = document.createElement("div"); row.className = "pos-line";
      row.innerHTML = '<div><div class="name">' + esc(l.name) + '</div><div class="meta">' + esc(l.sku) + " · " + rp(l.price) + '</div></div>' +
        '<div class="amt">' + rp(l.qty * l.price - l.discount) + '</div>' +
        '<div class="pos-qty"><button type="button" data-a="dec" aria-label="Kurangi">−</button><input data-a="qty" inputmode="numeric" aria-label="Jumlah ' + esc(l.name) + '" value="' + l.qty + '">' +
        '<button type="button" data-a="inc" aria-label="Tambah">+</button><input class="ld" data-a="disc" inputmode="numeric" aria-label="Diskon ' + esc(l.name) + '" placeholder="diskon Rp" value="' + (l.discount || "") + '">' +
        '<button type="button" class="rm" data-a="rm" aria-label="Hapus ' + esc(l.name) + '">×</button></div>';
      row.addEventListener("click", function (e) {
        var a = e.target.dataset.a; if (!a || e.target.tagName !== "BUTTON") return;
        if (a === "inc") { if (l.stock !== null && l.qty + 1 > l.stock) { toast("Stok tinggal " + l.stock + "."); return; } l.qty += 1; }
        if (a === "dec") { l.qty -= 1; if (l.qty < 1) cart.splice(i, 1); }
        if (a === "rm") cart.splice(i, 1);
        render();
      });
      row.addEventListener("change", function (e) {
        var a = e.target.dataset.a, n = digits(e.target.value);
        if (a === "qty") { if (l.stock !== null && n > l.stock) { toast("Stok tinggal " + l.stock + "."); n = l.stock; } l.qty = n; if (l.qty < 1) cart.splice(i, 1); }
        if (a === "disc") l.discount = Math.min(n, l.qty * l.price);
        render();
      });
      linesBox.appendChild(row);
    });
    var f = figures();
    $("sub").textContent = rp(f.shelf - f.discount);
    $("tax-row").hidden = !setup.taxRate; $("tax").textContent = rp(f.tax);
    $("tax-label").textContent = setup.inclusive ? "Termasuk PPN " + setup.taxRate + "%" : "PPN " + setup.taxRate + "%";
    $("round-row").hidden = Math.abs(f.rounding) < 1; $("round").textContent = rp(f.rounding);
    $("total").textContent = rp(f.payable); $("pay").disabled = !cart.length || f.payable <= 0;
  }

  /* ---- payment ---- */
  var tenderInputs = Array.prototype.slice.call(document.querySelectorAll("[data-tender]"));
  function tendered() { var t = {}; tenderInputs.forEach(function (el) { t[el.dataset.tender] = digits(el.value); }); return t; }
  function payState() {
    var f = figures(), t = tendered(), sum = t.cash + t.qris + t.edc + t.transfer, nonCash = sum - t.cash, box = $("change").parentNode;
    var diff = sum - f.payable, ok = diff >= 0 && nonCash <= f.payable;
    $("change-label").textContent = nonCash > f.payable ? "Non-tunai melebihi total" : diff >= 0 ? "Kembali" : "Kurang";
    $("change").textContent = rp(Math.abs(diff)); box.className = "pos-change " + (ok ? "ok" : "short");
    $("acct-wrap").hidden = !t.transfer; $("finish").disabled = !ok || busy;
  }
  function openPay() {
    if (!cart.length) return;
    var f = figures(); $("due").textContent = rp(f.payable); $("pay-error").hidden = true;
    tenderInputs.forEach(function (el) { el.value = ""; }); $("ref").value = "";
    var quick = $("quick"); quick.innerHTML = "";
    var notes = [f.payable].concat([10000, 20000, 50000, 100000].map(function (n) { return Math.ceil(f.payable / n) * n; }));
    notes.filter(function (n, i) { return notes.indexOf(n) === i; }).slice(0, 5).forEach(function (n, i) {
      var b = document.createElement("button"); b.type = "button"; b.textContent = i === 0 ? "Uang pas" : rp(n);
      b.addEventListener("click", function () { var cash = tenderInputs[0]; cash.value = n; cash.dispatchEvent(new Event("input")); payState(); });
      quick.appendChild(b);
    });
    payDialog.showModal(); payState(); tenderInputs[0].focus();
  }
  function csrf() { return document.querySelector("[name=csrfmiddlewaretoken]").value; }
  function finish() {
    if (busy) return;
    var f = figures(), t = tendered(), tenders = [];
    Object.keys(t).forEach(function (method) {
      if (t[method]) tenders.push({method: method, amount: t[method], reference: method === "cash" ? "" : $("ref").value, account: method === "transfer" ? $("acct").value : null});
    });
    busy = true; payState(); $("pay-error").hidden = true;
    fetch(window.location.pathname + window.location.search, {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRFToken": csrf()},
      body: JSON.stringify({idem: idem, discount: f.discount, buyer: $("buyer").value, tenders: tenders,
        lines: cart.map(function (l) { return {id: l.id, qty: l.qty, discount: l.discount}; })})})
      .then(function (r) { return r.json().then(function (data) { return {ok: r.ok, data: data}; }); })
      .then(function (res) {
        busy = false;
        if (!res.ok) { $("pay-error").textContent = res.data.error || "Transaksi gagal disimpan."; $("pay-error").hidden = false; payState(); return; }
        payDialog.close(); products = res.data.products || products;
        $("done-number").textContent = "Struk " + res.data.number + " · " + rp(res.data.total);
        $("done-change").textContent = rp(res.data.change); $("done-print").href = res.data.receipt + "?print=1";
        bumpShift(res.data.total); doneDialog.showModal(); $("done-next").focus();
      })
      .catch(function () { busy = false; $("pay-error").textContent = "Koneksi terputus. Tekan Selesaikan lagi: transaksi tidak akan tercatat dua kali."; $("pay-error").hidden = false; payState(); });
  }
  var shiftTotal = digits($("shift-total").textContent), shiftCount = digits($("shift-count").textContent);
  function bumpShift(amount) { shiftTotal += Math.round(amount); shiftCount += 1; $("shift-total").textContent = rp(shiftTotal); $("shift-count").textContent = shiftCount + " struk"; }
  function reset() { cart = []; disc.value = ""; $("buyer").value = ""; idem = newKey(); q.value = ""; doneDialog.close(); render(); draw(); q.focus(); }

  /* ---- wiring ---- */
  var acct = $("acct"); setup.accounts.forEach(function (a) { var o = document.createElement("option"); o.value = a.id; o.textContent = a.name; acct.appendChild(o); });
  tenderInputs.forEach(function (el) { money(el); el.addEventListener("input", payState); }); money(disc);
  disc.addEventListener("input", render);
  q.addEventListener("input", draw);
  q.addEventListener("keydown", function (e) {
    if (e.key !== "Enter") return; e.preventDefault();
    var text = q.value.trim().toLowerCase(); if (!text) return;
    var exact = products.filter(function (p) { return p.sku.toLowerCase() === text; }), found = exact.length ? exact : matches();
    if (found.length === 1) { add(found[0]); q.value = ""; draw(); } else if (!found.length) toast("Produk tidak ditemukan: " + q.value);
  });
  $("clear").addEventListener("click", function () { cart = []; render(); });
  $("pay").addEventListener("click", openPay);
  $("pay-form").addEventListener("submit", function (e) { e.preventDefault(); if (!$("finish").disabled) finish(); });
  $("done-next").addEventListener("click", reset);
  doneDialog.addEventListener("cancel", function (e) { e.preventDefault(); reset(); });
  document.querySelectorAll("[data-close]").forEach(function (b) { b.addEventListener("click", function () { b.closest("dialog").close(); }); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "F2") { e.preventDefault(); if (!payDialog.open && !doneDialog.open) openPay(); }
    if (e.key === "F4") { e.preventDefault(); disc.focus(); }
    if (e.key === "/" && document.activeElement.tagName !== "INPUT") { e.preventDefault(); q.focus(); }
  });
  categories(); draw(); render();
})();
