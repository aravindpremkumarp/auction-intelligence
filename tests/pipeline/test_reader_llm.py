"""The structured client with a scripted fake OpenAI client: tier fallback,
truncation, one repair round, usage accounting."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from pipeline.reader import llm as L


class _Out(BaseModel):
    reserve: str
    village: str | None = None


def _resp(text, finish="stop", usage=(100, 20)):
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1],
                              prompt_tokens_details=None))


class _Fake:
    """Answers per call from a script; records every request's kwargs."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_strict_tier_used_when_supported():
    fake = _Fake([_resp('{"reserve": "Rs.1"}')])
    r = L.call_structured("m", "sys", "usr", _Out, client=fake, reasoning_off=True,
                          provider_order=["deepseek"])
    assert r.tier == "json_schema" and r.parsed.reserve == "Rs.1"
    kw = fake.calls[0]
    assert kw["temperature"] == 0 and kw["seed"] == L.DEFAULT_SEED
    assert kw["response_format"]["type"] == "json_schema"
    assert kw["response_format"]["json_schema"]["strict"] is True
    assert kw["extra_body"] == {"reasoning": {"enabled": False},
                                "provider": {"order": ["deepseek"], "allow_fallbacks": True}}
    assert r.usage == {"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 0}


def test_falls_back_a_tier_when_response_format_is_rejected():
    fake = _Fake([RuntimeError("400 response_format json_schema not supported"),
                  _resp('```json\n{"reserve": "Rs.2"}\n```')])
    r = L.call_structured("m", "s", "u", _Out, client=fake, reasoning_off=False)
    assert r.tier == "json_object" and r.parsed.reserve == "Rs.2"
    assert fake.calls[1]["response_format"] == {"type": "json_object"}


def test_other_errors_are_raised_not_swallowed():
    fake = _Fake([RuntimeError("401 unauthorized")])
    with pytest.raises(RuntimeError, match="401"):
        L.call_structured("m", "s", "u", _Out, client=fake, reasoning_off=False)


def test_truncation_raises():
    fake = _Fake([_resp('{"reserve": "Rs', finish="length")])
    with pytest.raises(L.Truncated):
        L.call_structured("m", "s", "u", _Out, client=fake, reasoning_off=False, max_tokens=50)


def test_one_repair_round_then_next_tier():
    fake = _Fake([_resp('{"nope": 1}'),                       # strict: invalid
                  _resp('{"reserve": "Rs.3"}'),               # repair succeeds
                  ])
    r = L.call_structured("m", "s", "u", _Out, client=fake, reasoning_off=False)
    assert r.repaired and r.parsed.reserve == "Rs.3" and r.tier == "json_schema"
    assert "did not match the schema" in fake.calls[1]["messages"][-1]["content"]
    assert r.usage["prompt_tokens"] == 200


def test_unparseable_after_every_tier():
    fake = _Fake([_resp("x")] * 6)
    with pytest.raises(L.Unparseable):
        L.call_structured("m", "s", "u", _Out, client=fake, reasoning_off=False)


def test_strip_fences():
    assert L.strip_fences('```json\n{"a":1}\n```') == '{"a":1}'
    assert L.strip_fences('Sure: {"a":1} done') == '{"a":1}'


def test_usage_is_priced_by_the_model_that_served_it():
    from pipeline.langextract_run import Usage
    from types import SimpleNamespace as NS
    u = Usage()
    u.add_openai(NS(prompt_tokens=1_000_000, completion_tokens=1_000_000, prompt_tokens_details=None),
                 "deepseek/deepseek-v4.1-flash")
    assert abs(u.est_cost - (0.027 + 0.60)) < 1e-9
    u.add_openai(NS(prompt_tokens=1_000_000, completion_tokens=0, prompt_tokens_details=None), "unknown/model")
    assert abs(u.est_cost - (0.027 + 0.60 + 0.30)) < 1e-9          # falls back to the old default
    assert abs(L.call_cost("deepseek/deepseek-v4.1-flash",
                           {"prompt_tokens": 2_000_000, "completion_tokens": 0, "cached_tokens": 1_000_000})
               - 0.054) < 1e-9
