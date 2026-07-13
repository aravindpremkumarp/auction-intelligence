# Attribution

This template pack adapts open-source work:

## motion-anything (Apache-2.0)

- **Source:** https://github.com/nexu-io/motion-anything — © nexu.io, Apache License 2.0.
- **What was adapted:** the CSS/JS motion recipes in `lib/motion.css` / `lib/motion.js`
  (`count-up`, `kinetic-headline`, `stagger-list`, `shine-border`, `attention-pulse`,
  `shiny-text`, `toast-pop` spring) and the scene structure of two HyperFrames
  templates (`hyperframes-money-counter-hype`, `hyperframes-logo-outro-cinematic`)
  in the reel compositions.
- **Changes made:** re-tokened to the Auctionscope palette; count-up reformatted for
  Indian currency (₹ lakh/crore, en-IN grouping) and switched to load-time autostart;
  looping effects freeze once `data-render-ready` is set; the reel variants of
  count-up and typewriter are reimplemented as seek-safe GSAP timeline renders
  (the upstream rAF/IntersectionObserver versions do not render deterministically
  under frame-by-frame video capture).
- Full triage of the upstream library: `docs/marketing/motion-anything-catalog.md`.

## GSAP (`lib/gsap.min.js`)

- **Source:** https://gsap.com — © GreenSock/Webflow, standard GSAP license (free to
  use; see the license header in the file). Vendored so CI renders don't depend on
  CDN reachability.

## HyperFrames

- The 9:16 reel templates are HyperFrames compositions (https://github.com/heygen-com/hyperframes,
  Apache-2.0) and follow the composition contract from the `hyperframes-core` skill.

## Fonts

- Inter, Bricolage Grotesque, JetBrains Mono — loaded from Google Fonts under the
  SIL Open Font License; not vendored.
