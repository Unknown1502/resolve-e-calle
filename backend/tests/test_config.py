"""Configuration contract.

``buyer_company`` gates whether the agent may place a live call at all
(disclosure_ready / caller-identity requirement E). This is the first
place a wrong environment-variable name would surface, and it already
did once during development: ``.env.example`` briefly documented
``RESOLVE_E_BUYER_COMPANY`` when pydantic-settings actually reads
``BUYER_COMPANY`` (no ``env_prefix`` is configured). That mismatch would
have silently left ``buyer_company`` empty -- which fails closed here,
but for the wrong reason, and would have cost real debugging time on
whoever followed the example file.
"""

from __future__ import annotations

import pathlib

import pytest

from app.config import Settings


class TestBuyerCompanyEnvVar:
    def test_the_actual_env_var_name_is_buyer_company(self, monkeypatch) -> None:
        monkeypatch.setenv("BUYER_COMPANY", "Contoso Manufacturing")
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.buyer_company == "Contoso Manufacturing"

    def test_env_example_documents_the_real_name_not_a_guess(self) -> None:
        # Regression for the exact mismatch described above.
        example = pathlib.Path(__file__).resolve().parents[2] / ".env.example"
        text = example.read_text(encoding="utf-8")
        assert "BUYER_COMPANY=" in text
        assert "RESOLVE_E_BUYER_COMPANY" not in text

    def test_the_wrong_name_does_not_reappear_anywhere_live(self) -> None:
        """The first fix only covered `.env.example`.

        Two more copies of the same wrong name were still live in
        `cli.py`'s printed error and `call_orchestrator.py`'s logged one
        -- found during a later release-verification pass, not by this
        test, because this test didn't exist yet to catch them. It does
        now: scan every source and doc file in the repo, not just the
        one place the bug happened to be noticed first.

        Deliberately excludes this test file itself (which must say the
        wrong name once, as a literal, to assert its absence elsewhere)
        and the historical verification/decision-log docs, which
        correctly describe the bug in past tense as something that was
        found and fixed -- rewriting those to omit the wrong name would
        erase the record of the mistake, not the mistake itself.
        """
        root = pathlib.Path(__file__).resolve().parents[2]
        exempt = {
            pathlib.Path(__file__).resolve(),
            root / "docs" / "verification-report.md",
            root / "docs" / "decision-log.md",
        }
        offenders = []
        for pattern in ("*.py", "*.md", "*.example", "*.json"):
            for f in root.rglob(pattern):
                if f.resolve() in exempt:
                    continue
                parts = set(f.parts)
                if parts & {
                    ".venv", "node_modules", ".git", "__pycache__",
                    "dist", ".pytest_cache", ".ruff_cache",
                }:
                    continue
                try:
                    if "RESOLVE_E_BUYER_COMPANY" in f.read_text(encoding="utf-8"):
                        offenders.append(str(f.relative_to(root)))
                except (UnicodeDecodeError, PermissionError):
                    continue
        assert not offenders, f"wrong env var name still present in: {offenders}"

    def test_disclosure_ready_is_false_by_default(self) -> None:
        assert Settings(buyer_company="").disclosure_ready is False

    def test_disclosure_ready_rejects_whitespace_only(self) -> None:
        assert Settings(buyer_company="   ").disclosure_ready is False

    def test_disclosure_ready_true_once_set(self) -> None:
        assert Settings(buyer_company="Contoso Manufacturing").disclosure_ready is True

    def test_safe_dump_reports_disclosure_readiness_without_leaking_a_secret(self) -> None:
        # buyer_company is not a secret (it is spoken aloud on every
        # call), so unlike calle_api_key it is fine to surface directly
        # for operator visibility on /health.
        dump = Settings(buyer_company="Contoso Manufacturing").safe_dump()
        assert dump["disclosure_ready"] is True
        assert dump["buyer_company"] == "Contoso Manufacturing"

    def test_live_calls_still_require_both_a_flag_and_a_key_independent_of_company(
        self,
    ) -> None:
        # buyer_company is a separate, additional gate -- it must not
        # accidentally relax the existing two-condition live-call check.
        s = Settings(
            calle_live_calls=True, calle_api_key="", buyer_company="Contoso Manufacturing"
        )
        assert s.live_calls_enabled is False


class TestEnvExampleNeverCarriesARealKey:
    """Regression for a real, live CALL-E key found committed to
    `.env.example` during a release-verification pass.

    `.env.example` is the one file in this project explicitly meant to
    be safe to publish -- every other credential-shaped file is
    git-ignored. It is not: only `.env` is. `git ls-files` (which the
    original `scripts/check_secrets.sh` used) returns nothing at all in
    a repository with zero commits, which this one had at the time, so
    that check had been silently a no-op through every prior "PASS".
    The shell script now also scans `git ls-files --others
    --exclude-standard` (everything that WOULD be staged); this test is
    the same check in Python, run as part of the normal suite rather
    than only when someone remembers to run the shell script by hand.
    """

    def test_calle_api_key_is_blank_in_the_example_file(self) -> None:
        example = pathlib.Path(__file__).resolve().parents[2] / ".env.example"
        for line in example.read_text(encoding="utf-8").splitlines():
            if line.startswith("CALLE_API_KEY="):
                assert line == "CALLE_API_KEY=", (
                    f"a real-looking value follows CALLE_API_KEY= in .env.example: "
                    f"{line[:24]}..."
                )
                return
        raise AssertionError("CALLE_API_KEY= line not found in .env.example at all")

    def test_no_high_entropy_iams_key_anywhere_git_would_stage(self) -> None:
        import re
        import shutil
        import subprocess

        git = shutil.which("git")
        if git is None:
            pytest.skip("git not available")

        strict = re.compile(r"iams_(live|test)_[A-Za-z0-9_-]{20,}")
        root = pathlib.Path(__file__).resolve().parents[2]
        result = subprocess.run(  # noqa: S603 - static argv, no shell, no user input
            [git, "ls-files", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            pytest.skip("not a git repository")

        offenders = []
        for rel in result.stdout.splitlines():
            f = root / rel
            if not f.is_file() or f.name == "check_secrets.sh":
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError):
                continue
            if strict.search(text):
                offenders.append(rel)
        assert not offenders, f"high-entropy CALL-E key found in: {offenders}"
