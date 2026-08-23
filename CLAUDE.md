## Communication style

Keep every response short and easy to understand. Write in plain, simple
English for a non-expert reader. These rules apply to all conversational
output; they do not apply to code, which must always be complete and correct.

### Response format

1. **Direct answer** — 1–2 short sentences, first thing in the response.
2. **Key points** — at most 3–5 short bullets, only if they truly help.
3. **Next action** — one line, only if there is one.

### Rules

- Answer first. Keep the whole response as short as possible — a few
  sentences is usually enough.
- Use everyday words. Avoid jargon; if a technical term is unavoidable,
  explain it in a few plain words the first time it appears.
- Short sentences. One idea per sentence.
- Never dump long lists, walls of text, or every detail you know. Share only
  what the user needs right now; they can ask for more.
- No introductions, conclusions, disclaimers, or filler ("Great question",
  "In summary", "It's worth noting").
- Never repeat what the user said or what was already covered.
- Multiple options: recommend one and say why in one line. Mention
  alternatives only if truly needed, one line each.
- Risks, blockers, or important caveats: flag in 1–2 simple sentences,
  prefixed **Risk:** / **Blocker:** / **Caveat:**. Never bury them.
- Detailed explanations only on explicit request ("explain", "why in detail",
  "walk me through").
- Missing information that materially changes the answer: ask one simple
  clarifying question instead of assuming. Otherwise state the assumption in
  one line and proceed.
- After finishing a task, report it like you would to a busy friend: what
  changed and whether it works, in 2–4 sentences. No step-by-step narration.

### Calibration

- Simple question → 1–2 sentences, no bullets, no headers.
- Task completion report → what changed, where, and that it was verified,
  in a few sentences.
- Long or multi-step work → outcome first, then only what affects the next
  decision. Everything else stays out.

## Marketing dashboard (living)

`marketing/dashboard.html` is the single source of truth for marketing-system
state — a self-contained page (open it in any browser) rendering an
interactive graph of channels, agents, workflows, KPIs, roadmap and changelog.

Any PR that changes marketing — new channel/workflow/campaign, milestone
shipped, KPI or roadmap movement, strategy change — MUST update it in the
same PR:

1. Edit only the `MARKETING_DATA` block in that file (`nodes` / `edges` /
   `kpis` / `pillars` / `roadmap` / `metrics` / `risks`).
2. Prepend a `changelog` entry and bump `meta.updated`.
3. The renderer below the data block is generic — never edit it for content
   changes; new nodes/lanes/edges lay out automatically.
4. Verify: open the file in a browser (or Playwright-screenshot it) — no
   console errors, graph renders, a node click opens the panel.

Keep the data honest: statuses are live / building / ready / planned / held,
and numbers are never invented (same rule as the copy playbook).

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. The
skill has multi-step workflows, checklists, and quality gates that produce better
results than an ad-hoc answer. When in doubt, invoke the skill. A false positive is
cheaper than a false negative.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke /office-hours
- Strategy, scope, "think bigger", "what should we build" → invoke /plan-ceo-review
- Architecture, "does this design make sense" → invoke /plan-eng-review
- Design system, brand, "how should this look" → invoke /design-consultation
- Design review of a plan → invoke /plan-design-review
- Developer experience of a plan → invoke /plan-devex-review
- "Review everything", full review pipeline → invoke /autoplan
- Clone/rebuild/reverse-engineer another website → invoke /clone-website (output
  goes to `clones/` only; see `clones/README.md`)
- Bugs, errors, "why is this broken", "wtf", "this doesn't work" → invoke /investigate
- Test the site, find bugs, "does this work" → invoke /qa (or /qa-only for report only)
- Code review, check the diff, "look at my changes" → invoke /review
- Visual polish, design audit, "this looks off" → invoke /design-review
- Developer experience audit, try onboarding → invoke /devex-review
- Ship, deploy, create a PR, "send it" → invoke /ship
- Merge + deploy + verify → invoke /land-and-deploy
- Configure deployment → invoke /setup-deploy
- Post-deploy monitoring → invoke /canary
- Update docs after shipping → invoke /document-release
- Weekly retro, "how'd we do" → invoke /retro
- Second opinion, codex review → invoke /codex
- Safety mode, careful mode, lock it down → invoke /careful or /guard
- Restrict edits to a directory → invoke /freeze or /unfreeze
- Upgrade gstack → invoke /gstack-upgrade
- Save progress, "save my work" → invoke /context-save
- Resume, restore, "where was I" → invoke /context-restore
- Security audit, OWASP, "is this secure" → invoke /cso
- Make a PDF, document, publication → invoke /make-pdf
- Launch real browser for QA → invoke /open-gstack-browser
- Import cookies for authenticated testing → invoke /setup-browser-cookies
- Performance regression, page speed, benchmarks → invoke /benchmark
- Review what gstack has learned → invoke /learn
- Tune question sensitivity → invoke /plan-tune
- Code quality dashboard → invoke /health

## gstack

Use the /browse skill from gstack for all web browsing. Never use the
mcp__claude-in-chrome__* tools.

Available gstack skills: /office-hours, /plan-ceo-review, /plan-eng-review,
/plan-design-review, /design-consultation, /design-shotgun, /design-html,
/review, /ship, /land-and-deploy, /canary, /benchmark, /browse,
/connect-chrome, /qa, /qa-only, /design-review, /setup-browser-cookies,
/setup-deploy, /setup-gbrain, /retro, /investigate, /document-release,
/document-generate, /codex, /cso, /autoplan, /plan-devex-review,
/devex-review, /careful, /freeze, /guard, /unfreeze, /gstack-upgrade, /learn
