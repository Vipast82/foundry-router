"""The general Roblox skill library (contrib/skills/roblox-*): every skill installs cleanly and stays
a short entry page with its references and licences (v0.181.16, 2026-11-02)."""
import os
import re

import pytest

from foundry_coordinator import skills_install as SI

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "contrib", "skills")
SKILLS = sorted(d for d in os.listdir(ROOT) if d.startswith("roblox-")
                and os.path.isdir(os.path.join(ROOT, d)))


def test_the_library_has_the_planned_skills():
    assert len(SKILLS) == 27
    index = open(os.path.join(ROOT, "ROBLOX_SKILLS.md"), encoding="utf-8").read()
    for name in SKILLS:
        assert f"`{name}`" in index, f"{name} missing from ROBLOX_SKILLS.md"


@pytest.mark.parametrize("name", SKILLS)
def test_each_skill_is_a_short_entry_page_with_references_and_notices(name):
    d = os.path.join(ROOT, name)
    text = open(os.path.join(d, "SKILL.md"), encoding="utf-8").read()
    front = re.match(r"^---\nname: (.+)\ndescription: (.+)\n---\n", text)
    assert front, f"{name}: frontmatter must be name + description"
    assert front.group(1).strip() == name
    assert 60 <= len(front.group(2)) <= 400
    assert len(text.encode("utf-8")) <= 4600, f"{name}: SKILL.md over the entry-page budget"
    refs = os.path.join(d, "references")
    assert os.path.isfile(os.path.join(refs, "THIRD_PARTY_NOTICES.md"))
    named = set(re.findall(r"`(?:references/)?([a-z0-9-]+\.md)`", text.split("## References")[-1]))
    assert named, f"{name}: no references listed"
    for f in named:
        assert os.path.isfile(os.path.join(refs, f)), f"{name}: listed reference {f} missing"
    # every copied file names its source and licence on its first line
    for f in os.listdir(refs):
        if f in ("THIRD_PARTY_NOTICES.md",):
            continue
        first = open(os.path.join(refs, f), encoding="utf-8").readline()
        if first.startswith("<!-- From "):
            assert "MIT" in first and "commit" in first


@pytest.mark.parametrize("name", SKILLS)
def test_the_installer_finds_each_skill(name):
    assert SI._ok_name(name)
    assert SI.find_source("anyproject", name) is not None


def test_contested_api_claims_follow_the_roblox_reference():
    """roblox-brain's lighting note says LightingStyle is not script-writable; the Roblox API
    reference says it is (only the deprecated Technology is not). The entry page states the
    reference's answer."""
    wb = open(os.path.join(ROOT, "roblox-world-building", "SKILL.md"), encoding="utf-8").read()
    assert "`Lighting.LightingStyle`" in wb and "can be set from scripts" in wb
    assert "`Lighting.Technology` is deprecated and cannot" in wb
