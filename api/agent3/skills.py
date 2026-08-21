"""
api/agent3/skills.py
--------------------
On-demand skill loading, written here rather than taken from
`deepagents.middleware.SkillsMiddleware`.

That was measured, not assumed. Against deepagents 0.7.7:

- **+36 MB RSS.** `create_agent` alone imports at 68.3 MB; adding
  `SkillsMiddleware` takes it to 104.5 MB — identical to importing all of
  `deepagents`, because its package `__init__` pulls `create_deep_agent`,
  the filesystem middleware and the subagent middleware no matter which
  submodule you ask for.
- **It requires the filesystem tools we exist to drop.** `backend` is a
  REQUIRED constructor argument and the middleware binds no tools of its
  own (`hasattr(SkillsMiddleware, "tools")` is False). It loads a skill by
  instructing the model to call `read_file` on a path. Binding it therefore
  means binding `ls`, `read_file`, `write_file`, `edit_file`, `delete`,
  `glob`, `grep` and `execute` — measured at **~2,611 tokens** of schema in
  every prompt.
- **Plus 464 tokens** of its own default system prompt, most of it
  irrelevant here (a quantum-computing example, "Executing Skill Scripts",
  Deepagents-vs-Agents source labelling).
- **Plus one model call per skill load**, since progressive disclosure goes
  through a tool call rather than direct injection.

That is ~3,075 tokens per turn against a 676-token core instruction file —
4.5x the whole prompt step 3 shrank. So the mechanism here is deliberately
dumber and cheaper: match a trigger, read the file, inject the text. No tool
is bound, no round-trip is spent, and a skill costs nothing on the turns
that do not use it.

`docs/auction-deep-agent-2026-08.md` §6 originally specified
`SkillsMiddleware`; that line is corrected in the same commit as this file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent / "skills"

#: Cap on injected skill text per turn. Two skills is already ~1,400 tokens;
#: beyond that the agent is better served asking a narrower question than
#: reading more manuals.
MAX_SKILLS_PER_TURN = 2


@dataclass(frozen=True)
class Skill:
    name: str
    path: Path
    #: Lowercase substrings that make this skill relevant. Matched against
    #: the user's message, NOT against tool output — a skill is chosen by
    #: what was asked, before any tool has run.
    triggers: tuple[str, ...]
    _text: list[str] = field(default_factory=list, compare=False, repr=False)

    def text(self) -> str:
        """Read once per process. Skill files are static repo content."""
        if not self._text:
            self._text.append(self.path.read_text(encoding="utf-8"))
        return self._text[0]


#: The triggers, kept next to the skills they load rather than in the prompt.
#: These mirror the routing table in instructions.md — the drift test asserts
#: every skill here has a file and vice versa.
_TRIGGERS: dict[str, tuple[str, ...]] = {
    "diligence": (
        "everything about", "tell me all", "due diligence", "diligence",
        "should i worry", "should i bid", "safe to buy", "safe bid",
        "walk me through", "full detail", "full report", "dossier",
        "any risk", "risks",
    ),
    "extent": (
        "how big", "how large", "square feet", "sq ft", "sqft", "sq.ft",
        "acre", "cent", "ground", "hectare", "area of", "extent",
        "size of", "how many cents", "convert",
    ),
    "identifiers": (
        "survey number", "survey no", "s.no", "patta", "door number",
        "door no", "plot number", "plot no", "cersai", "chitta", "khata",
        "assessment number", "identifier",
    ),
}

#: A bare number-with-slash ("331/1", "45-2A") is how survey numbers arrive
#: even when the user never says the word "survey".
_SURVEY_SHAPED = re.compile(r"\b\d{1,4}\s*[/-]\s*\d{1,4}[A-Za-z]?\b")


def available_skills() -> dict[str, Skill]:
    """Every skill with a SKILL.md on disk, keyed by directory name."""
    out: dict[str, Skill] = {}
    if not SKILLS_DIR.is_dir():
        return out
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        name = path.parent.name
        out[name] = Skill(name=name, path=path,
                          triggers=_TRIGGERS.get(name, (name,)))
    return out


def select_skills(question: str, limit: int = MAX_SKILLS_PER_TURN) -> list[Skill]:
    """Which skills this question needs, most specific first.

    Deliberately a substring match rather than a model call: choosing a skill
    with an LLM would cost exactly the round-trip this design removes, and a
    wrong choice is cheap (a few hundred wasted tokens) while a wrong
    *answer* is not.
    """
    text = (question or "").lower()
    if not text.strip():
        return []
    skills = available_skills()
    scored: list[tuple[int, str, Skill]] = []
    for name, skill in skills.items():
        hits = sum(1 for t in skill.triggers if t in text)
        if name == "identifiers" and _SURVEY_SHAPED.search(question or ""):
            hits += 1
        if hits:
            scored.append((hits, name, skill))
    # Most trigger hits first; name as a tiebreak so selection is
    # deterministic — a prompt that varies run to run cannot be cached.
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [s[2] for s in scored[:limit]]


def render_skills(skills: list[Skill]) -> str:
    """The block appended to a turn's context, or "" when nothing matched."""
    if not skills:
        return ""
    parts = [
        "The following reference material was loaded for this question. "
        "Follow it where it applies; it does not override your core rules."
    ]
    parts.extend(s.text().strip() for s in skills)
    return "\n\n---\n\n".join(parts)
