"""The job summary of the submit job: which machine took each leg, or why it fell back."""

from __future__ import annotations

import re

from .submit import SubmitResult

_UNSAFE = re.compile(r"[^A-Za-z0-9 ._()+-]")
MAX_NAME = 60


def clean(text: str) -> str:
    """A name chosen by a remote machine, safe to put in a markdown table."""
    return _UNSAFE.sub("", text).strip()[:MAX_NAME]


def machine(result: SubmitResult, os_name: str) -> str:
    """The machine that took a leg, as people know it: `ces (ivansete-octopus)`."""
    name = clean(result.names.get(os_name, "")) or "a team machine"
    login = clean(result.logins.get(os_name, ""))
    return f"{name} ({login})" if login else name


def duration(seconds: object) -> str:
    if not isinstance(seconds, int | float) or seconds < 0:
        return ""
    whole = round(seconds)
    return f"{whole // 60}m{whole % 60:02d}s" if whole >= 60 else f"{whole}s"


def render_text(oses: list[str], result: SubmitResult) -> str:
    """The same as the table, for the job log: what ran where and how it ended."""
    rows = []
    for os_name in oses:
        if os_name in result.handled:
            outcome = result.outcomes.get(os_name) or {}
            state = clean(str(outcome.get("state", ""))) or "taken"
            exit_code = outcome.get("exit")
            if state == "failure" and isinstance(exit_code, int) and exit_code >= 0:
                state = f"failure (exit code {exit_code})"
            took = duration(outcome.get("seconds"))
            note = "" if result.lines.get(os_name) or not outcome else " [log not received]"
            rows.append((os_name, state, machine(result, os_name), took + note))
        else:
            reason = clean(result.reasons.get(os_name, "")) or "not taken"
            rows.append((os_name, "GitHub", "hosted runner", reason))
    widths = [max(len(r[i]) for r in rows) for i in range(3)] if rows else [0, 0, 0]
    lines = ["Octopus CI result"]
    for os_name, state, where, extra in rows:
        lines.append(
            f"  {os_name:<{widths[0]}}  {state:<{widths[1]}}  {where:<{widths[2]}}  {extra}".rstrip()
        )
    return "\n".join(lines)


def render(oses: list[str], result: SubmitResult) -> str:
    lines = [
        "### Octopus CI",
        "",
        "| Leg | Where it ran | Result |",
        "| --- | --- | --- |",
    ]
    for os_name in oses:
        node = result.handled.get(os_name)
        if node is not None:
            name = clean(result.names.get(os_name, "")) or "a team machine"
            login = clean(result.logins.get(os_name, ""))
            who = f"**{name}**" + (f" ({login})" if login else "")
            lines.append(
                f"| {clean(os_name)} | {who}, node `{clean(node)[:12]}` | "
                f"{_outcome(result.outcomes.get(os_name))} |"
            )
        else:
            reason = clean(result.reasons.get(os_name, "")) or "not taken"
            lines.append(f"| {clean(os_name)} | GitHub-hosted runner | {reason} |")
    note = (
        "A leg taken by a team machine reports its result to this job, which waits for it; "
        "a machine that goes silent hands its leg back to GitHub."
    )
    return "\n".join([*lines, "", note, ""])


def _outcome(outcome: dict | None) -> str:
    if not outcome:
        return "taken"
    state = clean(str(outcome.get("state", ""))) or "unknown"
    detail = clean(str(outcome.get("detail", "")))
    exit_code = outcome.get("exit")
    if state == "failure" and isinstance(exit_code, int) and exit_code >= 0:
        state = f"failure (exit code {exit_code})"
    took = duration(outcome.get("seconds"))
    return f"**{state}**" + (f" in {took}" if took else "") + (f": {detail}" if detail else "")
