// Visual/responsive check with Playwright: screenshots and overflow detection.
//
// Playwright is not a project dependency; install it in a throw-away folder:
//   mkdir -p /tmp/pw && cd /tmp/pw && npm init -y && npm i playwright@1.49.1 && npx playwright install chromium
// Then, with the dev server running on seeded data (scripts/agents/seed_demo.py):
//   NODE_PATH=/tmp/pw/node_modules node scripts/agents/ui_check.mjs http://127.0.0.1:8765 /tmp/ui-shots
//
// Reports pages with horizontal scroll and elements that overflow the viewport or whose
// text is cut without an ellipsis. Exits with status 1 when problems are found.
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const base = process.argv[2] || "http://127.0.0.1:8765";
const outDir = process.argv[3] || "/tmp/ui-shots";
const user = process.env.UI_USER || "demo";
const password = process.env.UI_PASSWORD || "Demo-Password-1";
const widths = [320, 360, 390, 768, 1024, 1440];
const variants = [
  { name: "light-es", scheme: "light", locale: "es-ES" },
  { name: "dark-en", scheme: "dark", locale: "en-GB" },
];
const pages = ["/", "/gallery/", "/persons/", "/accounts/profile/", "/upload/", "/admin/", "/admin/scan/", "/accounts/users/", "/accounts/audit/", "/ranking/", "/ranking/quiz/", "/questions/", "/questions/?box=all", "/admin/honorifics/", "/map/", "/admin/locations/", "/admin/locations/1/", "/admin/export/", "/tree/", "/tree/?mode=social&depth=2", "/tree/?view=list"];

async function findIssues(page) {
  return page.evaluate(() => {
    const found = [];
    const vw = document.documentElement.clientWidth;
    if (document.documentElement.scrollWidth > vw + 1) found.push(`horizontal scroll (${document.documentElement.scrollWidth}px > ${vw}px)`);
    for (const el of document.querySelectorAll("body *")) {
      const style = getComputedStyle(el);
      if (style.display === "none" || style.visibility === "hidden" || el.closest("dialog:not([open]), [hidden], .filter-bar, .visually-hidden, .skip-link, .tree-stage, .leaflet-container")) continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      if (r.right > vw + 1 || r.left < -1) found.push(`outside viewport: ${el.tagName.toLowerCase()}.${[...el.classList].join(".")} "${(el.textContent || "").trim().slice(0, 40)}"`);
      const clips = el.scrollWidth > el.clientWidth + 1 && style.overflowX !== "visible" && style.textOverflow !== "ellipsis" && !["auto", "scroll"].includes(style.overflowX);
      // Editable fields scroll their own value: a long value is not cut text.
      if (clips && el.children.length === 0 && !["INPUT", "TEXTAREA"].includes(el.tagName)) found.push(`text cut: ${el.tagName.toLowerCase()}.${[...el.classList].join(".")} "${el.textContent.trim().slice(0, 40)}"`);
    }
    return [...new Set(found)].slice(0, 10);
  });
}

// Timed quiz kinds and the emojis of nicknames (spec 5.13, 5.14): start a quiz from its
// card, capture the question once the photo has sharpened a bit, answer and capture.
const quizKinds = ["hidden", "emojis", "locations"];

async function quizFlows(page, variant, problems) {
  for (const width of [390, 1440]) {
    await page.setViewportSize({ width, height: width < 600 ? 844 : 900 });
    const check = async (label) => {
      const issues = await findIssues(page);
      await page.screenshot({ path: path.join(outDir, `${variant.name}-${width}-quiz-${label}.png`), fullPage: true });
      if (issues.length) problems.push({ variant: variant.name, width, url: `quiz ${label}`, issues });
    };
    for (const kind of quizKinds) {
      await page.goto(`${base}/ranking/quiz/`, { waitUntil: "networkidle" });
      const start = page.locator(`form:has(input[name="kind"][value="${kind}"]) button`);
      if (!(await start.count())) { problems.push({ variant: variant.name, width, url: `quiz ${kind}`, issues: ["quiz not available"] }); continue; }
      await Promise.all([page.waitForNavigation(), start.click()]);
      await page.waitForTimeout(kind === "locations" ? 300 : 4500);
      await check(`${kind}-question`);
      const selects = page.locator('select[name="match"]');
      if (await selects.count()) {
        for (let i = 0; i < await selects.count(); i++) await selects.nth(i).selectOption({ index: 1 });
        await page.click('#quiz-card form button[type="submit"]');
      } else {
        await page.click('#quiz-card button[name="option"] >> nth=0');
      }
      await page.waitForSelector(".quiz-feedback");
      await check(`${kind}-feedback`);
    }
    const person = await page.evaluate(async () => {
      const html = await (await fetch("/persons/")).text();
      const match = html.match(/href="(\/persons\/\d+\/)"/);
      return match ? match[1] : null;
    });
    if (person) {
      await page.goto(`${base}${person}?sheet=nicknames`, { waitUntil: "networkidle" });
      await page.waitForTimeout(300);
      await check("nicknames-sheet");
    }
  }
}

fs.mkdirSync(outDir, { recursive: true });
const browser = await chromium.launch();
const problems = [];

for (const variant of variants) {
  const context = await browser.newContext({ colorScheme: variant.scheme, locale: variant.locale, extraHTTPHeaders: { "Accept-Language": variant.locale } });
  const page = await context.newPage();
  await page.goto(`${base}/accounts/login/`);
  await page.fill("#id_username", user);
  await page.fill("#id_password", password);
  await Promise.all([page.waitForNavigation(), page.click('button[type="submit"]')]);
  const photoLink = await page.evaluate(async () => {
    const html = await (await fetch("/gallery/")).text();
    const match = html.match(/href="(\/photos\/\d+\/)"/);
    return match ? match[1] : null;
  });
  const urls = photoLink ? [...pages, photoLink] : pages;

  for (const width of widths) {
    await page.setViewportSize({ width, height: width < 600 ? 800 : 900 });
    for (const url of urls) {
      await page.goto(base + url, { waitUntil: "networkidle" });
      const issues = await findIssues(page);
      const name = `${variant.name}-${width}-${url.replace(/\W+/g, "_") || "home"}.png`;
      await page.screenshot({ path: path.join(outDir, name), fullPage: false });
      if (issues.length) problems.push({ variant: variant.name, width, url, issues });
    }
  }
  // Overlays only render when opened: capture them on a phone and a desktop width.
  for (const width of [360, 1440]) {
    await page.setViewportSize({ width, height: width < 600 ? 800 : 900 });
    const shot = (name) => page.screenshot({ path: path.join(outDir, `${variant.name}-${width}-overlay-${name}.png`) });
    await page.goto(`${base}/`, { waitUntil: "networkidle" });
    await page.click(".avatar-button");
    await shot("user-menu");
    await page.goto(`${base}/gallery/`, { waitUntil: "networkidle" });
    await page.click('[data-open-sheet="sheet-person"]');
    await shot("person-sheet");
    await page.keyboard.press("Escape");
    await page.click('[data-selection="start"]');
    const items = page.locator(".jitem a");
    if (await items.count() > 1) { await items.nth(0).click(); await items.nth(1).click(); }
    await shot("gallery-selection");
    await page.click('[data-selection="cancel"]');
    if (photoLink) {
      await page.goto(base + photoLink, { waitUntil: "networkidle" });
      await page.click('[data-action="open-viewer"]');
      await page.waitForTimeout(300);
      await shot("viewer");
      if (await page.locator('[data-viewer="faces"]').count()) await page.click('[data-viewer="faces"]'); // only photos with faces
      await page.click('[data-viewer="zoom-in"]');
      await page.waitForTimeout(200);
      await shot("viewer-zoom-faces");
    }
  }
  await quizFlows(page, variant, problems);
  await context.close();
}
await browser.close();

if (problems.length) {
  for (const p of problems) console.log(`✗ ${p.variant} ${p.width}px ${p.url}\n    ${p.issues.join("\n    ")}`);
  process.exit(1);
}
console.log(`✓ No overflow found. Screenshots in ${outDir}`);
