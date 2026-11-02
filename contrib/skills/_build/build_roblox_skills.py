"""Assemble Foundry's generic Roblox skill library (contrib/skills/roblox-*) from three upstream
libraries: each skill's references/ get the upstream deep references with a provenance header,
and a THIRD_PARTY_NOTICES.md. SKILL.md files are written by hand and are not touched here.

usage: python build_skills.py <upstream_dir> <foundry_repo>
"""
import os
import re
import shutil
import sys

UP, REPO = sys.argv[1], sys.argv[2]
EL4 = os.path.join(UP, "EL4CTEO_roblox-skills", "skills")
BRAIN = os.path.join(UP, "TabooHarmony_roblox-brain", "skills")
OUT = os.path.join(REPO, "contrib", "skills")

EL4_URL = "https://github.com/EL4CTEO/roblox-skills"
BRAIN_URL = "https://github.com/TabooHarmony/roblox-brain"
EL4_SHA = sys.argv[3] if len(sys.argv) > 3 else ""
BRAIN_SHA = sys.argv[4] if len(sys.argv) > 4 else ""

# target skill -> list of (source, upstream skill path, reference file name)
# brain: copy references/full.md; el4: copy the SKILL.md body (frontmatter dropped) + its references/
MAP = {
    "roblox-luau": [("brain", "core/roblox-luau-core", "luau-core.md"),
                    ("brain", "core/roblox-luau-types", "luau-types.md"),
                    ("brain", "core/roblox-luau-patterns", "luau-patterns.md"),
                    ("el4", "roblox-luau", "luau-quick.md")],
    "roblox-architecture": [("brain", "core/roblox-architecture", "architecture.md"),
                            ("el4", "roblox-architecture", "architecture-quick.md")],
    "roblox-networking": [("brain", "core/roblox-networking", "networking.md"),
                          ("el4", "roblox-networking", "networking-quick.md")],
    "roblox-security": [("brain", "core/roblox-security", "security.md"),
                        ("el4", "roblox-security", "security-quick.md")],
    "roblox-data": [("brain", "core/roblox-data", "player-data.md"),
                    ("brain", "core/roblox-server-data", "server-data.md"),
                    ("el4", "roblox-data-stores", "data-stores-quick.md")],
    "roblox-performance": [("brain", "core/roblox-performance", "performance.md"),
                           ("el4", "roblox-performance", "performance-quick.md")],
    "roblox-world-building": [("brain", "gameplay/roblox-building", "building.md"),
                              ("brain", "gameplay/roblox-lighting", "lighting.md"),
                              ("el4", "roblox-world-building", "world-building-quick.md")],
    "roblox-physics": [("brain", "gameplay/roblox-physics", "physics.md"),
                       ("el4", "roblox-physics", "physics-quick.md")],
    "roblox-npc-ai": [("brain", "gameplay/roblox-npc-ai", "npc-ai.md"),
                      ("el4", "roblox-npc-ai", "npc-ai-quick.md")],
    "roblox-camera": [("brain", "gameplay/roblox-camera", "camera.md")],
    "roblox-input": [("brain", "gameplay/roblox-input", "input.md"),
                     ("el4", "roblox-input", "input-quick.md")],
    "roblox-ui": [("brain", "gameplay/roblox-gui", "gui.md"),
                  ("el4", "roblox-ui", "ui-quick.md")],
    "roblox-animation-vfx": [("brain", "gameplay/roblox-animation-vfx", "animation-vfx.md"),
                             ("el4", "roblox-characters-animation", "characters-quick.md")],
    "roblox-audio": [("brain", "gameplay/roblox-audio", "audio.md"),
                     ("el4", "roblox-audio", "audio-quick.md")],
    "roblox-localization": [("brain", "gameplay/roblox-localization", "localization.md")],
    "roblox-text-chat": [("el4", "roblox-text-chat", "text-chat.md")],
    "roblox-teleport-matchmaking": [("el4", "roblox-teleport-matchmaking", "teleport.md")],
    "roblox-game-systems": [("el4", "roblox-game-systems", "game-systems.md")],
    "roblox-game-design": [("brain", "design/roblox-game-design", "game-design.md"),
                           ("brain", "design/roblox-player-psychology", "player-psychology.md"),
                           ("brain", "design/roblox-growth-design", "growth.md")],
    "roblox-monetization": [("brain", "design/roblox-monetization", "monetization.md"),
                            ("el4", "roblox-monetization", "monetization-quick.md")],
    "roblox-analytics-liveops": [("brain", "design/roblox-analytics", "analytics.md"),
                                 ("el4", "roblox-analytics-liveops", "liveops-quick.md")],
    "roblox-testing": [("el4", "roblox-testing", "testing.md")],
    "roblox-code-review": [("el4", "roblox-code-review", "code-review.md")],
    "roblox-tooling": [("brain", "tools/roblox-tooling", "tooling.md"),
                       ("el4", "roblox-tooling", "tooling-quick.md")],
    "roblox-studio-mcp": [("brain", "tools/roblox-studio-mcp", "studio-mcp.md"),
                          ("el4", "roblox-studio-mcp", "studio-mcp-quick.md")],
    "roblox-open-cloud": [("brain", "tools/roblox-cloud", "open-cloud.md"),
                          ("el4", "roblox-open-cloud", "open-cloud-quick.md")],
    "roblox-publish-checklist": [("brain", "tools/roblox-publish-checklist", "publish-checklist.md")],
}

SYPHO_CREDIT = {
    "roblox-world-building": "`references/map-design-notes.md`",
}

MIT_EL4 = open(os.path.join(UP, "EL4CTEO_roblox-skills", "LICENSE"), encoding="utf-8").read()
MIT_BRAIN = open(os.path.join(UP, "TabooHarmony_roblox-brain", "LICENSE"), encoding="utf-8").read()


def body(md: str) -> str:
    """Drop YAML frontmatter."""
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end != -1:
            return md[end + 4:].lstrip("\n")
    return md


def header(src: str, path: str) -> str:
    if src == "brain":
        return (f"<!-- From TabooHarmony/roblox-brain ({BRAIN_URL}, commit {BRAIN_SHA}), "
                f"skills/{path}, MIT (see THIRD_PARTY_NOTICES.md). Deep reference; the SKILL.md "
                f"entry and any project rules win where they differ. -->\n\n")
    return (f"<!-- From EL4CTEO/roblox-skills ({EL4_URL}, commit {EL4_SHA}), skills/{path}, MIT "
            f"(see THIRD_PARTY_NOTICES.md). Deep reference; the SKILL.md entry and any project "
            f"rules win where they differ. -->\n\n")


def build(name: str, parts: list) -> list:
    d = os.path.join(OUT, name, "references")
    os.makedirs(d, exist_ok=True)       # hand-written references (no provenance header) are kept
    made, used = [], set()
    for src, path, fname in parts:
        used.add(src)
        if src == "brain":
            text = open(os.path.join(BRAIN, path, "references", "full.md"), encoding="utf-8").read()
            with open(os.path.join(d, fname), "w", encoding="utf-8", newline="\n") as f:
                f.write(header(src, path + "/references/full.md") + text)
            made.append(fname)
        else:
            base = os.path.join(EL4, path)
            text = body(open(os.path.join(base, "SKILL.md"), encoding="utf-8").read())
            with open(os.path.join(d, fname), "w", encoding="utf-8", newline="\n") as f:
                f.write(header(src, path + "/SKILL.md") + text)
            made.append(fname)
            refs = os.path.join(base, "references")
            if os.path.isdir(refs):
                for r in sorted(os.listdir(refs)):
                    stem = os.path.splitext(fname)[0].replace("-quick", "")
                    out = f"{stem}-{r}"
                    t = open(os.path.join(refs, r), encoding="utf-8").read()
                    with open(os.path.join(d, out), "w", encoding="utf-8", newline="\n") as f:
                        f.write(header(src, f"{path}/references/{r}") + t)
                    made.append(out)
    notice = ["# Third-party notices", "",
              "Files in this folder other than this notice are copied from the libraries below "
              "(with a one-line provenance comment added at the top), under the MIT License. "
              "The skill's `SKILL.md` is Foundry's own text.", ""]
    if "brain" in used:
        notice += [f"## TabooHarmony/roblox-brain", f"{BRAIN_URL} (commit {BRAIN_SHA})", "",
                   "```", MIT_BRAIN.strip(), "```", ""]
    if "el4" in used:
        notice += [f"## EL4CTEO/roblox-skills", f"{EL4_URL} (commit {EL4_SHA})", "",
                   "```", MIT_EL4.strip(), "```", ""]
    if name in SYPHO_CREDIT:
        notice += ["## SyphoDev Roblox skills (ideas only)",
                   "https://github.com/SyphoDev-yt/roblox-ai-skills - credit: the SyphoDev YouTube "
                   "channel. No file is copied: " + SYPHO_CREDIT[name] + " is Foundry's own summary "
                   "of ideas from that free library (its licence allows free sharing with a link "
                   "back to the source, and forbids selling).", ""]
    with open(os.path.join(d, "THIRD_PARTY_NOTICES.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(notice))
    return made


if __name__ == "__main__":
    for name, parts in MAP.items():
        made = build(name, parts)
        print(f"{name:30} {', '.join(made)}")
