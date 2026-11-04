// Countdown to an order's payment deadline: [data-countdown="<ISO time>"] shows the time left
// (mm:ss, or h:mm:ss); at zero its [data-countdown-scope] hides the pay button and shows its
// [data-expired-note].
(() => {
  const timers = [...document.querySelectorAll("[data-countdown]")]
    .map((node) => ({ node, end: Date.parse(node.dataset.countdown) }))
    .filter((timer) => !Number.isNaN(timer.end));
  if (!timers.length) return;
  const pad = (value) => String(value).padStart(2, "0");

  function tick() {
    const now = Date.now();
    timers.forEach(({ node, end }) => {
      const left = Math.max(0, Math.floor((end - now) / 1000));
      const hours = Math.floor(left / 3600);
      const rest = `${pad(Math.floor((left % 3600) / 60))}:${pad(left % 60)}`;
      node.textContent = hours ? `${hours}:${rest}` : rest;
      if (left) return;
      const scope = node.closest("[data-countdown-scope]") || document;
      scope.classList.add("expired");
      scope.querySelectorAll("[data-pay-button]").forEach((button) => { button.hidden = true; });
      scope.querySelectorAll("[data-expired-note]").forEach((note) => { note.hidden = false; });
    });
  }
  tick();
  setInterval(tick, 1000);
})();
