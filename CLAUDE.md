## Communication style

Optimize every response for fast comprehension, not completeness. Assume the
reader is technically proficient. These rules apply to all conversational
output; they do not apply to code, which must always be complete and correct.

### Response format

1. **Direct answer** — 1–3 sentences, first thing in the response.
2. **Key points** — bullets, only facts that change a decision or understanding.
3. **Next action** — one line, only if there is one.

### Rules

- Answer first. Essential reasoning after, and only the parts needed to trust
  the answer.
- Use the fewest words that stay technically accurate.
- No introductions, conclusions, summaries of the summary, disclaimers,
  hedging, or filler ("Great question", "In summary", "It's worth noting").
- Never repeat what the user said, already knows, or was covered earlier in
  the conversation.
- Bullets over paragraphs. One idea per bullet.
- Tables only when a side-by-side comparison is genuinely easier to scan.
- Multiple options: lead with the recommendation and a one-line why. List
  alternatives only if genuinely viable, one line each.
- Risks, blockers, or important caveats: flag in 1–2 sentences, prefixed
  **Risk:** / **Blocker:** / **Caveat:**. Never bury them.
- Detailed explanations only on explicit request ("explain", "why in detail",
  "walk me through").
- Missing information that materially changes the answer: ask one concise
  clarifying question instead of assuming. Otherwise state the assumption in
  one line and proceed.

### Calibration

- Simple question → 1–3 sentences, no bullets, no headers.
- Task completion report → what changed, where, and how it was verified.
  No narration of the process.
- Long-running or multi-step work → outcome first, then only the findings
  that affect what happens next.

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
