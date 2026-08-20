# Third-party attribution

The Next.js scaffold in this directory and the `/clone-website` skill at
`.claude/skills/clone-website/SKILL.md` are derived from
[JCodesMore/ai-website-cloner-template](https://github.com/JCodesMore/ai-website-cloner-template),
MIT licensed. The original licence text is kept verbatim in `LICENSE.upstream`.

Changes made for this repository:
- Output is confined to `clones/`; the rest of the repo is off limits.
- Browser automation uses the gstack `browse` CLI instead of Chrome MCP, matching
  the project rule in the root `CLAUDE.md`.
- Every cloned site is namespaced under its own route, layout, and scoped
  stylesheet so many unrelated sites can share one app without style bleed.
- Cloned pages are registered in `src/clones.ts` and listed on the index page.
- The optional Atlas Cloud image-generation fallback was removed; unrecoverable
  assets are reported in a manifest instead of being regenerated.
