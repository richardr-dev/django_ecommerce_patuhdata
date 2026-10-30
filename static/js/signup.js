// Store signup: suggest the address from the store name and preview the resulting link.
(() => {
  const name = document.getElementById("id_store_name");
  const slug = document.getElementById("id_slug");
  const preview = document.querySelector("[data-slug-preview]");
  if (!slug) return;
  // Once the seller types their own address, stop overwriting it (clearing it resumes).
  let edited = slug.value !== "";
  const toSlug = (value) => value.toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+/, "").slice(0, 40).replace(/-+$/, "");
  const show = () => { if (preview) preview.textContent = slug.value || "nama-toko"; };
  name?.addEventListener("input", () => {
    if (!edited) { slug.value = toSlug(name.value); show(); }
  });
  slug.addEventListener("input", () => {
    slug.value = slug.value.toLowerCase().replace(/[^a-z0-9-]/g, "");
    edited = slug.value !== "";
    show();
  });
})();
