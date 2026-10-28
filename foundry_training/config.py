"""Per-project training configuration: the `[training]` section of the same
project.toml the coordinator uses (the pipeline reads only `id`,
`workspace.path`, `roles` and `[training]`; it never imports the coordinator).

[training]
enabled = true
sources = ["kilo", "foundry", "coordinator", "git"]   # + "cline"
artifact_roots = ["docs/evidence"]        # images may be read only from here
allowed_email_domains = []
local_models = ["qwen3.8-flash-next-iq3_s"]   # authorship: what counts as the local model
local_personas = ["Foundry-Kilo-Local"]
unbound_project = ""                      # e.g. "foundry-unbound": Foundry captures with no
                                          # project (Cline, Chat, AnythingLLM, brain) go to this
                                          # pseudo-project; they feed only the calls kind

[training.policy.sft]                     # one table per dataset type
require = ["verification_pass", "review_approved_at_revision", "complete_tool_sequence",
           "system_and_tools_known", "local_author", "no_loop"]
max_tokens = 32768
[training.policy.correction]
require = ["verification_pass", "review_approved_at_revision", "feedback_present"]
[training.policy.preference]
require = ["same_context", "justified"]
[training.split]
eval_fraction = 0.15
seed = "v1"
heldout_projects = []
time_cutoff = ""                          # ISO date: tasks first seen at/after it go to eval
[training.retention]
raw_days = 30
normalized_days = 180
[training.export]
targets = ["canonical", "chatml-tools"]
allow_frontier_targets = false            # provider terms: see docs/TRAINING_DATA.md
include_reasoning = false                 # true: target turns carry reasoning_content
calls_quality = "verified"                # calls kind: "verified" | "ok" (router_ok)
calls_purposes = ["agent_turn"]           # calls kind: which capture purposes
[training.capture]
on_failure = "warn"                       # coordinator: ignore | warn | hold
[training.tags]
widgets = []                              # extra widget names to tag (labels.KNOWN_WIDGETS)

Read from outside [training]: `[fast_lane].tasks` (episodes of those tasks are
tagged fast_lane; a fast_lane table with enabled = false tags nothing).
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass, field

DEFAULT_REQUIRE = {
    "sft": ["verification_pass", "review_approved_at_revision", "complete_tool_sequence",
            "system_and_tools_known", "local_author", "no_loop", "approval_if_required"],
    "correction": ["verification_pass", "review_approved_at_revision", "feedback_present",
                   "local_author", "approval_if_required"],
    "preference": ["same_context", "justified"],
    "multimodal": ["verification_pass", "artifacts_present", "approval_if_required"],
}


@dataclass
class TrainingProject:
    id: str
    workspace: str
    path: str
    enabled: bool = True
    sources: list = field(default_factory=lambda: ["kilo", "foundry", "coordinator", "git"])
    artifact_roots: list = field(default_factory=list)
    allowed_email_domains: list = field(default_factory=list)
    local_models: list = field(default_factory=list)
    local_personas: list = field(default_factory=list)
    policy: dict = field(default_factory=dict)
    split: dict = field(default_factory=dict)
    retention: dict = field(default_factory=dict)
    export: dict = field(default_factory=dict)
    capture: dict = field(default_factory=dict)
    fast_lane_tasks: list = field(default_factory=list)
    tags: dict = field(default_factory=dict)
    unbound_project: str = ""
    unbound: bool = False               # the pseudo-project of unbound Foundry captures

    def require(self, kind: str) -> list[str]:
        return list((self.policy.get(kind) or {}).get("require") or DEFAULT_REQUIRE.get(kind, []))

    def policy_hash(self) -> str:
        blob = json.dumps({"policy": self.policy, "split": self.split, "export": self.export,
                           "local": [self.local_models, self.local_personas]}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def artifact_dirs(self) -> list[str]:
        return [os.path.join(self.workspace, r) for r in self.artifact_roots]


def load(path: str) -> TrainingProject:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    t = raw.get("training") or {}
    ws = (raw.get("workspace") or {}).get("path") or ""
    ws = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(path)),
                                      os.path.expandvars(os.path.expanduser(ws))))
    roles = raw.get("roles") or {}
    fl = raw.get("fast_lane") if isinstance(raw.get("fast_lane"), dict) else {}
    return TrainingProject(
        id=str(raw.get("id") or ""), workspace=ws, path=os.path.abspath(path),
        enabled=bool(t.get("enabled", False)),
        sources=list(t.get("sources") or ["kilo", "foundry", "coordinator", "git"]),
        artifact_roots=list(t.get("artifact_roots") or []),
        allowed_email_domains=list(t.get("allowed_email_domains") or []),
        local_models=list(t.get("local_models") or []),
        local_personas=list(t.get("local_personas") or ([roles["implementer"]]
                                                          if roles.get("implementer") else [])),
        policy=dict(t.get("policy") or {}), split=dict(t.get("split") or {}),
        retention=dict(t.get("retention") or {}), export=dict(t.get("export") or {}),
        capture=dict(t.get("capture") or {}),
        fast_lane_tasks=[str(x) for x in (fl.get("tasks") or [])]
        if fl.get("enabled", True) else [],
        tags=dict(t.get("tags") or {}),
        unbound_project=str(t.get("unbound_project") or ""))


def unbound_of(p: TrainingProject) -> TrainingProject:
    """The pseudo-project for Foundry captures no coordinator project bound,
    declared by `p` (its authorship, export, split and auto settings)."""
    import dataclasses
    return dataclasses.replace(p, id=p.unbound_project, workspace="", sources=["foundry"],
                               artifact_roots=[], fast_lane_tasks=[], unbound_project="",
                               unbound=True)


def find(paths: list[str]) -> list[TrainingProject]:
    out = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                cand = os.path.join(p, name, "project.toml")
                if os.path.isfile(cand):
                    out.append(load(cand))
        elif p:
            out.append(load(p))
    ids = [p.id for p in out]
    if len(ids) != len(set(ids)):
        raise ValueError("two project profiles share an id")
    return out


def by_workspace(projects: list[TrainingProject], directory: str):
    """The project whose workspace contains `directory` (exact path match on
    the configured workspace; never a fuzzy match)."""
    d = os.path.normcase(os.path.realpath(directory or ""))
    for p in projects:
        root = os.path.normcase(os.path.realpath(p.workspace))
        if d == root or d.startswith(root + os.sep):
            return p
    return None
