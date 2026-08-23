# Clone workspace

Reverse-engineer any website into a Next.js copy, using the `/clone-website` skill.

## Setup (once)

```bash
cd clones
npm install
```

Browser automation is required. It uses the gstack `browse` daemon — invoke `/browse`
once in the session if it is not already running.

## Clone a site

From the repo root, in Claude Code:

```
/clone-website https://example.com https://example.com/pricing
```

Pass every page you want. Each URL becomes its own route, research folder, screenshot
folder, component namespace, and asset folder, so pages never overwrite each other.

## What it does

1. **Recon** — full-page screenshots at 1440 / 768 / 390, then a scroll, click, hover
   and responsive sweep to record behaviour a screenshot cannot show.
2. **Foundation** — per-site route layout, scoped stylesheet with the extracted design
   tokens, TypeScript types, deduplicated SVG icons, and every image/video downloaded.
3. **Specs + parallel builders** — each section gets a spec file of exact
   `getComputedStyle()` values, then a builder agent in its own git worktree builds it.
4. **Assembly** — sections wired into the route with page-level scroll and animation
   behaviour.
5. **Visual QA** — side-by-side diff against the original at desktop and mobile, plus
   an interaction pass, before the clone is called done.

## Check the result

```bash
cd clones
npx next dev      # then open the workspace index at /
npx next build    # must pass before a clone is considered finished
```

## Limits

Front-end only — layout, styling, interactions and assets. No backend, database, auth,
payments, or real-time features. Fidelity is high on marketing and landing pages, lower
on heavy JS animation, video backgrounds, 3D, and stateful web apps.

## Boundaries

Output is confined to `clones/`. Clone your own sites, or competitors for private study.
Do not use this to impersonate a brand, phish, pass a design off as your own, or breach
a site's terms of service. See `NOTICE.md` for upstream attribution.
