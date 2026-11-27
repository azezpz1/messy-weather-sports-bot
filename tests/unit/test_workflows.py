"""Regression checks for GitHub Actions workflows."""

import os
import re
import subprocess
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
TEXT = (WORKFLOWS / "venue-drift-check.yml").read_text("utf-8")


def test_every_cron_has_exactly_one_job_guarded_on_it() -> None:
    crons = re.findall(r'^\s*- cron: "([^"]+)"', TEXT, flags=re.MULTILINE)
    guards = re.findall(r"github\.event\.schedule == '([^']+)'", TEXT)

    assert len(crons) == 2
    assert sorted(crons) == sorted(guards)
    assert len(set(crons)) == len(crons)


def test_each_sports_job_checks_only_its_own_sport() -> None:
    jobs = re.split(r"^  (?=[a-z]+:\n)", TEXT.split("jobs:\n", 1)[1], flags=re.MULTILINE)
    commands = {
        name: re.findall(r"uv run python scripts/check_venue_drift\.py --sport (\w+)", body)
        for name, body in ((job.split(":", 1)[0], job) for job in jobs if job.strip())
    }

    assert commands == {"nfl": ["nfl"], "cfb": ["cfb"]}


def run_scripts(text: str) -> list[str]:
    """The script of every `run:` step: its value when that is on the same line, or - for a
    block (`run: |`) - every line indented under it."""
    lines = text.splitlines()
    scripts: list[str] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*(?:-\s+)?)run:\s*(.*)$", line)
        if match is None:
            continue
        value = match.group(2)
        if not value.startswith(("|", ">")):
            scripts.append(value)
            continue
        key_column = len(match.group(1))
        body: list[str] = []
        for following in lines[index + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) <= key_column:
                break
            body.append(following)
        scripts.append("\n".join(body))
    return scripts


def test_no_run_step_interpolates_an_expression() -> None:
    # Expressions in a script body are how workflow injection happens; pass values via env.
    scripts = run_scripts(TEXT)

    assert scripts
    assert not [script for script in scripts if "${{" in script]


def test_the_run_step_scan_reads_block_scripts_and_list_items_too() -> None:
    sample = (
        "steps:\n"
        "  - name: Greet\n"
        "    run: |\n"
        "      echo start\n"
        '      echo "${{ github.event.pull_request.title }}"\n'
        "  - run: uv sync\n"
        "  - name: After\n"
        "    run: echo ok\n"
    )

    scripts = run_scripts(sample)

    assert [("${{" in script) for script in scripts] == [True, False, False]
    assert scripts[1:] == ["uv sync", "echo ok"]


@pytest.mark.parametrize(
    ("path", "relevant"),
    [
        ("docs/unrelated.md", "false"),
        ("tests/unit/test_weather.py", "false"),
        ("README.md", "true"),
        ("uv.lock", "true"),
        (".github/workflows/dashboard-browser.yml", "true"),
        ("tests/e2e/test_dashboard_deployment.py", "true"),
        ("tests/conftest.py", "true"),
        ("scripts/update-to-latest-release.sh", "true"),
        ("src/messy_weather_sports_bot/scheduler_store.py", "true"),
        ("src/messy_weather_sports_bot/migrations/versions/example.py", "true"),
        ("src/messy_weather_sports_bot/templates/example.html", "true"),
    ],
)
def test_dashboard_path_filter(tmp_path: Path, path: str, relevant: str) -> None:
    ci = (WORKFLOWS / "ci.yml").read_text("utf-8")
    script = next(script for script in run_scripts(ci) if "git diff" in script)

    def git(*args: str) -> None:
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=CI Test",
                "-c",
                "user.email=ci@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "--quiet")
    changed = tmp_path / path
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("original\n", encoding="utf-8")
    git("add", "-f", "--", path)
    git("commit", "-qm", "Base tree")
    git("rm", "--quiet", "--", path)
    git("commit", "-qm", "PR tree")

    output = tmp_path / "output"
    subprocess.run(
        ["bash", "-eo", "pipefail", "-c", script],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_OUTPUT": str(output)},
        check=True,
    )
    assert output.read_text("utf-8") == f"relevant={relevant}\n"
