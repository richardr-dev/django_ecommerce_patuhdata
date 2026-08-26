(() => {
  const postal = document.getElementById("postal-code");
  const areaId = document.getElementById("destination-area-id");
  const areaSearch = document.getElementById("area-search");
  const areaResults = document.getElementById("area-results");
  const rateButton = document.getElementById("check-shipping");
  const message = document.getElementById("shipping-message");
  if (!postal || !areaId || !areaSearch || !areaResults || !rateButton) return;

  let postcodeTimer;
  let resolving = false;

  function fillAddress(area) {
    areaId.value = area.id;
    areaSearch.value = area.name;
    if (area.postal_code) postal.value = area.postal_code;
    for (const field of ["province", "city", "district", "village"]) {
      const input = document.querySelector(`[name=${field}]`);
      if (input && area[field]) input.value = area[field];
    }
  }

  function calculateRates() {
    rateButton.disabled = false;
    rateButton.click();
  }

  async function resolvePostcode(value) {
    if (resolving) return;
    resolving = true;
    message.textContent = `Mencari alamat untuk kode pos ${value}…`;
    try {
      const response = await fetch(`/checkout/shipping-areas/?q=${encodeURIComponent(value)}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Kode pos tidak tersedia");
      const exact = data.areas.filter((area) => String(area.postal_code) === value);
      if (exact.length === 1) {
        fillAddress(exact[0]);
        areaResults.innerHTML = "";
        message.textContent = "Alamat ditemukan. Menghitung ongkir…";
        calculateRates();
      } else if (exact.length > 1) {
        areaSearch.value = value;
        areaSearch.dispatchEvent(new Event("input", { bubbles: true }));
        message.textContent = "Kode pos memiliki beberapa wilayah. Pilih alamat yang tepat.";
      } else {
        message.textContent = "Kode pos tidak ditemukan. Cari kecamatan atau kota secara manual.";
      }
    } catch (error) {
      message.textContent = error.message;
    } finally {
      resolving = false;
    }
  }

  postal.addEventListener("input", () => {
    clearTimeout(postcodeTimer);
    const value = postal.value.trim();
    if (!/^\d{5}$/.test(value)) return;
    postcodeTimer = setTimeout(() => resolvePostcode(value), 350);
  });

  areaResults.addEventListener("click", (event) => {
    if (event.target.closest(".area-option")) {
      setTimeout(calculateRates, 0);
    }
  });

  if (/^\d{5}$/.test(postal.value.trim()) && !areaId.value) {
    resolvePostcode(postal.value.trim());
  }
})();
