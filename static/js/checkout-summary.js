// Checkout summary: Subtotal + Ongkir (+ Pajak) = Total, so the amount on the payment page is
// never a surprise. Shipping options carry their price in data-price; live courier quotes
// announce themselves with the "checkout:shipping" event.
(() => {
  const total = document.getElementById("checkout-total");
  if (!total) return;
  const shippingCell = document.getElementById("shipping-summary");
  const taxCell = document.getElementById("tax-summary");
  const totalCell = document.getElementById("total-summary");
  const note = document.getElementById("total-note");
  const couponNote = document.getElementById("coupon-note");
  const subtotal = Number(total.dataset.subtotal) || 0;
  const taxRate = Number(total.dataset.taxRate) || 0;
  const rupiah = (value) => `Rp${Math.round(value).toLocaleString("id-ID")}`;

  function refresh() {
    const chosen = document.querySelector("[name=shipping_method]:checked");
    const shipping = chosen && chosen.dataset.price !== undefined ? Number(chosen.dataset.price) || 0 : null;
    const tax = Math.round(subtotal * taxRate) / 100;
    if (taxCell) taxCell.textContent = rupiah(tax);
    if (shippingCell) {
      shippingCell.textContent = shipping === null ? "Pilih layanan" : shipping ? rupiah(shipping) : "Gratis";
    }
    totalCell.textContent = rupiah(subtotal + tax + (shipping || 0));
    if (note) note.hidden = shipping !== null;
    const coupon = document.querySelector("[name=coupon_code]");
    if (couponNote) couponNote.hidden = !(coupon && coupon.value.trim());
  }

  document.addEventListener("change", (event) => {
    if (event.target.matches("[name=shipping_method]")) refresh();
  });
  document.addEventListener("input", (event) => {
    if (event.target.matches("[name=coupon_code]")) refresh();
  });
  document.addEventListener("checkout:shipping", refresh);
  refresh();
})();
