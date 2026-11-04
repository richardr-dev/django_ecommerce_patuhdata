// Variants on the product page: build them from options (Ukuran: S, M, L × Warna: Hitam, Putih),
// add or remove rows, and hide the product's single stock field once the product has variants.
(function () {
  const card = document.getElementById("variants");
  if (!card) return;
  const form = card.closest("form");
  const rows = card.querySelector("[data-variant-rows]");
  const table = card.querySelector("[data-variant-table]");
  const empty = card.querySelector("[data-variant-empty]");
  const template = card.querySelector("[data-variant-template]");
  const total = form.querySelector('input[name="variants-TOTAL_FORMS"]');
  const optionRows = card.querySelector("[data-option-rows]");
  const MAX_OPTIONS = 3;
  const MAX_VARIANTS = 100;

  const field = (row, name) => row.querySelector(`[name$="-${name}"]`);
  const liveRows = () => [...rows.querySelectorAll("[data-variant-row]")].filter((row) => !row.hidden);
  const productSku = () => (form.querySelector('input[name="sku"]')?.value || card.dataset.productSku || "").trim();
  const skuPart = (text) => text.normalize("NFKD").replace(/[^\w]+/g, "-").replace(/^-+|-+$/g, "").toUpperCase();

  function refresh() {
    const hasRows = liveRows().length > 0;
    table.hidden = !hasRows;
    empty.hidden = hasRows;
    // A product with variants keeps its stock per variant, so the single stock fields go away.
    for (const name of ["stock", "initial_stock", "warehouse"]) {
      const input = form.querySelector(`.field-grid [name="${name}"]`);
      const label = input && input.closest("label");
      if (label) label.hidden = hasRows;
      if (input) input.disabled = hasRows;
    }
  }

  function addRow(values = {}) {
    const index = Number(total.value);
    const holder = document.createElement("div");
    holder.innerHTML = template.innerHTML.replace(/__prefix__/g, String(index));
    const row = holder.firstElementChild;
    for (const [name, value] of Object.entries(values)) {
      const input = field(row, name);
      if (input) input.value = value;
    }
    rows.appendChild(row);
    total.value = String(index + 1);
    refresh();
    return row;
  }

  function combinations(options) {
    return options.reduce((combos, option) => combos.flatMap((combo) =>
      option.values.map((value) => [...combo, [option.name, value]])), [[]]);
  }

  card.querySelector("[data-add-option]").addEventListener("click", () => {
    if (optionRows.children.length >= MAX_OPTIONS) return;
    const row = optionRows.firstElementChild.cloneNode(true);
    row.querySelectorAll("input").forEach((input) => { input.value = ""; });
    row.querySelector("[data-option-name]").placeholder = optionRows.children.length === 1 ? "mis. Warna" : "mis. Bahan";
    row.querySelector("[data-option-values]").placeholder = optionRows.children.length === 1 ? "mis. Hitam, Putih (pisahkan dengan koma)" : "mis. Katun, Linen (pisahkan dengan koma)";
    optionRows.appendChild(row);
  });

  card.querySelector("[data-generate]").addEventListener("click", () => {
    const options = [...optionRows.children].map((row) => ({
      name: row.querySelector("[data-option-name]").value.trim(),
      values: [...new Set(row.querySelector("[data-option-values]").value.split(",")
        .map((value) => value.trim()).filter(Boolean))],
    })).filter((option) => option.values.length);
    if (!options.length) {
      optionRows.querySelector("[data-option-values]").focus();
      return;
    }
    const existing = new Set(liveRows().map((row) => field(row, "name").value.trim().toLowerCase()));
    const base = productSku();
    for (const combo of combinations(options)) {
      if (liveRows().length >= MAX_VARIANTS) break;
      const name = combo.map(([, value]) => value).join(" / ");
      if (existing.has(name.toLowerCase())) continue;
      existing.add(name.toLowerCase());
      const attributes = Object.fromEntries(combo.map(([key, value], i) => [key || `Opsi ${i + 1}`, value]));
      const suffix = combo.map(([, value]) => skuPart(value)).join("-");
      addRow({ name, attributes: JSON.stringify(attributes), sku: base ? `${base}-${suffix}` : suffix, stock: "0" });
    }
  });

  card.querySelector("[data-add-variant]").addEventListener("click", () => {
    addRow({ stock: "0" }).querySelector('[name$="-name"]').focus();
  });

  rows.addEventListener("click", (event) => {
    const row = event.target.closest("[data-variant-row]");
    if (!row) return;
    if (event.target.matches("[data-remove-variant]")) {
      // Mark it deleted rather than removing it, so the formset's numbering stays intact.
      field(row, "DELETE").checked = true;
      row.hidden = true;
      refresh();
    } else if (event.target.matches("[data-toggle-more]")) {
      const more = row.querySelector(".variant-more");
      more.hidden = !more.hidden;
      event.target.setAttribute("aria-expanded", String(!more.hidden));
    }
  });

  rows.addEventListener("change", (event) => {
    if (event.target.matches('[name$="-DELETE"]')) {
      event.target.closest("[data-variant-row]").classList.toggle("is-deleted", event.target.checked);
    }
  });

  rows.querySelectorAll(".variant-item.has-errors .variant-more").forEach((more) => {
    if (more.querySelector(".errorlist")) more.hidden = false;
  });
  refresh();
})();
