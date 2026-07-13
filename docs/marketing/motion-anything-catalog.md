# motion-anything → Auctionscope: full library triage

**Source:** [nexu-io/motion-anything](https://github.com/nexu-io/motion-anything) (Apache-2.0, v0.1.0, Jul 2026) — an open-source motion engine whose library ships **403 motion recipes** and **58 HyperFrames video templates**.
**Why we mined it:** plan §4 Move 7 (HyperFrames-powered social engine) calls for deal / price-drop / city-carousel templates plus reels. motion-anything is built on the same HyperFrames substrate we already use, so its library is directly harvestable.
**What shipped from this triage:** the adapted template pack in `marketing/templates/` — see the README there. This doc is the complete review of everything else, so nobody has to re-explore the library.

## TL;DR

- **58 "video templates" = 18 real HyperFrames HTML templates + 40 AI-video-generation prompts.** The 40 prompts (Seedance cinematic scenes — dragons, K-pop, wuxia) are useless for our HTML→MP4 pipeline and off-brand for a data-honest auction feed. Of the 18 real ones, **2 were adapted** into our reels and **7 are shortlisted** for later.
- **403 recipes = ~140 genuinely distinct effects + ~263 ports/icons** (96 animated UI icons, 76 Animate.css ports, 10 AnimXYZ ports, 81 React Bits ports). **8 recipes were adapted** into our kit; **19 are shortlisted**; the rest are triaged in Appendix A with a reason each.
- **The biggest catch found during adaptation:** motion-anything's JS recipes animate via `requestAnimationFrame`/`IntersectionObserver` and are **not seek-safe** — under HyperFrames' frame-by-frame capture a callback-driven counter renders `0` forever. Our reel templates reimplement count-up and typewriter as GSAP timeline renders (textContent tween + modifier, staggered `set()`s), verified by seeking the registered timeline and screenshotting.

## What we adapted (8 recipes, 2 template structures)

| Upstream | Became | Where |
|---|---|---|
| `count-up` recipe | INR compact counter (₹ L/Cr, en-IN grouping), autostart for fixed-viewport renders | `lib/motion.js` + seek-safe GSAP variant in both reels |
| `kinetic-headline` recipe | word/letter stagger headline | `lib/motion.{css,js}`, reels |
| `stagger-list` recipe | card-content entrance | `lib/motion.css` |
| `shine-border` recipe | deal-card border light, accent-tinted, frozen for PNG renders | `lib/motion.css` |
| `attention-pulse` recipe | closing-soon urgency ring, danger-tinted | `lib/motion.css` |
| `shiny-text` recipe | eyebrow label shimmer, frozen for PNG renders | `lib/motion.css` |
| `toast-pop` recipe (spring) | `badge-pop` for the computed price-drop % badge | `lib/motion.css` |
| `fx-typewriter-multi` recipe | AI-answer typing in the Evaluate reel, as seek-safe staggered `gsap.set()`s | `evaluate-reel-1080x1920.html` |
| `hyperframes-money-counter-hype` template | stats-reel count-up scene structure | `stats-reel-1080x1920.html` |
| `hyperframes-logo-outro-cinematic` template | logo-sting outro scene | both reels |

## Shortlist — worth pulling later (when the content calendar needs them)

**Templates (7):** `data-bar-chart-race` (city/bank inventory race once /stats has a time series) · `saas-product-promo-30s` (a proper 30s Auctionscope promo) · `website-to-video-promo` · `brand-sizzle-reel` · `tiktok-karaoke-talking-head` (founder clips) · `social-overlay-stack` (social proof) · `product-reveal-minimal` (5s teasers).

**Recipes (19):** full list with per-recipe use cases in Appendix A — highlights: `ref-number-flow` (odometer price ticker), `fx-confetti` (deal-won posts), `rotating-text` (city rotation on covers), `ref-rough-notation` (hand-drawn circle on the key number), `bounce-cards` (multi-property collage), `crossfade-glide` (scene transitions for longer promos).

## What we skipped, and why (bulk categories)

| Bucket | Count | Reason |
|---|---|---|
| AI-video-gen prompt "templates" | 40 of 58 | Text prompts for Seedance-class video models, not HTML; cinematic fantasy content has no fit with notice-grounded auction posts |
| `ref-ua-*` animated icons | 96 | App-UI icons (menus, toggles, spinners) — nothing to do with social output |
| `anim-*` Animate.css ports | 76 | Generic entrances/exits; our kit's stagger/kinetic cover the tasteful subset |
| `xyz-*` AnimXYZ ports | 10 | Same |
| Ambient/shader backgrounds (aurora, plasma, galaxy, `rb-*` WebGL…) | ~78 | Visual noise against the Coinbase-institutional brand; also heavy for CI renders |
| Hover/press, loading, scroll, page-transition categories | ~110 | Interaction motion for running apps — reusable if we ever polish the web app's micro-interactions, but not for rendered social assets |

The two appendices below cover **every single item** — all 403 recipes and all 58 templates — with a verdict each.

### Appendix A — all 403 recipes, triaged

| Recipe | Category | What it is | Auctionscope verdict |
|---|---|---|---|
| `fx-typewriter-multi` | text-kinetic | Multi-line typewriter — types lines in sequence. | **ADAPTED** — fx-typewriter-multi → evaluate-reel AI answer typing (seek-safe GSAP reimplementation) |
| `shiny-text` | text-kinetic | Shiny Text — dependency-free, reduced-motion safe. | **ADAPTED** — shiny-text → eyebrow label shimmer (kit) |
| `star-border` | ambient | Star Border — dependency-free, reduced-motion safe. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `glare-hover` | hover-press | Glare Hover — dependency-free, reduced-motion safe. | Skip — interaction/app-UI motion, not social-video |
| `glitch-text` | text-kinetic | Glitch Text — dependency-free, reduced-motion safe. | Not needed now |
| `fade-content` | scroll-reveal | Fade Content — dependency-free, reduced-motion safe. | Skip — interaction/app-UI motion, not social-video |
| `click-spark` | feedback-delight | Click Spark — dependency-free, reduced-motion safe. | Not needed now |
| `rotating-text` | text-kinetic | Rotating Text — dependency-free, reduced-motion safe. | Shortlist — rotating city names on cover slides |
| `true-focus` | text-kinetic | True Focus — dependency-free, reduced-motion safe. | Not needed now |
| `electric-border` | ambient | Electric Border — dependency-free, reduced-motion safe. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `gradual-blur` | ambient | Gradual Blur — dependency-free, reduced-motion safe. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `blur-text` | text-kinetic | Blur Text — dependency-free, reduced-motion safe. | Not needed now |
| `decrypted-text` | text-kinetic | Decrypted Text — dependency-free, reduced-motion safe. | Shortlist — price-reveal tease variant |
| `falling-text` | text-kinetic | Falling Text — dependency-free, reduced-motion safe. | Not needed now |
| `shuffle-text` | text-kinetic | Shuffle Text — dependency-free, reduced-motion safe. | Not needed now |
| `scroll-float` | text-kinetic | Scroll Float — dependency-free, reduced-motion safe. | Not needed now |
| `circular-text` | ambient | Circular Text — dependency-free, reduced-motion safe. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `magnet-lines` | ambient | Magnet Lines — dependency-free, reduced-motion safe. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `pixel-transition` | hover-press | Pixel Transition — dependency-free, reduced-motion safe. | Skip — interaction/app-UI motion, not social-video |
| `glass-icons` | hover-press | Glass Icons — dependency-free, reduced-motion safe. | Skip — interaction/app-UI motion, not social-video |
| `pixel-card` | hover-press | Pixel Card — dependency-free, reduced-motion safe. | Skip — interaction/app-UI motion, not social-video |
| `blob-cursor` | ambient | Blob Cursor — dependency-free, reduced-motion safe. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `crosshair` | ambient | Crosshair Cursor — dependency-free, reduced-motion safe. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `target-cursor` | ambient | Target Cursor — dependency-free, reduced-motion safe. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `elastic-slider` | feedback-delight | Elastic Slider — dependency-free, reduced-motion safe. | Shortlist — price-range visual for market-vs-reserve |
| `counter` | emphasis | Counter Roll — dependency-free, reduced-motion safe. | Shortlist — elastic stat counter — alt look for stats reel |
| `image-trail` | ambient | Image Trail — dependency-free, reduced-motion safe. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `dock` | hover-press | Magnify Dock — dependency-free, reduced-motion safe. | Skip — interaction/app-UI motion, not social-video |
| `gooey-nav` | hover-press | Gooey Nav — dependency-free, reduced-motion safe. | Skip — interaction/app-UI motion, not social-video |
| `stepper` | feedback-delight | Stepper — dependency-free, reduced-motion safe. | Not needed now |
| `bounce-cards` | entrance | Bounce Cards — dependency-free, reduced-motion safe. | Shortlist — multi-property collage post |
| `aurora` | ambient | A living aurora gradient mesh — GPU fragment shader in dependency-free WebGL2. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `iridescence` | ambient | A flowing iridescent silk field — GPU fragment shader in dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `liquid-chrome` | ambient | Liquid Chrome — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `plasma` | ambient | Plasma — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `threads` | ambient | Threads — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `dark-veil` | ambient | Dark Veil — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `ripple-grid` | ambient | Ripple Grid — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `prism` | ambient | Prism — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `balatro` | ambient | Balatro — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `grainient` | ambient | Grainient — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `plasma-wave` | ambient | Plasma Wave — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `faulty-terminal` | ambient | Faulty Terminal — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `ferrofluid` | ambient | Ferrofluid — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `galaxy` | ambient | Galaxy — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `gradient-blinds` | ambient | Gradient Blinds — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `light-rays` | ambient | Light Rays — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `lightfall` | ambient | Lightfall — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `line-waves` | ambient | Line Waves — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `orb` | ambient | Orb — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `prismatic-burst` | ambient | Prismatic Burst — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `radar` | ambient | Radar — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `side-rays` | ambient | Side Rays — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `soft-aurora` | ambient | Soft Aurora — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `strands` | ambient | Strands — a distinctive GPU shader background, dependency-free WebGL. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `lightning` | ambient | Lightning — a raking electric bolt with glow, faithful GPU shader (dependency-fr | Skip — ambient/shader background; off-brand for a data-honest feed |
| `silk` | ambient | Silk — flowing silky fabric waves, faithful GPU shader (dependency-free WebGL). | Skip — ambient/shader background; off-brand for a data-honest feed |
| `pixel-snow` | ambient | Pixel Snow — pixelated snowfall with depth fade, faithful GPU shader (dependency | Skip — ambient/shader background; off-brand for a data-honest feed |
| `pixel-blast` | ambient | Pixel Blast — a drifting pixel pattern that ripples out from clicks, faithful GP | Skip — ambient/shader background; off-brand for a data-honest feed |
| `dither` | ambient | Dither — a retro Bayer-dithered noise wave, faithful GPU shader (dependency-free | Skip — ambient/shader background; off-brand for a data-honest feed |
| `noise` | ambient | Noise — a film-grain overlay refreshed every few frames (canvas 2D, dependency-f | Skip — ambient/shader background; off-brand for a data-honest feed |
| `dot-grid` | feedback-delight | Dot Grid — dots light up near the pointer, get shoved by fast moves, blast on cl | Not needed now |
| `waves` | ambient | Waves — a perlin-warped line field with a pointer wake (canvas 2D, dependency-fr | Skip — ambient/shader background; off-brand for a data-honest feed |
| `dot-field` | ambient | Dot Field — a gradient dot lattice that bulges away from the pointer with a soft | Skip — ambient/shader background; off-brand for a data-honest feed |
| `particles` | ambient | Particles — a floating 3D particle cloud with slow scene wobble (raw WebGL point | Skip — ambient/shader background; off-brand for a data-honest feed |
| `splash-cursor` | feedback-delight | Splash Cursor — pointer movement splats swirling glowing dye (full multi-pass We | Not needed now |
| `lottie-favorite` | feedback-delight | A heart/favorite icon that animates on toggle — Lottie JSON, portable across web | Not needed now |
| `lottie-fab` | feedback-delight | A floating action button that morphs open — Lottie JSON. | Not needed now |
| `lottie-tab` | page-transition | An animated bottom-tab indicator — Lottie JSON. | Skip — interaction/app-UI motion, not social-video |
| `lottie-pagination` | feedback-delight | An animated pagination dots indicator — Lottie JSON. | Not needed now |
| `kinetic-headline` | text-kinetic | Words stagger in, one after another — for a hero line. | **ADAPTED** — kinetic-headline → headline word-stagger (kit + reels) |
| `page-fade` | page-transition | A soft fade-through between pages or sections. | Skip — interaction/app-UI motion, not social-video |
| `crossfade-glide` | video-transition | Silky shot-to-shot transition for launch films. | Shortlist — video scene transition for longer promos |
| `anim-bounce` | emphasis | Bounce — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-flash` | emphasis | Flash — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-headshake` | emphasis | Head Shake — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-heartbeat` | emphasis | Heart Beat — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-jello` | emphasis | Jello — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-pulse` | emphasis | Pulse — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-rubberband` | emphasis | Rubber Band — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-shake` | emphasis | Shake — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-shakex` | emphasis | Shake X — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-shakey` | emphasis | Shake Y — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-swing` | emphasis | Swing — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-tada` | emphasis | Tada — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-wobble` | emphasis | Wobble — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-backindown` | entrance | Back In Down — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-backinleft` | entrance | Back In Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-backinright` | entrance | Back In Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-backinup` | entrance | Back In Up — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-bouncein` | entrance | Bounce In — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-bounceindown` | entrance | Bounce In Down — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-bounceinleft` | entrance | Bounce In Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-bounceinright` | entrance | Bounce In Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-bounceinup` | entrance | Bounce In Up — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadein` | entrance | Fade In — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeinbottomleft` | entrance | Fade In Bottom Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeinbottomright` | entrance | Fade In Bottom Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeindown` | entrance | Fade In Down — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeindownbig` | entrance | Fade In Down Big — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeinleft` | entrance | Fade In Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeinleftbig` | entrance | Fade In Left Big — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeinright` | entrance | Fade In Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeinrightbig` | entrance | Fade In Right Big — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeintopleft` | entrance | Fade In Top Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeintopright` | entrance | Fade In Top Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeinup` | entrance | Fade In Up — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeinupbig` | entrance | Fade In Up Big — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-flip` | entrance | Flip — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-flipinx` | entrance | Flip In X — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-flipiny` | entrance | Flip In Y — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-lightspeedinleft` | entrance | Light Speed In Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-lightspeedinright` | entrance | Light Speed In Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-rotatein` | entrance | Rotate In — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-rotateindownleft` | entrance | Rotate In Down Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-rotateindownright` | entrance | Rotate In Down Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-rotateinupleft` | entrance | Rotate In Up Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-rotateinupright` | entrance | Rotate In Up Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-slideindown` | entrance | Slide In Down — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-slideinleft` | entrance | Slide In Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-slideinright` | entrance | Slide In Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-slideinup` | entrance | Slide In Up — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-jackinthebox` | entrance | Jack In The Box — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-rollin` | entrance | Roll In — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-zoomin` | entrance | Zoom In — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-zoomindown` | entrance | Zoom In Down — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-zoominleft` | entrance | Zoom In Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-zoominright` | entrance | Zoom In Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-zoominup` | entrance | Zoom In Up — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-backoutdown` | exit | Back Out Down — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-backoutleft` | exit | Back Out Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-backoutright` | exit | Back Out Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-backoutup` | exit | Back Out Up — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-bounceout` | exit | Bounce Out — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-bounceoutdown` | exit | Bounce Out Down — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-bounceoutleft` | exit | Bounce Out Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-bounceoutright` | exit | Bounce Out Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-bounceoutup` | exit | Bounce Out Up — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeout` | exit | Fade Out — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeoutbottomleft` | exit | Fade Out Bottom Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeoutbottomright` | exit | Fade Out Bottom Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeoutdown` | exit | Fade Out Down — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeoutdownbig` | exit | Fade Out Down Big — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeoutleft` | exit | Fade Out Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeoutleftbig` | exit | Fade Out Left Big — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeoutright` | exit | Fade Out Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeoutrightbig` | exit | Fade Out Right Big — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeouttopleft` | exit | Fade Out Top Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeouttopright` | exit | Fade Out Top Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeoutup` | exit | Fade Out Up — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-fadeoutupbig` | exit | Fade Out Up Big — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-rotateout` | exit | Rotate Out — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-rotateoutdownleft` | exit | Rotate Out Down Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-rotateoutdownright` | exit | Rotate Out Down Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-rotateoutupleft` | exit | Rotate Out Up Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-rotateoutupright` | exit | Rotate Out Up Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-slideoutdown` | exit | Slide Out Down — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-slideoutleft` | exit | Slide Out Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-slideoutright` | exit | Slide Out Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-slideoutup` | exit | Slide Out Up — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-hinge` | exit | Hinge — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-rollout` | exit | Roll Out — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-zoomout` | exit | Zoom Out — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-zoomoutdown` | exit | Zoom Out Down — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-zoomoutleft` | exit | Zoom Out Left — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-zoomoutright` | exit | Zoom Out Right — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `anim-zoomoutup` | exit | Zoom Out Up — ready-made CSS keyframe animation (Animate.css). | Generic — Animate.css port; kit covers our entrances |
| `xyz-fade-up` | entrance | Fade Up — composable AnimXYZ entrance (xyz attribute). | Generic — AnimXYZ port; same |
| `xyz-fade-down` | entrance | Fade Down — composable AnimXYZ entrance (xyz attribute). | Generic — AnimXYZ port; same |
| `xyz-fade-left` | entrance | Fade Left — composable AnimXYZ entrance (xyz attribute). | Generic — AnimXYZ port; same |
| `xyz-fade-right` | entrance | Fade Right — composable AnimXYZ entrance (xyz attribute). | Generic — AnimXYZ port; same |
| `xyz-fade-small` | entrance | Fade Small — composable AnimXYZ entrance (xyz attribute). | Generic — AnimXYZ port; same |
| `xyz-fade-big` | entrance | Fade Big — composable AnimXYZ entrance (xyz attribute). | Generic — AnimXYZ port; same |
| `xyz-flip-up` | entrance | Flip Up — composable AnimXYZ entrance (xyz attribute). | Generic — AnimXYZ port; same |
| `xyz-flip-left` | entrance | Flip Left — composable AnimXYZ entrance (xyz attribute). | Generic — AnimXYZ port; same |
| `xyz-rotate` | entrance | Rotate In — composable AnimXYZ entrance (xyz attribute). | Generic — AnimXYZ port; same |
| `xyz-rise-big` | entrance | Rise Big — composable AnimXYZ entrance (xyz attribute). | Generic — AnimXYZ port; same |
| `ref-ua-activity` | loading | Activity — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-airplay` | page-transition | Airplay — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-alertcircle` | feedback-delight | Alert Circle — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-alertoctagon` | feedback-delight | Alert Octagon — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-alerttriangle` | feedback-delight | Alert Triangle — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-archive` | feedback-delight | Archive — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-arrowdown` | page-transition | Arrow Down — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-arrowdowncircle` | page-transition | Arrow Down Circle — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-arrowleftcircle` | page-transition | Arrow Left Circle — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-arrowrightcircle` | page-transition | Arrow Right Circle — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-arrowup` | page-transition | Arrow Up — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-arrowupcircle` | page-transition | Arrow Up Circle — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-behance` | feedback-delight | Behance — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-bookmark` | feedback-delight | Bookmark — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-calendar` | feedback-delight | Calendar — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-checkbox` | feedback-delight | Check Box — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-checkmark` | feedback-delight | Checkmark — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-codepen` | feedback-delight | Codepen — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-copy` | feedback-delight | Copy — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-download` | loading | Download — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-dribbble` | feedback-delight | Dribbble — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-edit` | feedback-delight | Edit — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-error` | feedback-delight | Error — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-explore` | feedback-delight | Explore — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-facebook` | feedback-delight | Facebook — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-folder` | feedback-delight | Folder — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-github` | feedback-delight | Github — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-heart` | feedback-delight | Heart — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-help` | feedback-delight | Help — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-home` | feedback-delight | Home — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-infinity` | loading | Infinity — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-info` | feedback-delight | Info — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-instagram` | feedback-delight | Instagram — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-linkedin` | feedback-delight | Linkedin — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-loading` | loading | Loading — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-loading2` | loading | Loading2 — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-loading3` | loading | Loading3 — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-lock` | feedback-delight | Lock — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-mail` | feedback-delight | Mail — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-maximizeminimize` | page-transition | Maximize Minimize — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-maximizeminimize2` | page-transition | Maximize Minimize2 — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-menu` | page-transition | Menu — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-menu2` | page-transition | Menu2 — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-menu3` | page-transition | Menu3 — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-menu4` | page-transition | Menu4 — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-microphone` | feedback-delight | Microphone — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-microphone2` | feedback-delight | Microphone2 — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-notification` | feedback-delight | Notification — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-notification2` | feedback-delight | Notification2 — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-playpause` | page-transition | Play Pause — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-playpausecircle` | page-transition | Play Pause Circle — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-plustox` | feedback-delight | Plus To X — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-pocket` | feedback-delight | Pocket — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-radiobutton` | loading | Radio Button — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-scrolldown` | page-transition | Scroll Down — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-searchtox` | feedback-delight | Search To X — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-settings` | feedback-delight | Settings — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-settings2` | feedback-delight | Settings2 — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-share` | feedback-delight | Share — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-skipback` | page-transition | Skip Back — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-skipforward` | page-transition | Skip Forward — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-star` | feedback-delight | Star — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-thumbup` | feedback-delight | Thumb Up — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-toggle` | feedback-delight | Toggle — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-trash` | feedback-delight | Trash — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-trash2` | feedback-delight | Trash2 — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-twitter` | feedback-delight | Twitter — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-userminus` | feedback-delight | User Minus — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-userplus` | feedback-delight | User Plus — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-userx` | feedback-delight | User X — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-video` | feedback-delight | Video — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-video2` | feedback-delight | Video2 — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-visibility` | feedback-delight | Visibility — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-visibility2` | feedback-delight | Visibility2 — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-volume` | feedback-delight | Volume — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-youtube` | feedback-delight | Youtube — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-youtube2` | feedback-delight | Youtube2 — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-zoomin` | feedback-delight | Zoom In — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-ua-zoomout` | feedback-delight | Zoom Out — animated icon (useAnimations). | Skip — animated UI icon (app UI, not social) |
| `ref-magnetic-cursor` | hover-press | Elements lean toward the cursor with easing — the signature agency-site micro-in | Skip — interaction/app-UI motion, not social-video |
| `tilt-3d` | hover-press | A card tilts in 3D toward the pointer with a soft glare — tactile depth. Depende | Skip — interaction/app-UI motion, not social-video |
| `ripple-press` | feedback-delight | A ripple blooms from the exact tap/click point — clear, honest press feedback. D | Not needed now |
| `ref-elastic-button` | feedback-delight | Button squashes & stretches on press with a spring — playful but crisp. | Not needed now |
| `spotlight-card` | hover-press | A soft radial spotlight follows the cursor across a card, lifting its border. De | Shortlist — hover spotlight — website listing cards, not social |
| `shine-border` | ambient | A soft light travels continuously around an element's border — quiet, premium. P | **ADAPTED** — shine-border → deal-card border light (kit, accent-tinted) |
| `shimmer-button` | feedback-delight | A soft band of light sweeps across a CTA (slower at rest, faster on hover). Pure | Not needed now |
| `border-beam` | ambient | A small bright comet of light travels the border — a lively accent. Pure CSS, de | Skip — ambient/shader background; off-brand for a data-honest feed |
| `like-burst` | feedback-delight | Celebratory particle burst on tap — for likes & reactions. | Shortlist — engagement celebration micro-post |
| `fade-in-up` | entrance | A clean rise + fade for elements appearing on the page. | Shortlist — baseline entrance (kit stagger covers it) |
| `scroll-reveal` | scroll-reveal | Content rises into view as you scroll. Elegant, restrained. | Skip — interaction/app-UI motion, not social-video |
| `card-lift-hover` | hover-press | A card that lifts toward you on hover. Crisp and tactile. | Skip — interaction/app-UI motion, not social-video |
| `magnetic-button` | hover-press | A button that leans toward the cursor. Alive, not noisy. | Skip — interaction/app-UI motion, not social-video |
| `count-up` | emphasis | Numbers that animate up — for stats & metrics. | **ADAPTED** — count-up → INR compact counter (static kit + seek-safe GSAP reel version) |
| `loading-shimmer` | loading | Skeleton placeholders with a soft sweeping shimmer. | Skip — interaction/app-UI motion, not social-video |
| `attention-pulse` | attention | A soft expanding ring that draws the eye to one element. | **ADAPTED** — attention-pulse → closing-soon urgency ring (kit, danger-tinted) |
| `stagger-list` | entrance | List items rise + fade in one after another. | **ADAPTED** — stagger-list → card content entrance (kit) |
| `toggle-spring` | feedback-delight | A switch whose knob springs across on toggle. | Not needed now |
| `fx-particle-burst` | feedback-delight | A bright particle burst from the center — a celebratory accent. | Not needed now |
| `fx-confetti` | feedback-delight | Confetti fires across the frame — for wins and reveals. | Shortlist — sold/deal-won celebration posts |
| `fx-firework` | feedback-delight | Fireworks launch and burst — a festive hero moment. | Not needed now |
| `fx-starfield` | ambient | A slow drifting starfield — a calm cosmic backdrop. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `fx-matrix-rain` | ambient | Falling code glyphs — a techy backdrop. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `fx-knowledge-graph` | ambient | Linked nodes drifting — a data / AI backdrop. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `fx-neural-net` | ambient | A pulsing neural network — an AI backdrop. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `fx-constellation` | ambient | Connected points like a constellation — quiet motion. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `fx-orbit-ring` | ambient | Particles orbiting rings — an elegant loop. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `fx-galaxy-swirl` | ambient | A swirling galaxy of particles. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `fx-word-cascade` | text-kinetic | Words cascade into place — a kinetic title. | Shortlist — hook-line emphasis in reels |
| `fx-letter-explode` | text-kinetic | Letters fly in from everywhere and settle into the word. | Not needed now |
| `fx-chain-react` | feedback-delight | A chain reaction of bursts ripples across the frame. | Not needed now |
| `fx-magnetic-field` | ambient | Field lines bending around poles — a hypnotic backdrop. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `fx-data-stream` | ambient | Streaming data lines — a flowing techy backdrop. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `fx-gradient-blob` | ambient | Soft drifting gradient blobs — a warm ambient glow. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `fx-sparkle-trail` | feedback-delight | A trail of sparkles — a light, delightful accent. | Not needed now |
| `fx-shockwave` | feedback-delight | An expanding shockwave ripple — an impactful beat. | Not needed now |
| `fx-counter-explosion` | emphasis | A number counts up then bursts — for a big stat. | Shortlist — stat burst — festival/milestone posts |
| `button-press` | feedback-delight | A button that springs down on press and pops back — tactile tap feedback. | Not needed now |
| `ripple` | feedback-delight | A Material-style ripple spreads from the point of contact. | Not needed now |
| `tab-bar-slide` | page-transition | A bottom tab bar whose active pill slides between tabs. | Skip — interaction/app-UI motion, not social-video |
| `toast-pop` | feedback-delight | A toast slides up from the bottom, holds, then fades away. | **ADAPTED** — toast-pop → badge-pop spring for the price-drop % badge (kit) |
| `bottom-sheet` | page-transition | A bottom sheet slides up over a dimmed backdrop, then dismisses. | Skip — interaction/app-UI motion, not social-video |
| `checkbox-pop` | feedback-delight | A checkbox that pops and draws its check on toggle. | Not needed now |
| `ref-animated-toggle` | feedback-delight | Dark/light toggle whose knob springs across with a morph. | Not needed now |
| `ref-drag-reorder` | feedback-delight | List items reflow with spring physics as you drag one to a new spot. | Not needed now |
| `ref-auto-animate` | feedback-delight | Add/remove/move any element and it transitions automatically — one line. | Not needed now |
| `ref-rough-notation` | attention | Underline, circle or highlight text with an animated hand-drawn stroke. | Shortlist — hand-drawn circle/underline on the key number |
| `ref-number-flow` | emphasis | Digits roll & slide as a value changes — for live stats and prices. | Shortlist — odometer-style number ticker — premium price transitions |
| `ref-lenis-smooth` | ambient | Buttery inertial scrolling — the base layer under most award-site motion. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `ref-scrolltrigger-pin` | scroll-reveal | Pin a section and choreograph elements to scroll progress — scrollytelling. | Skip — interaction/app-UI motion, not social-video |
| `ref-css-scroll-anim` | scroll-reveal | Native scroll-timeline reveals — no JS, GPU-cheap, progressively enhanced. | Skip — interaction/app-UI motion, not social-video |
| `ref-aos` | scroll-reveal | Drop-in data-attribute reveals as elements enter the viewport. | Skip — interaction/app-UI motion, not social-video |
| `ref-rellax-parallax` | ambient | Foreground and background drift at different speeds — depth on scroll. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `ref-horizontal-scroll` | scroll-reveal | Vertical scroll drives a horizontal reel of panels — portfolio staple. | Skip — interaction/app-UI motion, not social-video |
| `ref-locomotive` | ambient | Smooth scroll + parallax + in-view classes — the Codrops-era toolkit. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `ref-view-transitions` | page-transition | Native cross-document / SPA morph transitions between states — shared-element ma | Skip — interaction/app-UI motion, not social-video |
| `ref-barba` | page-transition | Seamless SPA-style transitions between pages with lifecycle hooks. | Skip — interaction/app-UI motion, not social-video |
| `ref-flip-shared` | page-transition | An element flies from list to detail, keeping identity — FLIP technique. | Skip — interaction/app-UI motion, not social-video |
| `ref-vaul-drawer` | page-transition | A drawer/bottom-sheet that drags and settles with real spring physics. | Skip — interaction/app-UI motion, not social-video |
| `ref-split-text` | text-kinetic | Split a headline into chars/words/lines and stagger them in — the hero move. | Shortlist — headline char splitter used as reference for reels |
| `ref-splitting` | text-kinetic | Split text into indexed spans for per-char CSS choreography. | Not needed now |
| `text-scramble` | text-kinetic | Glyphs shuffle then resolve into the final word — a techy decode reveal. Depende | Shortlist — price-reveal tease variant |
| `ref-typed` | text-kinetic | Types and deletes phrases with a blinking caret — hero taglines. | Not needed now |
| `ref-variable-font` | text-kinetic | A weight/width wave travels across a headline via font-variation-settings. | Not needed now |
| `gradient-text` | text-kinetic | A slow on-brand gradient drifts through a headline — quiet premium emphasis. Pur | Not needed now |
| `ref-shader-transition` | video-transition | GL displacement wipes between scenes/images — GLSL transition pack. | Not needed now |
| `ref-motion-dev` | entrance | The modern hybrid engine (ex-Framer Motion): springs, layout, scroll, gestures. | Not needed now |
| `ref-animejs` | entrance | Lightweight timeline engine for SVG/DOM/JS-object animation with staggering. | Not needed now |
| `ref-animations-dev` | hover-press | Emil Kowalski's playbook for interface motion that feels right (easing, timing,  | Skip — interaction/app-UI motion, not social-video |
| `ref-joshw-animation` | entrance | Deep, tasteful guide to CSS transitions/keyframes with spring-like feel. | Not needed now |
| `ref-lottiefiles` | feedback-delight | Huge library of ready JSON animations — drop-in for icons, empty states, loaders | Not needed now |
| `ref-rive` | feedback-delight | State-machine-driven vector animation that responds to input in real time. | Not needed now |
| `ref-confetti-tsparticles` | feedback-delight | A tasteful confetti or emoji burst for a genuine win — one per moment. | Not needed now |
| `ref-shader-gradient` | ambient | A living, GPU-rendered gradient mesh — the Stripe/Linear-era hero backdrop. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `ref-webgl-distortion` | ambient | Hover/scroll ripples and displaces imagery via a fragment shader. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `ref-particles-bg` | ambient | A responsive constellation of particles reacting to the pointer. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `ref-metaballs-blob` | ambient | Gooey, merging blobs drift behind the content — organic ambient motion. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `ref-three-hero` | ambient | A lit, floating 3D object that reacts to scroll/pointer — R3F hero. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `ref-spline-3d` | ambient | Drop in a designed, interactive 3D scene — no-code 3D for the web. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `ref-gaussian-splat` | ambient | Photoreal captured 3D (radiance-field splats) rendered live in the browser. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `ref-curtains` | ambient | Turn DOM images into WebGL planes you can warp and shade on scroll/hover. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `rb-evil-eye` | ambient | Evil Eye — animated component. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `rb-ascii-text` | text-kinetic | ASCII Text — animated component. | Not needed now |
| `rb-curved-loop` | text-kinetic | Curved Loop — animated component. | Not needed now |
| `rb-fuzzy-text` | text-kinetic | Fuzzy Text — animated component. | Not needed now |
| `rb-scrambled-text` | text-kinetic | Scrambled Text — animated component. | Not needed now |
| `rb-scroll-velocity` | text-kinetic | Scroll Velocity — animated component. | Not needed now |
| `rb-shuffle` | text-kinetic | Shuffle — animated component. | Not needed now |
| `rb-split-text` | text-kinetic | Split Text — animated component. | Not needed now |
| `rb-text-cursor` | text-kinetic | Text Cursor — animated component. | Not needed now |
| `rb-text-pressure` | text-kinetic | Text Pressure — animated component. | Not needed now |
| `rb-text-type` | text-kinetic | Text Type — animated component. | Not needed now |
| `rb-variable-proximity` | text-kinetic | Variable Proximity — animated component. | Not needed now |
| `rb-ballpit` | ambient | Ballpit — animated component. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `rb-beams` | ambient | Beams — animated component. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `rb-color-bends` | ambient | Color Bends — animated component. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `rb-floating-lines` | ambient | Floating Lines — animated component. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `rb-grid-distortion` | ambient | Grid Distortion — animated component. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `rb-grid-motion` | ambient | Grid Motion — animated component. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `rb-grid-scan` | ambient | Grid Scan — animated component. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `rb-hyperspeed` | ambient | Hyperspeed — animated component. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `rb-letter-glitch` | ambient | Letter Glitch — animated component. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `rb-light-pillar` | ambient | Light Pillar — animated component. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `rb-liquid-ether` | ambient | Liquid Ether — animated component. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `rb-shape-grid` | ambient | Shape Grid — animated component. | Skip — ambient/shader background; off-brand for a data-honest feed |
| `rb-animated-content` | feedback-delight | Animated Content — animated component. | Not needed now |
| `rb-antigravity` | feedback-delight | Antigravity — animated component. | Not needed now |
| `rb-cubes` | feedback-delight | Cubes — animated component. | Not needed now |
| `rb-ghost-cursor` | feedback-delight | Ghost Cursor — animated component. | Not needed now |
| `rb-laser-flow` | feedback-delight | Laser Flow — animated component. | Not needed now |
| `rb-logo-loop` | feedback-delight | Logo Loop — animated component. | Not needed now |
| `rb-magic-rings` | feedback-delight | Magic Rings — animated component. | Not needed now |
| `rb-magnet` | feedback-delight | Magnet — animated component. | Not needed now |
| `rb-meta-balls` | feedback-delight | Meta Balls — animated component. | Not needed now |
| `rb-metallic-paint` | feedback-delight | Metallic Paint — animated component. | Not needed now |
| `rb-orbit-images` | feedback-delight | Orbit Images — animated component. | Not needed now |
| `rb-pixel-trail` | feedback-delight | Pixel Trail — animated component. | Not needed now |
| `rb-ribbons` | feedback-delight | Ribbons — animated component. | Not needed now |
| `rb-shape-blur` | feedback-delight | Shape Blur — animated component. | Not needed now |
| `rb-sticker-peel` | feedback-delight | Sticker Peel — animated component. | Not needed now |
| `rb-animated-list` | hover-press | Animated List — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-border-glow` | hover-press | Border Glow — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-bubble-menu` | hover-press | Bubble Menu — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-card-nav` | hover-press | Card Nav — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-card-swap` | hover-press | Card Swap — animated component. | Shortlist — before/after price swap motion |
| `rb-carousel` | hover-press | Carousel — animated component. | Shortlist — in-app style carousel motion reference |
| `rb-chroma-grid` | hover-press | Chroma Grid — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-circular-gallery` | hover-press | Circular Gallery — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-decay-card` | hover-press | Decay Card — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-dome-gallery` | hover-press | Dome Gallery — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-flowing-menu` | hover-press | Flowing Menu — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-fluid-glass` | hover-press | Fluid Glass — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-flying-posters` | hover-press | Flying Posters — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-folder` | hover-press | Folder — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-glass-surface` | hover-press | Glass Surface — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-infinite-menu` | hover-press | Infinite Menu — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-lanyard` | hover-press | Lanyard — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-magic-bento` | hover-press | Magic Bento — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-masonry` | hover-press | Masonry — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-model-viewer` | hover-press | Model Viewer — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-pill-nav` | hover-press | Pill Nav — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-profile-card` | hover-press | Profile Card — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-reflective-card` | hover-press | Reflective Card — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-scroll-stack` | hover-press | Scroll Stack — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-stack` | hover-press | Stack — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-staggered-menu` | hover-press | Staggered Menu — animated component. | Skip — interaction/app-UI motion, not social-video |
| `rb-tilted-card` | hover-press | Tilted Card — animated component. | Shortlist — hero property card tilt for covers |


### Appendix B — all 58 video templates, triaged

| Template | Category | Aspect | Auctionscope verdict |
|---|---|---|---|
| `hyperframes-html-in-canvas-iphone-device` | VFX / HTML-in-Canvas | 16:9 | Skip — 3D device demo, heavier than our look |
| `hyperframes-html-in-canvas-text-cursor` | VFX / HTML-in-Canvas | 16:9 | Maybe — cinematic search-cursor intro |
| `hyperframes-html-in-canvas-shatter` | VFX / HTML-in-Canvas | 16:9 | Skip — glass shatter outro, off-brand |
| `hyperframes-html-in-canvas-liquid-background` | VFX / HTML-in-Canvas | 16:9 | Skip — liquid hero, off-brand |
| `hyperframes-html-in-canvas-liquid-glass` | VFX / HTML-in-Canvas | 16:9 | Skip — liquid glass, off-brand |
| `hyperframes-html-in-canvas-magnetic` | VFX / HTML-in-Canvas | 16:9 | Skip — abstract field viz, off-brand |
| `hyperframes-html-in-canvas-portal-reveal` | VFX / HTML-in-Canvas | 16:9 | Maybe — dashboard reveal for Evaluate |
| `hyperframes-money-counter-hype` | Short Form | 9:16 | **ADAPTED** → stats-reel count-up scenes |
| `hyperframes-app-showcase-three-phones` | Product | 16:9 | Maybe — app UI showcase (we are web-first) |
| `hyperframes-brand-sizzle-reel` | Marketing | 16:9 | Shortlist — brand sizzle when more assets exist |
| `hyperframes-saas-product-promo-30s` | Marketing | 16:9 | Shortlist — base for a 30s Auctionscope promo |
| `hyperframes-logo-outro-cinematic` | Branding | 16:9 | **ADAPTED** → outro scene in both reels |
| `hyperframes-product-reveal-minimal` | Cinematic | 16:9 | Shortlist — 5s feature teaser |
| `hyperframes-social-overlay-stack` | Short Form | 9:16 | Shortlist — social-proof overlay stack |
| `hyperframes-tiktok-karaoke-talking-head` | Short Form | 9:16 | Shortlist — founder talking-head with captions |
| `hyperframes-data-bar-chart-race` | Data | 16:9 | Shortlist — city/bank inventory race (needs our /stats time series) |
| `hyperframes-flight-map-route` | Travel | 16:9 | Maybe — city-to-city auction map motion |
| `hyperframes-website-to-video-promo` | Marketing | 16:9 | Shortlist — site-tour cut from auctionscope.in screenshots |
| `3d-animated-boy-building-lego` | General | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `a-decade-of-refinement-glow-up` | Advertising | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `ancient-guardian-dragon-rescue` | General | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `ancient-indian-kingdom-fpv-video` | General | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `animation-transfer-and-camera-tracking-prompt` | General | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `beat-synced-outfit-transformation-dance` | General | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `character-intro-motion-graphics-sequence` | Motion Graphics | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `cinematic-birthday-celebration-sequence` | Cinematic | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `cinematic-dragon-interaction-flight` | Cinematic | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `cinematic-east-asian-woman-hand-dance` | Cinematic | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `cinematic-emotional-face-close-up` | Cinematic | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `cinematic-marine-biologist-exploration` | Cinematic | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `cinematic-music-podcast-and-guitar-technique` | Cinematic | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `cinematic-route-navigation-guide` | Cinematic | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `cinematic-street-racing-sequence-for-seedance-2` | Cinematic | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `cinematic-vampire-alley-fight-sequence` | Cinematic | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `crimson-horizon-sci-fi-cinematic-sequence` | Cinematic | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `cyberpunk-game-trailer-script` | General | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `forbidden-city-cat-satire` | General | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `hollywood-haute-couture-fantasy-video-prompt` | VFX / Fantasy | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `hunched-character-animation` | General | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `live-action-anime-adaptation-water-vs-thunder-br` | Anime | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `luxury-supercar-cinematic-narrative` | Cinematic | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `magical-academy-storyboard-sequence` | Advertising | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `modern-rural-aesthetics-healing-short-film-video` | Cinematic | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `nightclub-flyer-atmospheric-animation` | General | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `retro-hk-wuxia-film-aesthetic` | Cinematic | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `seedance-2-0-15-second-cinematic-japanese-romanc` | Cinematic | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `seedance-2-0-80-year-old-rapper-mv` | General | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `sequence-and-movement-instruction-for-martial-ar` | General | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `soul-switching-mirror-magic-sequence` | VFX / Fantasy | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `toaster-rocket-jumpscare` | General | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `traditional-dance-performance` | Advertising | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `video-seedance-desk-hologram-ar-realdesk` | Product | 9:16 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `video-seedance-three-kingdoms-guanyu-slaying-yan` | Cinematic | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `video-seedance-three-kingdoms-lyubu-yuanmen-arch` | Cinematic | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `video-seedance-three-kingdoms-zhaoyun-cradle-esc` | Cinematic | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `vintage-disney-style-pirate-crocodile-animation` | General | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `viral-k-pop-dance-choreography` | Social / Meme | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |
| `wasteland-factory-chase` | General | 16:9 | Skip — AI-video-generation prompt (Seedance/cinematic), not an HTML template; no fit for a data-grounded auction feed |

## How to mine more later

```bash
git clone https://github.com/nexu-io/motion-anything.git
node cli/bin/motion.js serve 4399        # browse the library with live previews
# recipe sources: recipes/<surface>/<id>/{recipe.motion.yaml, *.css, *.js, SKILL.md}
# real HyperFrames templates: the 18 `hyperframes-*` entries in app/data/video-templates.json
#   → install the underlying catalog blocks with `npx hyperframes add <block>`
```

Everything is Apache-2.0; keep attributions in `marketing/templates/ATTRIBUTION.md` current when vendoring more.
