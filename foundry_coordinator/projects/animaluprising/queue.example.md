# AnimalUprising: approving a pilot queue (example, not approved)

The coordinator only runs what is approved by name. Nothing here is approved.

Start with a **non-visual** sprint whose plan exists, after `doctor` is clean and the rule
amendment is applied:

```powershell
$env:FOUNDRY_COORD_PROFILES = "C:\Users\shock\OneDrive\Documents\ClaudeCode\Foundry-Router\foundry_coordinator\projects"
py -3 -m foundry_coordinator doctor animaluprising
py -3 -m foundry_coordinator queue approve animaluprising <ID> --by victor --note "pilot 1"
py -3 -m foundry_coordinator queue approve animaluprising <ID2> --deps <ID> --by victor --note "pilot 2"
py -3 -m foundry_coordinator run --until-settled
```

As of 2026-10-15 the active sprint is UI-01 (tier B, visual, session 12, step 3–4 of 8): it
needs Studio, captures and Victor's visual approval, so it is not a pilot candidate. Use the
current `docs/MASTER_ORDER.md` and the plans in `docs/progress/` to pick the pilot, not this
note.
