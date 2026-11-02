// Behaviour tests for static/js/storefront-campaign.js countdowns, against a minimal fake DOM.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../../static/js/storefront-campaign.js", import.meta.url), "utf8");

function run(remainingMs) {
  const nodes = {};
  const campaign = {
    dataset: { campaign: "1", endsAt: new Date(Date.now() + remainingMs).toISOString() },
    hidden: false,
    querySelector: selector => (nodes[selector] ??= { textContent: "" }),
  };
  const document = {
    querySelectorAll: selector => (selector === "[data-campaign]" ? [campaign] : []),
    querySelector: () => null,
  };
  vm.runInNewContext(SOURCE, { document, Date, String, Math, Object, setInterval: () => {} });
  const text = name => nodes[`[data-${name}]`].textContent;
  return { campaign, text };
}

test("a 30-hour sale shows 1 day 06 hours, or 30 total hours in the flash sale timer", () => {
  const { text } = run(((30 * 60 + 5) * 60 + 9) * 1000 + 500);
  assert.equal(text("days"), "01");
  assert.equal(text("hours"), "06");
  assert.equal(text("total-hours"), "30");
  assert.equal(text("minutes"), "05");
  assert.equal(text("seconds"), "09");
});

test("an ended sale hides itself", () => {
  const { campaign, text } = run(-1000);
  assert.equal(campaign.hidden, true);
  assert.equal(text("seconds"), "00");
});
