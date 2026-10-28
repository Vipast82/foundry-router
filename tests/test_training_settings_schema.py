"""2026-10-28: the project's training pipeline settings are typed controls in the
Automation settings editor (they were one opaque `training` JSON block)."""

from foundry_automation import schema as S


def test_training_pipeline_settings_are_typed_fields():
    want = {"training.enabled": "bool", "training.unbound_project": "str",
            "training.export.include_reasoning": "bool", "training.export.calls_quality": "enum",
            "training.auto.kinds": "list", "training.retention.raw_days": "int",
            "training.split.eval_fraction": "float", "training.capture.on_failure": "enum",
            "training.policy": "table"}
    for key, typ in want.items():
        f = S.find_field(key)
        assert f is not None and f.type == typ, key
    assert S.find_field("training") is None                  # no opaque catch-all any more
    assert S.check_value(S.find_field("training.export.calls_quality"), "maybe")
    assert S.check_value(S.find_field("training.export.calls_quality"), "ok") is None
    assert S.check_value(S.find_field("training.retention.raw_days"), 0)
    assert S.find_field("training.auto.key_env").scope == "local"
