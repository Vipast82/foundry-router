"""W2.5: one source for the automation defaults. The coordinator's
[automation] module re-exports foundry_automation.schema; the limits the
engine falls back to equal the schema's defaults; the GUI shows the values
that are in force."""

import pytest

from foundry_automation import schema as S
from foundry_coordinator import automation as A


def test_automation_defaults_are_the_schema_defaults():
    assert A.AUTOMATION_DEFAULTS is S.AUTOMATION_DEFAULTS
    assert A.DEFAULT_SCOPE_RULES is S.DEFAULT_SCOPE_RULES
    assert A.HELPER_MODES == S.HELPER_MODES and A.SCOPE_DEFAULTS == S.SCOPE_DEFAULTS
    assert A.defaults() == S.automation_table("x", None)
    assert A.defaults() is not A.defaults()                       # a fresh copy each time
    assert A.defaults()["lead_hard_max_calls"] == S.AUTOMATION_DEFAULTS["lead_hard_max_calls"]
    # every [automation] key is described once, in the field registry
    for k in S.AUTOMATION_DEFAULTS:
        f = S.find_field(f"automation.{k}")
        assert f is not None and f.default == S.AUTOMATION_DEFAULTS[k], k


def test_validator_parity_including_errors():
    raw = {"helpers": "proactive", "subagents_allowed": ["explore", "au-lint"],
           "scope_auto_approve": [{"path": "docs/{task}_gate.json"}],
           "lead_hard_max_calls": 12}
    assert A.table("p", raw) == S.automation_table("p", raw)
    for bad in ({"helpers": "sometimes"}, {"bogus": 1}, {"lead_hard_max_calls": -1},
                {"scope_auto_approve": [{"path": "../x"}]}):
        with pytest.raises(A.AutomationError) as ea:
            A.table("p", bad)
        with pytest.raises(S.SettingsError) as es:
            S.automation_table("p", bad)
        assert str(ea.value) == str(es.value) and ea.value.path == es.value.path
        assert isinstance(ea.value, S.SettingsError)            # profile.py catches either


def test_max_review_calls_is_four_everywhere():
    from foundry_coordinator import obligations as O
    f = S.find_field("limits.max_review_calls")
    assert f is not None and f.default == 4
    assert O.LIMITS["review_calls"] == ("review_calls", "max_review_calls", 4)
    import inspect
    from foundry_coordinator import engine, telemetry
    assert 'limit("max_review_calls", 4)' in inspect.getsource(engine)
    assert '("max_review_calls", 4)' in inspect.getsource(telemetry)


def test_schema_limit_defaults_match_the_engine_fallbacks():
    from foundry_coordinator import obligations as O
    for _, (_, key, default) in O.LIMITS.items():
        if not key or "." in key:
            continue
        f = S.find_field(f"limits.{key}")
        if f is not None:
            assert f.default == default, key
