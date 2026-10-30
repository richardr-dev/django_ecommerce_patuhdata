// Checkout: fill the address fields from the chosen saved address. Input events let the
// shipping-rate script react to the new postcode exactly as if the shopper had typed it.
(() => {
  const picker = document.getElementById("saved-address");
  const form = document.getElementById("checkout-form");
  if (!picker || !form) return;
  const FIELDS = ["customer_name", "phone", "province", "city", "district", "village",
    "postal_code", "shipping_address", "delivery_notes", "destination_area_id"];
  picker.addEventListener("change", () => {
    const option = picker.selectedOptions[0];
    const isNew = option.dataset.new === "1";
    FIELDS.forEach((name) => {
      const input = form.elements.namedItem(name);
      if (!input || (isNew && name === "customer_name")) return;
      input.value = isNew ? "" : option.dataset[name] || "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
  });
})();
