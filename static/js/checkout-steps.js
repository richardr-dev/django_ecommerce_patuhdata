// Checkout one step at a time (1 Alamat, 2 Pengiriman, 3 Bayar), which reads far better on a
// phone than one long form. Each step change saves the form to the server-side session, so a
// reload or a later visit resumes where the shopper left off. Without JavaScript the page stays
// one ordinary form.
(() => {
  const form = document.getElementById("checkout-form");
  const steps = form ? [...form.querySelectorAll(".checkout-step")] : [];
  const bar = document.getElementById("checkout-steps");
  if (!form || steps.length !== 3 || !bar) return;

  const draftUrl = form.dataset.draftUrl;
  const SKIP = new Set(["idempotency_key", "shipping_method", "payment_method", "loyalty_points"]);
  let current = 1;

  const shown = (field) => !field.disabled && !field.closest("[hidden]");
  const fieldsOf = (step) => [...steps[step - 1].querySelectorAll("input, select, textarea")]
    .filter(shown);
  const shippingChosen = () => !!form.querySelector("[name=shipping_method]:checked");

  function valid(step, { report = false } = {}) {
    for (const field of fieldsOf(step)) {
      if (!field.checkValidity()) {
        if (report) field.reportValidity();
        return false;
      }
    }
    if (step === 2 && !shippingChosen()) {
      if (report) {
        const message = document.getElementById("shipping-message");
        if (message) {
          message.textContent = "Pilih layanan pengiriman terlebih dahulu.";
          message.className = "notice";
        }
      }
      return false;
    }
    return true;
  }

  function saveDraft(step, { beacon = false } = {}) {
    if (!draftUrl) return;
    const data = new FormData(form);
    for (const name of [...data.keys()]) if (SKIP.has(name)) data.delete(name);
    data.set("step", String(step));
    if (beacon && navigator.sendBeacon) {
      navigator.sendBeacon(draftUrl, data);
      return;
    }
    fetch(draftUrl, { method: "POST", body: data, keepalive: true,
      headers: { "X-Requested-With": "fetch" } }).catch(() => {});
  }

  const text = (name) => (form.querySelector(`[name=${name}]`)?.value || "").trim();

  function fillReview() {
    const review = document.getElementById("checkout-review");
    if (!review) return;
    const choice = form.querySelector("[name=shipping_method]:checked");
    const label = choice && choice.closest("label");
    const pickup = choice && choice.hasAttribute("data-pickup");
    const place = [text("shipping_address"), text("district"), text("city"), text("postal_code")]
      .filter(Boolean).join(", ");
    review.querySelector("[data-review=address]").textContent = pickup
      ? `${text("customer_name")} · ${text("phone")} · ambil sendiri di outlet`
      : `${text("customer_name")} · ${text("phone")}\n${place}`;
    const title = label && (label.querySelector(".courier-title") || label.querySelector("span strong"));
    const name = title ? title.textContent.trim() : "";
    const price = label ? label.lastElementChild.textContent.trim() : "";
    review.querySelector("[data-review=shipping]").textContent = [name, price].filter(Boolean)
      .join(" · ");
    review.hidden = false;
  }

  function show(step) {
    current = step;
    steps.forEach((node, index) => node.classList.toggle("active", index + 1 === step));
    bar.querySelectorAll("[data-step-label]").forEach((item) => {
      const number = Number(item.dataset.stepLabel);
      item.classList.toggle("active", number === step);
      item.classList.toggle("done", number < step);
    });
    if (step === 2) {
      const check = document.getElementById("check-shipping");
      const choices = document.getElementById("shipping-choices");
      if (check && choices && !choices.children.length && !check.disabled) check.click();
    }
    if (step === 3) fillReview();
    const top = form.getBoundingClientRect().top + window.scrollY - 90;
    if (window.scrollY > top) window.scrollTo({ top, behavior: "smooth" });
  }

  function go(step) {
    if (step > current) {
      for (let earlier = current; earlier < step; earlier += 1) {
        if (!valid(earlier)) {
          if (earlier !== current) show(earlier);
          valid(earlier, { report: true }); // points the browser at the missing field
          return;
        }
      }
    }
    saveDraft(step);
    show(step);
  }

  form.classList.add("stepped");
  bar.hidden = false;

  form.addEventListener("click", (event) => {
    const target = event.target.closest("[data-next], [data-back], [data-goto], [data-pickup-shortcut]");
    if (!target) return;
    event.preventDefault();
    if (target.hasAttribute("data-next")) go(current + 1);
    else if (target.hasAttribute("data-back")) go(current - 1);
    else if (target.hasAttribute("data-goto")) go(Number(target.dataset.goto));
    else {
      const outlet = form.querySelector("[name=shipping_method][data-pickup]");
      if (outlet) {
        outlet.checked = true;
        outlet.dispatchEvent(new Event("change", { bubbles: true }));
      }
      go(2);
    }
  });
  bar.addEventListener("click", (event) => {
    const item = event.target.closest("[data-step-label]");
    if (item && Number(item.dataset.stepLabel) < current) go(Number(item.dataset.stepLabel));
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") saveDraft(current, { beacon: true });
  });

  // Resume at the saved step, but never past a step that still needs input.
  const wanted = Math.min(3, Math.max(1, Number(form.dataset.initialStep) || 1));
  let start = 1;
  while (start < wanted && valid(start)) start += 1;
  show(start);
})();
