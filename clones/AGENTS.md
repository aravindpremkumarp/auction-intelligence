# Clone Workspace — Agent Rules

## What this is
`clones/` is a Next.js app whose only job is to host reverse-engineered copies of
other websites, built by the `/clone-website` skill. It is **not** the product.
`web/` (static site) and the Python services at the repo root are off limits to
anything running in this workspace.

## Tech stack
- **Framework:** Next.js 16 (App Router, React 19, TypeScript strict)
- **UI:** shadcn/ui (Tailwind CSS v4, `cn()` utility)
- **Icons:** Lucide React by default, replaced by extracted SVGs per site
- **Node:** 22+

## Commands (run from `clones/`)
- `npm install` — first-time setup
- `npx next dev` — dev server
- `npx next build` — production build
- `npx tsc --noEmit` — typecheck
- `npx eslint` — lint

## Layout
```
src/
  app/
    page.tsx              # workspace index — lists every clone, never overwritten
    layout.tsx            # root layout — shared shell only, no per-site fonts
    globals.css           # shadcn baseline only, shared by every site
    sites/<site-key>.css  # one stylesheet per cloned site, scoped to .site-<site-key>
    <site-key>/           # cloned routes, mirroring the source pathname
  clones.ts               # registry rendered by the index page
  components/
    ui/                   # shadcn primitives
    sites/<site-key>/     # cloned components, namespaced per site and page
  lib/utils.ts            # cn()
public/sites/<site-key>/  # downloaded images, videos, fonts, favicons
docs/
  research/<site-key>/    # extraction artifacts and component specs
  design-references/      # screenshots
scripts/                  # per-page asset download scripts
```

## Hard rules
- **Never leak styles across sites.** Fonts load in `src/app/<site-key>/layout.tsx`;
  tokens live in `src/app/sites/<site-key>.css` under `.site-<site-key>`.
  `globals.css` and the root layout stay generic.
- **Never overwrite another site's route, components, assets, or research folder.**
- **Never write outside `clones/`.**
- Every builder agent verifies `npx tsc --noEmit` before finishing; the orchestrator
  verifies `npx next build` after each merge.
- TypeScript strict, no `any`. Named exports, PascalCase components. Tailwind classes,
  no inline styles. 2-space indent. Mobile-first responsive.
- Match the target 1:1 during emulation — no personal aesthetic changes.

## Agent teams
Builder agents each work in their own git worktree branch scoped to `clones/`, and the
orchestrator merges them, resolving conflicts with full context. Reject any merge
carrying a change outside `clones/`.
