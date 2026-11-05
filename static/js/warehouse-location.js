// Warehouse location picker: search the courier provider's own areas (kecamatan / postal code),
// then place a draggable pin on an OpenStreetMap map. Fills the form's hidden area_id,
// area_name, latitude and longitude fields and the visible postal code, so shipping rates and
// courier pickups start from exactly this place.
(() => {
  const box = document.querySelector(".warehouse-location");
  if (!box) return;
  const $ = (id) => document.getElementById(id);
  const field = (name) => document.getElementById(`id_${name}`);
  const search = $("location-search");
  const results = $("location-results");
  const chosen = $("location-chosen");
  const status = $("location-status");
  const JAKARTA = [-6.2, 106.816];
  let map, pin, timer, version = 0;

  const say = (text) => { status.textContent = text; };

  // The pin card: confirms a point is chosen and shows the address found there.
  const pinCard = $("location-pin");
  const pinAddress = $("location-pin-address");
  const pinCoords = $("location-pin-coords");
  const undo = $("location-undo");
  let lookup = 0, lookupTimer, previousAddress = null;

  function showPin(lat, lng, text) {
    pinCard.hidden = false;
    pinCoords.textContent = `${Number(lat).toFixed(6)}, ${Number(lng).toFixed(6)}`;
    if (text !== undefined) pinAddress.textContent = text;
  }

  async function reverseGeocode(lat, lng) {
    const url = "https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=18&addressdetails=1"
      + `&accept-language=id&lat=${lat}&lon=${lng}`;
    try {
      const found = await (await fetch(url, { headers: { Accept: "application/json" },
        referrerPolicy: "strict-origin-when-cross-origin" })).json();
      return found && found.display_name ? found : null;
    } catch {
      return null;
    }
  }

  // Street-level part of a reverse-geocoded place: building, road and number, neighbourhood.
  function streetOf(place) {
    const a = place.address || {};
    const road = [a.road, a.house_number].filter(Boolean).join(" ");
    const parts = [place.name && place.name !== a.road ? place.name : "", road,
      a.neighbourhood || a.hamlet || "", a.suburb || a.village || "", a.city_district || "",
      a.city || a.town || a.county || "", a.state || ""].filter(Boolean);
    const unique = parts.filter((part, index) => parts.indexOf(part) === index);
    return unique.join(", ") + (a.postcode ? ` ${a.postcode}` : "");
  }

  async function matchArea(postcode) {
    // No kecamatan chosen yet: pick it from the pin's postcode when exactly one area fits.
    if (!postcode || field("area_id").value) return;
    try {
      const response = await fetch(`${box.dataset.areasUrl}?q=${encodeURIComponent(postcode)}`,
        { headers: { Accept: "application/json" } });
      const data = await response.json();
      const exact = (data.areas || []).filter((area) => area.postal_code === postcode);
      if (exact.length === 1 && !field("area_id").value) choose(exact[0], { movePin: false });
    } catch { /* the seller can still pick the kecamatan by search */ }
  }

  function pinPlaced(lat, lng) {
    showPin(lat, lng, "Mencari alamat di titik ini…");
    clearTimeout(lookupTimer);
    const current = ++lookup;
    lookupTimer = setTimeout(async () => {  // one lookup per drag, within OSM's usage policy
      const place = await reverseGeocode(lat, lng);
      if (current !== lookup) return;
      if (!place) {
        showPin(lat, lng, "Alamat di titik ini tidak ditemukan. Tulis alamat lengkap secara manual.");
        return;
      }
      const street = streetOf(place) || place.display_name;
      showPin(lat, lng, street);
      const address = field("address");
      if (address && address.value.trim() !== street) {
        if (previousAddress === null) previousAddress = address.value;
        address.value = street;
        address.dispatchEvent(new Event("input", { bubbles: true }));
        undo.hidden = !previousAddress.trim();
        say("Pin dipasang dan alamat diperbarui. Lengkapi nomor, blok, atau patokan bila perlu, lalu simpan.");
      }
      const postcode = (place.address || {}).postcode || "";
      const postal = field("postal_code");
      if (postal && !postal.value.trim() && /^\d{5}$/.test(postcode)) postal.value = postcode;
      matchArea(/^\d{5}$/.test(postcode) ? postcode : "");
    }, 600);
  }

  undo.addEventListener("click", () => {
    if (previousAddress === null) return;
    field("address").value = previousAddress;
    previousAddress = null;
    undo.hidden = true;
    say("Alamat dikembalikan. Pin tetap di posisinya.");
  });

  function setPoint(lat, lng, { zoom, exact = false } = {}) {
    field("latitude").value = Number(lat).toFixed(6);
    field("longitude").value = Number(lng).toFixed(6);
    if (exact) pinPlaced(lat, lng);
    else showPin(lat, lng);
    if (!map) return;
    if (pin) pin.setLatLng([lat, lng]);
    else {
      pin = window.L.marker([lat, lng], { draggable: true }).addTo(map);
      pin.on("dragend", () => {
        const point = pin.getLatLng();
        say("Pin dipindahkan. Jangan lupa simpan perubahan.");
        setPoint(point.lat, point.lng, { exact: true });
      });
    }
    map.setView([lat, lng], zoom || Math.max(map.getZoom(), 16));
  }

  async function geocode(query) {
    const url = "https://nominatim.openstreetmap.org/search?format=json&limit=1&countrycodes=id"
      + `&accept-language=id&q=${encodeURIComponent(query)}`;
    try {
      const found = await (await fetch(url, { headers: { Accept: "application/json" },
        referrerPolicy: "strict-origin-when-cross-origin" })).json();
      return found && found[0] ? [Number(found[0].lat), Number(found[0].lon)] : null;
    } catch {
      return null;
    }
  }

  function choose(area, { movePin = true } = {}) {
    field("area_id").value = area.id;
    field("area_name").value = area.name;
    if (area.postal_code && field("postal_code")) field("postal_code").value = area.postal_code;
    $("location-chosen-name").textContent = area.name;
    $("location-chosen-postal").textContent = area.postal_code || "";
    chosen.hidden = false;
    results.replaceChildren();
    search.value = "";
    if (!movePin) return;
    say("Mencari titik di peta…");
    geocode([area.district, area.city, area.province].filter(Boolean).join(", ") || area.name)
      .then((point) => {
        if (point) {
          setPoint(point[0], point[1], { zoom: 14 });
          say("Geser pin ke lokasi persis gudang, lalu simpan.");
        } else {
          say("Titik tidak ditemukan otomatis. Klik peta atau gunakan lokasi Anda.");
        }
      });
  }

  async function find(query) {
    const current = ++version;
    results.textContent = "Mencari…";
    try {
      const response = await fetch(`${box.dataset.areasUrl}?q=${encodeURIComponent(query)}`,
        { headers: { Accept: "application/json" } });
      const data = await response.json();
      if (current !== version) return;
      results.replaceChildren();
      if (data.error) { results.textContent = data.error; return; }
      if (!data.areas.length) { results.textContent = "Tidak ditemukan. Coba nama kecamatan atau kode pos."; return; }
      for (const area of data.areas) {
        const option = document.createElement("button");
        option.type = "button";
        option.textContent = area.name;
        option.addEventListener("click", () => choose(area));
        results.appendChild(option);
      }
    } catch {
      if (current === version) results.textContent = "Koneksi terputus. Coba lagi.";
    }
  }

  search.addEventListener("input", () => {
    clearTimeout(timer);
    const query = search.value.trim();
    if (query.length < 3) { version += 1; results.replaceChildren(); return; }
    timer = setTimeout(() => find(query), 350);
  });
  search.addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });

  $("location-here").addEventListener("click", () => {
    if (!navigator.geolocation) { say("Browser ini tidak mendukung lokasi."); return; }
    say("Mengambil lokasi Anda…");
    navigator.geolocation.getCurrentPosition(
      (position) => {
        say("Lokasi Anda dipakai. Pastikan Anda sedang di gudang, lalu simpan.");
        setPoint(position.coords.latitude, position.coords.longitude, { zoom: 18, exact: true });
      },
      () => say("Izin lokasi ditolak. Geser pin secara manual."),
      { enableHighAccuracy: true, timeout: 10000 });
  });

  $("location-find-address").addEventListener("click", async () => {
    const query = [field("address")?.value, field("area_name").value].filter(Boolean).join(", ");
    if (!query.trim()) { say("Isi alamat atau pilih kecamatan dulu."); return; }
    say("Mencari alamat…");
    const point = await geocode(query);
    if (point) {
      setPoint(point[0], point[1], { zoom: 17 });
      say("Periksa posisi pin, geser bila perlu, lalu simpan.");
    } else {
      say("Alamat tidak ditemukan di peta. Geser pin secara manual.");
    }
  });

  window.addEventListener("load", () => {
    if (!window.L) { say("Peta tidak dapat dimuat. Lokasi tetap bisa dipilih lewat pencarian."); return; }
    const lat = parseFloat(field("latitude").value);
    const lng = parseFloat(field("longitude").value);
    map = window.L.map("location-map").setView(JAKARTA, 11);
    // OpenStreetMap refuses tiles without a Referer, and the site's policy sends none by default.
    window.L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19,
      referrerPolicy: "strict-origin-when-cross-origin",
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' })
      .addTo(map);
    map.on("click", (event) => {
      say("Pin dipasang. Jangan lupa simpan perubahan.");
      setPoint(event.latlng.lat, event.latlng.lng, { exact: true });
    });
    if (!Number.isNaN(lat) && !Number.isNaN(lng)) {
      setPoint(lat, lng, { zoom: 17 });
      showPin(lat, lng, field("address")?.value.trim() || "Titik tersimpan.");
    }
  });
})();
