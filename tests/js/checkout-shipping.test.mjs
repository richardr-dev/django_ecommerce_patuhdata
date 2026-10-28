// Behaviour tests for static/js/checkout-shipping.js. No dependencies: run with
//   node --test tests/js/
// The script runs against a minimal fake DOM, a manual clock and a fetch whose responses the test
// releases explicitly, so races and timeouts are deterministic.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../../static/js/checkout-shipping.js", import.meta.url), "utf8");

class El {
  constructor(tag, props = {}) {
    Object.assign(this, { tagName: tag, children: [], listeners: {}, parent: null, value: "",
      disabled: false, checked: false, className: "", dataset: {}, name: "", type: "", _text: "" },
    props);
  }
  // Like a real <input>, assigned values are always stored as strings.
  get value() { return this._value ?? ""; }
  set value(value) { this._value = String(value); }
  get textContent() { return this.children.length ? this.children.map((c) => c.textContent).join("") : this._text; }
  set textContent(value) { this.children = []; this._text = String(value); }
  append(...els) { for (const el of els) { el.parent = this; this.children.push(el); } }
  appendChild(el) { this.append(el); return el; }
  replaceChildren(...els) { this.children = []; this._text = ""; this.append(...els); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type) { for (const fn of this.listeners[type] || []) fn({ target: this }); }
  click() { this.dispatch("click"); }
  get lastElementChild() { return this.children.at(-1); }
  closest(tag) { let node = this; while (node && node.tagName !== tag) node = node.parent; return node; }
  *walk() { for (const child of this.children) { yield child; yield* child.walk(); } }
}

function setup({ postal = "", areaId = "" } = {}) {
  const ids = {
    "check-shipping": new El("button", { disabled: true }),
    "shipping-choices": new El("div"),
    "shipping-message": new El("p"),
    "checkout-submit": new El("button", { disabled: true }),
    "postal-code": new El("input", { value: postal }),
    "destination-area-id": new El("input", { value: areaId }),
    "area-search": new El("input"),
    "area-results": new El("div"),
    "shipping-summary": new El("strong", { _text: "Pilih layanan" }),
  };
  const named = Object.fromEntries(["province", "city", "district", "village"]
    .map((name) => [name, new El("input", { name })]));
  named.csrfmiddlewaretoken = new El("input", { value: "csrf-token" });
  const script = new El("script", { dataset: { areasUrl: "/areas/", ratesUrl: "/rates/" } });
  const document = {
    currentScript: script,
    getElementById: (id) => ids[id] || null,
    createElement: (tag) => new El(tag),
    querySelector(selector) {
      if (selector === "[name=shipping_method]:checked") {
        return [...ids["shipping-choices"].walk()].find((el) => el.name === "shipping_method" && el.checked) || null;
      }
      const match = /^\[name=(\w+)\]$/.exec(selector);
      return match ? named[match[1]] || null : null;
    },
  };

  let now = 0;
  const timers = new Map();
  let nextTimer = 1;
  const clock = {
    setTimeout(fn, delay = 0) { const id = nextTimer++; timers.set(id, { fn, at: now + delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    async tick(ms) {
      now += ms;
      for (const [id, timer] of [...timers].sort((a, b) => a[1].at - b[1].at)) {
        if (timer.at <= now && timers.has(id)) { timers.delete(id); timer.fn(); }
      }
      await settle();
    },
  };

  const requests = [];
  function fetch(url, options = {}) {
    return new Promise((resolve, reject) => {
      const request = { url, options, resolve, reject };
      options.signal?.addEventListener("abort", () =>
        reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
      requests.push(request);
    });
  }

  vm.runInNewContext(SOURCE, { document, fetch, AbortController, URLSearchParams, console,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });
  return { ids, named, clock, requests };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

function json(status, body) {
  return { ok: status < 400, status, headers: { get: () => "application/json" }, json: async () => body };
}

function html(status) {
  return { ok: false, status, headers: { get: () => "text/html" },
    json: async () => { throw new SyntaxError("Unexpected token '<'"); } };
}

async function respond(request, response) {
  request.resolve(response);
  await settle();
  await settle();
}

async function type(input, value, clock) {
  input.value = value;
  input.dispatch("input");
  await clock.tick(350);
}

const QUOTES = { quotes: [{ token: "t1", name: "JNE Reguler", price: 18000, eta: "2-3 hari" },
  { token: "t2", name: "SiCepat BEST", price: 25000, eta: "1 hari" }], provider_name: "Biteship" };
const area = (id, postal) => ({ id, name: `Area ${id}`, postal_code: postal, province: "DKI Jakarta" });

test("a slow, older address search cannot overwrite newer results", async () => {
  const { ids, clock, requests } = setup();
  await type(ids["area-search"], "menteng", clock);
  await type(ids["area-search"], "kemang", clock);
  assert.equal(requests.length, 2);

  await respond(requests[1], json(200, { areas: [area("KEMANG", 12730)] }));
  await respond(requests[0], json(200, { areas: [area("MENTENG", 10310)] }));

  const options = ids["area-results"].children;
  assert.deepEqual(options.map((el) => el.textContent), ["Area KEMANG"]);
});

test("a corrected postcode is resolved instead of being dropped", async () => {
  const { ids, named, clock, requests } = setup();
  await type(ids["postal-code"], "10310", clock);
  await type(ids["postal-code"], "10320", clock);
  assert.equal(requests.length, 2, "second postcode must trigger its own lookup");

  await respond(requests[1], json(200, { areas: [area("B", 10320)] }));
  await respond(requests[0], json(200, { areas: [area("A", 10310)] }));

  assert.equal(ids["destination-area-id"].value, "B");
  assert.equal(ids["postal-code"].value, "10320");
  assert.equal(named.province.value, "DKI Jakarta");
  assert.equal(requests.at(-1).url, "/rates/", "an exact match fetches rates automatically");
});

test("happy path: exact postcode fills the address and shows quotes", async () => {
  const { ids, clock, requests } = setup();
  await type(ids["postal-code"], "10310", clock);
  await respond(requests[0], json(200, { areas: [area("MENTENG", 10310)] }));
  const rates = requests[1];
  assert.equal(rates.options.method, "POST");
  assert.equal(rates.options.headers["X-CSRFToken"], "csrf-token");
  assert.equal(new URLSearchParams(rates.options.body).get("destination_area_id"), "MENTENG");

  await respond(rates, json(200, QUOTES));

  const radios = [...ids["shipping-choices"].walk()].filter((el) => el.name === "shipping_method");
  assert.deepEqual(radios.map((el) => el.value), ["t1", "t2"]);
  assert.equal(ids["checkout-submit"].disabled, false);
  assert.match(ids["shipping-summary"].textContent, /18\.000/);
  assert.equal(ids["shipping-message"].textContent, "2 layanan Biteship tersedia");
});

test("a postcode shared by several areas asks the customer to choose", async () => {
  const { ids, clock, requests } = setup();
  await type(ids["postal-code"], "40111", clock);
  await respond(requests[0], json(200, { areas: [area("A", 40111), area("B", 40111), area("C", 40112)] }));
  assert.equal(ids["area-results"].children.length, 2);
  assert.equal(requests.length, 1, "no rates until an area is chosen");

  ids["area-results"].children[1].click();
  await settle();
  assert.equal(ids["destination-area-id"].value, "B");
  assert.equal(requests.at(-1).url, "/rates/");
});

test("rates for an old address are discarded when the postcode changes mid-request", async () => {
  const { ids, clock, requests } = setup({ areaId: "OLD", postal: "10310" });
  const stale = requests[0];
  assert.equal(stale.url, "/rates/", "a re-rendered form with an address reloads its quotes");

  await type(ids["postal-code"], "1032", clock);
  await respond(stale, json(200, QUOTES));

  assert.equal(ids["shipping-choices"].children.length, 0);
  assert.equal(ids["checkout-submit"].disabled, true);
});

for (const [name, response, expected] of [
  ["an HTML error page", html(502), /Layanan kurir sedang bermasalah/],
  ["an expired session (CSRF 403)", html(403), /Sesi Anda berakhir/],
  ["a JSON error from the server", json(422, { error: "Alamat tidak valid." }), /^Alamat tidak valid\.$/],
  ["an empty quote list", json(200, { quotes: [] }), /Tidak ada layanan kurir/],
]) {
  test(`${name} shows a readable message and re-enables the button`, async () => {
    const { ids, requests } = setup({ areaId: "MENTENG", postal: "10310" });
    await respond(requests[0], response);
    assert.match(ids["shipping-message"].textContent, expected);
    assert.doesNotMatch(ids["shipping-message"].textContent, /Unexpected token|JSON/);
    assert.equal(ids["check-shipping"].disabled, false);
    assert.equal(ids["checkout-submit"].disabled, true);
  });
}

test("a courier API that never answers times out after 20 seconds", async () => {
  const { ids, clock } = setup({ areaId: "MENTENG", postal: "10310" });
  assert.equal(ids["shipping-message"].textContent, "Mengambil tarif kurir…");
  await clock.tick(19999);
  assert.equal(ids["shipping-message"].textContent, "Mengambil tarif kurir…");
  await clock.tick(1);
  assert.match(ids["shipping-message"].textContent, /terlalu lama/);
  assert.equal(ids["check-shipping"].disabled, false);
});

test("a dropped connection is explained in Indonesian", async () => {
  const { ids, requests } = setup({ areaId: "MENTENG", postal: "10310" });
  requests[0].reject(new TypeError("Failed to fetch"));
  await settle();
  await settle();
  assert.match(ids["shipping-message"].textContent, /Koneksi terputus/);
});

test("address search errors are shown in the results list", async () => {
  const { ids, clock, requests } = setup();
  await type(ids["area-search"], "menteng", clock);
  await respond(requests[0], html(500));
  assert.match(ids["area-results"].textContent, /Layanan kurir sedang bermasalah/);
});
