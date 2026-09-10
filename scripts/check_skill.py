#!/usr/bin/env python3
"""Pre-flight check for the awesome-phone-call-agents submission.

    python scripts/check_skill.py

Mirrors the rules that `scripts/validate_repository.py` enforces in the
target repository, so a PR fails here -- in a second, locally -- rather
than in their CI after it is opened.

It is a *subset*: it checks the rules that apply to a single contributed
skill, not the repository-wide ones (their README title, their required
top-level docs) which only their own tree can satisfy.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"

DIR_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
# Chinese, Japanese and Korean ranges are prohibited in repo-facing docs.
CJK = re.compile(r"[一-鿿぀-ヿ가-힯]")
LOCAL_REF = re.compile(r"`((?:references|scripts|assets)/[^`]+)`")
ACTIONABLE = ("read", "use", "consult", "run", "see", "follow")
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.net", "example.org")
EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.[\w.-]+)")

REQUIRED_REFERENCES = ("safety.md", "examples.md")
MIN_DESCRIPTION = 40

failures: list[str] = []
notes: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


def check_skill(skill: Path) -> None:
    name = skill.name

    if not DIR_NAME.match(name):
        fail(f"{name}: directory must be lowercase kebab-case")

    md = skill / "SKILL.md"
    if not md.is_file():
        fail(f"{name}: SKILL.md is missing")
        return

    if (skill / "README.md").is_file():
        # Only outbound-call-skill-creator is exempt upstream.
        fail(f"{name}: a skill must not carry a top-level README.md")

    text = md.read_text(encoding="utf-8")

    m = FRONTMATTER.match(text)
    if not m:
        fail(f"{name}: SKILL.md has no YAML frontmatter")
        return
    block = m.group(1)

    fm: dict[str, str] = {}
    for line in block.split("\n"):
        if ":" in line and not line.startswith((" ", "\t", "-")):
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()

    if fm.get("name") != name:
        fail(f"{name}: frontmatter name {fm.get('name')!r} must equal the directory name")

    desc = fm.get("description", "")
    if len(desc) < MIN_DESCRIPTION:
        fail(f"{name}: description is {len(desc)} chars, needs >= {MIN_DESCRIPTION}")
    if not re.search(r"\b(phone|call)\b", desc, re.IGNORECASE):
        fail(f"{name}: description must mention a phone or call workflow")

    refs = skill / "references"
    for required in REQUIRED_REFERENCES:
        if not (refs / required).is_file():
            fail(f"{name}: references/{required} is required")

    # Every local path mentioned must exist, and be mentioned with an
    # actionable verb nearby.
    for path_str in set(LOCAL_REF.findall(text)):
        target = skill / path_str.lstrip("./")
        if not target.exists():
            fail(f"{name}: SKILL.md references {path_str} which does not exist")
            continue
        for line in text.split("\n"):
            if f"`{path_str}`" in line:
                if not any(v in line.lower() for v in ACTIONABLE):
                    notes.append(
                        f"{name}: mention of {path_str} has no actionable verb "
                        f"({'/'.join(ACTIONABLE)})"
                    )
                break

    for f in sorted(skill.rglob("*")):
        if not f.is_file() or f.suffix not in {".md", ".py", ".json", ".mjs", ".ts"}:
            continue
        rel = f.relative_to(skill)
        body = f.read_text(encoding="utf-8")

        if CJK.search(body):
            fail(f"{name}/{rel}: CJK characters are prohibited")

        for i, line in enumerate(body.split("\n"), start=1):
            if line != line.rstrip():
                fail(f"{name}/{rel}:{i}: trailing whitespace")
                break

        for domain in set(EMAIL.findall(body)):
            if domain.lower() not in ALLOWED_EMAIL_DOMAINS:
                fail(f"{name}/{rel}: email domain {domain} is not an example domain")

        if f.suffix == ".json":
            try:
                json.loads(body)
            except json.JSONDecodeError as exc:
                fail(f"{name}/{rel}: invalid JSON ({exc})")


def main() -> int:
    if not SKILLS_DIR.is_dir():
        print("no skills/ directory", file=sys.stderr)
        return 1

    skills = sorted(d for d in SKILLS_DIR.iterdir() if d.is_dir())
    if not skills:
        print("skills/ contains no skill", file=sys.stderr)
        return 1

    for skill in skills:
        check_skill(skill)

    for note in notes:
        print(f"note: {note}")
    for f in failures:
        print(f"FAIL: {f}")

    if failures:
        print(f"\n{len(failures)} problem(s). Fix before opening the PR.")
        return 1

    print(f"OK: {len(skills)} skill(s) satisfy the submission rules.")
    if notes:
        print(f"({len(notes)} note(s) above are advisory.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
