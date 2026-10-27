"""2026-10-27: a gate run through AU's command runner (py -3 tools/guard/run.py ... -- <cmd>)
is a gate run, not a file read. Every GF-GATE1 gate run went through it and was excluded as
read_only_command, so the gate obligation could never be verified and the sprint looped."""

from foundry_coordinator.projects.animaluprising import adapter as A

GATE = (r"py -3 tools\guard\run.py --name gate_a7r_finish --timeout 600 --tail 30 -- "
        r"pwsh -NoProfile -File tools/gate.ps1 -Sprint GF-GATE1 -Files tools/gate.ps1,"
        r"tests/GateRunner.lua -Finish")


def test_a_gate_run_through_the_runner_is_a_verdict_source():
    assert A._shell_runs(GATE) == {"gate"}
    assert A._source_exclusion("bash", GATE, "gate") is None
    assert A._shell_runs(r"python tools/guard/run.py -- lune run tests/lune/run.luau") == {"lune"}


def test_reading_the_runners_log_is_still_a_read():
    assert A._shell_runs(r"py -3 tools\guard\run.py --last") == set()
    assert A._source_exclusion("bash", r"py -3 tools\guard\run.py --last", "gate") \
        == "read_only_command"
    log = r"Get-Content -LiteralPath tmp\guard\cmd\20261027-174741_gate_a7r_finish.log -Tail 40"
    assert A._source_exclusion("bash", log, "gate") == "read_only_command"
    # a runner wrapping a file read is still a read
    assert A._source_exclusion("bash", r"py -3 tools\guard\run.py --name r -- cat tmp\x.log",
                               "gate") == "read_only_command"
