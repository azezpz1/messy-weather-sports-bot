"""The venue drift workflow: each scheduled job must run for exactly its own cron.

GitHub passes the firing cron to the run as `github.event.schedule`, verbatim, so a job
guarded by a string that no cron produces is skipped on every scheduled run - and nothing
fails to say so."""

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "venue-drift-check.yml"
TEXT = WORKFLOW.read_text("utf-8")


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


def test_no_run_step_interpolates_an_expression() -> None:
    # Expressions in a script body are how workflow injection happens; pass values via env.
    run_lines = [line for line in TEXT.splitlines() if line.strip().startswith("run:")]

    assert run_lines
    assert not [line for line in run_lines if "${{" in line]
