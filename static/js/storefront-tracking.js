// Seller's analytics and ad pixels, loaded only with the shopper's consent ("Terima semua").
// IDs come from a JSON tag (validated server side); events: page view and purchase.
(() => {
  const tag = document.getElementById("store-tracking");
  if (!tag) return;
  let ids;
  try { ids = JSON.parse(tag.textContent); } catch { return; }
  const consented = () => document.cookie.split("; ").includes("cookie_consent=all");
  let loaded = false;

  function script(src) {
    const node = document.createElement("script");
    node.async = true;
    node.src = src;
    document.head.appendChild(node);
  }

  function load() {
    if (loaded || !consented()) return;
    loaded = true;
    const gtagIds = [ids.ga4_id, ids.google_ads_id].filter(Boolean);
    if (gtagIds.length) {
      window.dataLayer = window.dataLayer || [];
      window.gtag = function () { window.dataLayer.push(arguments); };
      window.gtag("js", new Date());
      gtagIds.forEach((id) => window.gtag("config", id));
      script(`https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(gtagIds[0])}`);
    }
    if (ids.gtm_id) {
      window.dataLayer = window.dataLayer || [];
      window.dataLayer.push({ "gtm.start": Date.now(), event: "gtm.js" });
      script(`https://www.googletagmanager.com/gtm.js?id=${encodeURIComponent(ids.gtm_id)}`);
    }
    if (ids.meta_pixel_id) {
      const fbq = window.fbq = function () {
        fbq.callMethod ? fbq.callMethod.apply(fbq, arguments) : fbq.queue.push(arguments);
      };
      fbq.push = fbq; fbq.loaded = true; fbq.version = "2.0"; fbq.queue = [];
      script("https://connect.facebook.net/en_US/fbevents.js");
      fbq("init", ids.meta_pixel_id);
      fbq("track", "PageView");
    }
    if (ids.tiktok_pixel_id) {
      const ttq = window.ttq = window.ttq || [];
      ["page", "track", "identify"].forEach((method) => {
        ttq[method] = function () { ttq.push([method, ...arguments]); };
      });
      script(`https://analytics.tiktok.com/i18n/pixel/events.js?sdkid=${encodeURIComponent(ids.tiktok_pixel_id)}&lib=ttq`);
      ttq.page();
    }
    if (ids.clarity_id) {
      window.clarity = window.clarity || function () { (window.clarity.q = window.clarity.q || []).push(arguments); };
      script(`https://www.clarity.ms/tag/${encodeURIComponent(ids.clarity_id)}`);
    }
    purchase();
  }

  // A paid order page reports the purchase once per order on this device.
  function purchase() {
    const node = document.querySelector("[data-track-purchase]");
    if (!node) return;
    const key = `lk-purchase-${node.dataset.orderId}`;
    try { if (localStorage.getItem(key)) return; localStorage.setItem(key, "1"); } catch { /* still report */ }
    const value = Number(node.dataset.value) || 0;
    const data = { value, currency: "IDR", transaction_id: node.dataset.orderId };
    if (window.gtag) window.gtag("event", "purchase", data);
    if (window.dataLayer && ids.gtm_id) window.dataLayer.push({ event: "purchase", ecommerce: data });
    if (window.fbq) window.fbq("track", "Purchase", { value, currency: "IDR" });
    if (window.ttq) window.ttq.track("CompletePayment", { value, currency: "IDR" });
  }

  load();
  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-cookie-choice='all']")) setTimeout(load, 0);
  });
})();
