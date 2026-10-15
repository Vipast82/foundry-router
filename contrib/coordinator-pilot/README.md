# Coordinator pilot (real Kilo + real Foundry, scripted models)

Runs the development coordinator end to end on two projects with a real
`kilo serve`, Foundry from this repository and git (local bare remotes only).
The models are **not real**: `mock_backends.py` plays Strata (a scripted coder
that drives Kilo's own write/read/bash tools) and Meridian (a scripted
reviewer on the Anthropic API).

```bash
npm pack @kilocode/cli-linux-x64@7.8.8 && tar xzf kilocode-cli-linux-x64-7.8.8.tgz   # or any kilo binary
python contrib/coordinator-pilot/run_pilot.py --kilo package/bin/kilo \
    --au /path/to/AnimalUprising-clone --out /tmp/pilot
```

- `fixture-game/`: a second game described only by a profile. Its
  `project.toml.template` is also the template for registering a new game.
- `au-sandbox/`: two tier-C pilot plans used in a *shared clone* of
  AnimalUprising with the proposed rule amendment applied there only.

Phases and results are written to `<out>/report.json`; the 2026-10-15 run is
summarised in docs/COORDINATOR_REPORT.md.
