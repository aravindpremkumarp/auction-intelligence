# Inspiration

A capture space for ideas from the internet — articles, product features, UI
patterns, screenshots, repos, tweets — and, most importantly, **how we plan to
integrate them** into Auction Intelligence.

This folder is intentionally low-friction. Drop things here as you find them.
Nothing here is a commitment to build; it's a staging area for ideas so they
don't get lost in browser tabs.

## How to add an inspiration

1. Copy [`TEMPLATE.md`](./TEMPLATE.md) into a new file named
   `YYYY-MM-DD-short-slug.md` (matching the dated-file convention used in
   `docs/`).
2. Fill in the source, what caught your eye, and your integration plan.
3. Put any images, PDFs, or saved pages in `assets/` and link to them from
   your note (e.g. `![screenshot](./assets/2026-07-06-stripe-pricing.png)`).
4. Commit. That's it.

If you just want to hand something to Claude — paste a link or drop a file in
`assets/` and say "add this to inspiration" — the note can be written up for
you.

## Folder layout

```
inspiration/
├── README.md              # this file
├── TEMPLATE.md            # copy this for each new idea
├── assets/                # screenshots, PDFs, saved pages, clippings
└── YYYY-MM-DD-*.md        # one file per inspiration
```

## Lifecycle of an idea

Each note carries a **Status** so we can tell raw captures from things we've
decided on:

| Status        | Meaning                                                        |
| ------------- | ------------------------------------------------------------- |
| `captured`    | Saved, not yet reviewed                                        |
| `exploring`   | Actively thinking through how/whether it fits                 |
| `planned`     | We intend to build it — promote to a `docs/design/` spec next |
| `implemented` | Shipped; link the PR/design doc                               |
| `parked`      | Interesting but not now (keep the reasoning)                   |

When an idea graduates to `planned`, the real design work moves to
`docs/design/` or `docs/superpowers/specs/`; leave a link back here so the
origin of the idea is traceable.
