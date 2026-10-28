// Checkout courier selection: address search, postcode auto-resolve and live rate quotes.
// Every request is versioned so a slow, stale response can never overwrite a newer one.
(() => {
  const script = document.currentScript;
  const button = document.getElementById("check-shipping");
  const choices = document.getElementById("shipping-choices");
  const message = document.getElementById("shipping-message");
  const submit = document.getElementById("checkout-submit");
  const postal = document.getElementById("postal-code");
  const areaId = document.getElementById("destination-area-id");
  const areaSearch = document.getElementById("area-search");
  const areaResults = document.getElementById("area-results");
  const summary = document.getElementById("shipping-summary");
  const csrf = document.querySelector("[name=csrfmiddlewaretoken]");
  if (!script || !button || !choices || !message || !submit || !postal || !areaId ||
      !areaSearch || !areaResults || !csrf) return;

  const areasUrl = script.dataset.areasUrl;
  const ratesUrl = script.dataset.ratesUrl;
  const REQUEST_TIMEOUT_MS = 20000;
  const POSTCODE = /^\d{5}$/;
  const latest = { areas: 0, postcode: 0, rates: 0 };
  let searchTimer;
  let postcodeTimer;

  function show(text, tone = "muted") {
    message.textContent = text;
    message.className = tone;
  }

  function resetRates(text) {
    latest.rates += 1; // Discard any rate response still in flight.
    submit.disabled = true;
    choices.replaceChildren();
    if (summary) summary.textContent = "Pilih layanan";
    show(text);
  }

  // fetch() wrapper that always yields JSON or a customer-readable Indonesian error.
  async function requestJson(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    let response;
    try {
      response = await fetch(url, { ...options, signal: controller.signal,
        headers: { Accept: "application/json", ...(options.headers || {}) } });
    } catch (error) {
      throw new Error(error.name === "AbortError"
        ? "Layanan kurir terlalu lama merespons. Coba lagi."
        : "Koneksi terputus. Periksa internet Anda lalu coba lagi.");
    } finally {
      clearTimeout(timer);
    }
    const type = response.headers.get("Content-Type") || "";
    const data = type.includes("application/json") ? await response.json().catch(() => null) : null;
    if (response.ok && data) return data;
    if (response.status === 403) throw new Error("Sesi Anda berakhir. Muat ulang halaman ini.");
    throw new Error((data && data.error) || "Layanan kurir sedang bermasalah. Coba beberapa saat lagi.");
  }

  function fillAddress(area) {
    areaId.value = area.id;
    areaSearch.value = area.name;
    if (area.postal_code) postal.value = area.postal_code;
    for (const field of ["province", "city", "district", "village"]) {
      const input = document.querySelector(`[name=${field}]`);
      if (input && area[field]) input.value = area[field];
    }
    areaResults.replaceChildren();
    button.disabled = false;
  }

  function renderAreas(areas) {
    areaResults.replaceChildren();
    if (!areas.length) {
      areaResults.textContent = "Alamat tidak ditemukan. Coba nama kecamatan atau kota.";
      return;
    }
    for (const area of areas) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "area-option";
      item.textContent = area.name;
      item.addEventListener("click", () => {
        fillAddress(area);
        loadRates();
      });
      areaResults.appendChild(item);
    }
  }

  async function searchAreas(query) {
    const version = ++latest.areas;
    areaResults.textContent = "Mencari alamat…";
    try {
      const data = await requestJson(`${areasUrl}?q=${encodeURIComponent(query)}`);
      if (version === latest.areas) renderAreas(data.areas || []);
    } catch (error) {
      if (version === latest.areas) areaResults.textContent = error.message;
    }
  }

  async function resolvePostcode(value) {
    const version = ++latest.postcode;
    show(`Mencari alamat untuk kode pos ${value}…`);
    try {
      const data = await requestJson(`${areasUrl}?q=${encodeURIComponent(value)}`);
      if (version !== latest.postcode || postal.value.trim() !== value) return;
      const exact = (data.areas || []).filter((area) => String(area.postal_code) === value);
      if (exact.length === 1) {
        fillAddress(exact[0]);
        loadRates();
      } else if (exact.length > 1) {
        latest.areas += 1;
        renderAreas(exact);
        show("Kode pos memiliki beberapa wilayah. Pilih alamat yang tepat.", "notice");
      } else {
        // The server can still resolve an exact postcode on its own (RajaOngkir).
        button.disabled = false;
        show("Alamat untuk kode pos ini tidak ditemukan otomatis. Klik cek ongkir atau cari kecamatan.", "notice");
      }
    } catch (error) {
      if (version !== latest.postcode) return;
      button.disabled = false;
      show(error.message, "notice");
    }
  }

  function renderQuotes(data) {
    const currency = new Intl.NumberFormat("id-ID", { style: "currency", currency: "IDR",
      maximumFractionDigits: 0 });
    data.quotes.forEach((quote, index) => {
      const label = document.createElement("label");
      label.className = "choice";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "shipping_method";
      radio.value = quote.token;
      radio.required = true;
      radio.checked = index === 0;
      const details = document.createElement("span");
      const name = document.createElement("strong");
      const eta = document.createElement("small");
      const price = document.createElement("strong");
      name.textContent = quote.name;
      eta.textContent = quote.eta || "Estimasi dari kurir";
      price.textContent = currency.format(quote.price);
      details.append(name, eta);
      label.append(radio, details, price);
      choices.appendChild(label);
    });
  }

  function updateSummary() {
    const selected = document.querySelector("[name=shipping_method]:checked");
    if (selected && summary) summary.textContent = selected.closest("label").lastElementChild.textContent;
  }

  async function loadRates() {
    resetRates("Mengambil tarif kurir…");
    const version = latest.rates;
    button.disabled = true;
    try {
      const body = new URLSearchParams({ postal_code: postal.value.trim(),
        destination_area_id: areaId.value });
      const data = await requestJson(ratesUrl, { method: "POST", body,
        headers: { "X-CSRFToken": csrf.value, "Content-Type": "application/x-www-form-urlencoded" } });
      if (version !== latest.rates) return;
      if (!data.quotes || !data.quotes.length) throw new Error("Tidak ada layanan kurir untuk alamat ini.");
      if (data.destination_area_id && !areaId.value) areaId.value = data.destination_area_id;
      renderQuotes(data);
      show(data.warning || `${data.quotes.length} layanan ${data.provider_name || "kurir"} tersedia`,
        data.fallback ? "notice" : "muted");
      submit.disabled = false;
      updateSummary();
    } catch (error) {
      if (version === latest.rates) show(error.message, "notice");
    } finally {
      if (version === latest.rates) button.disabled = !(areaId.value || POSTCODE.test(postal.value.trim()));
    }
  }

  areaSearch.addEventListener("input", () => {
    areaId.value = "";
    button.disabled = true;
    latest.areas += 1;
    areaResults.replaceChildren();
    resetRates("Pilih alamat, lalu cek ulang ongkir.");
    clearTimeout(searchTimer);
    const query = areaSearch.value.trim();
    if (query.length >= 3) searchTimer = setTimeout(() => searchAreas(query), 350);
  });

  postal.addEventListener("input", () => {
    areaId.value = "";
    latest.postcode += 1;
    clearTimeout(postcodeTimer);
    const value = postal.value.trim();
    button.disabled = !POSTCODE.test(value);
    resetRates("Kode pos berubah. Cek ulang ongkir.");
    if (POSTCODE.test(value)) postcodeTimer = setTimeout(() => resolvePostcode(value), 350);
  });

  button.addEventListener("click", loadRates);
  choices.addEventListener("change", updateSummary);

  // A page re-rendered after a validation error keeps the address but loses its quotes.
  if (areaId.value) {
    button.disabled = false;
    loadRates();
  } else if (POSTCODE.test(postal.value.trim())) {
    resolvePostcode(postal.value.trim());
  }
})();
