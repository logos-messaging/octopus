"""The job summary of the submit job: which machine took each leg, or why it fell back."""

from __future__ import annotations

import re
from collections.abc import Sequence

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


ICONS = {
    "success": "✅",
    "failure": "❌",
    "error": "❌",
    "timeout": "⏱️",
    "cancelled": "⏹️",
}
_HOSTED = "☁️"
_FALLBACK = "🔁"
_SAFE = re.compile(r"[^A-Za-z0-9._/:-]")


def _facts(context: dict[str, str] | None) -> str:
    """The line under the title: repo, commit and run, from the Actions environment."""
    ctx = {k: _SAFE.sub("", v) for k, v in (context or {}).items()}
    parts = []
    if ctx.get("repo"):
        parts.append(f"`{ctx['repo']}`" + (f" @ `{ctx['sha'][:7]}`" if ctx.get("sha") else ""))
    if ctx.get("run"):
        link = f"{ctx.get('server', 'https://github.com')}/{ctx.get('repo', '')}/actions/runs/{ctx['run']}"
        run = f"[run {ctx['run']}]({link})" if ctx.get("repo") else f"run {ctx['run']}"
        parts.append(run + (f", attempt {ctx['attempt']}" if ctx.get("attempt") else ""))
    return " · ".join(parts)


def render(
    oses: list[str],
    result: SubmitResult,
    *,
    context: dict[str, str] | None = None,
    hosted: Sequence[str] = (),
) -> str:
    """The job summary: a title with the verdict, one row per leg, and a tally."""
    rows = []
    passed = failed = lost = 0
    for os_name in oses:
        node = result.handled.get(os_name)
        if node is not None:
            outcome = result.outcomes.get(os_name)
            state = str((outcome or {}).get("state", ""))
            icon = ICONS.get(state, "▶️")
            passed += state == "success"
            failed += state in ("failure", "error", "timeout")
            rows.append(
                f"| {icon} {clean(os_name)} | {machine_md(result, os_name)} | {_outcome(outcome)} |"
            )
        else:
            lost += 1
            reason = clean(result.reasons.get(os_name, "")) or "not taken"
            rows.append(f"| {_FALLBACK} {clean(os_name)} | GitHub-hosted runner | {reason} |")
    for os_name in hosted:
        rows.append(f"| {_HOSTED} {clean(os_name)} | GitHub-hosted runner | always runs on GitHub |")
    team = len(result.handled)
    if failed:
        verdict = "❌ a leg failed on a team machine"
    elif team and passed == team and not lost:
        verdict = "✅ every leg on a team machine passed"
    elif team:
        verdict = "⚠️ some legs ran on GitHub instead"
    else:
        verdict = "⚠️ no team machine took a leg: everything runs on GitHub"
    tally = f"**Team machines:** {passed} of {len(oses)} legs passed"
    if failed:
        tally += f", {failed} failed"
    if lost:
        tally += f" · **fell back to GitHub:** {lost}"
    if hosted:
        tally += f" · **always on GitHub:** {', '.join(clean(h) for h in hosted)}"
    head = ["### Octopus CI", "", verdict]
    facts = _facts(context)
    if facts:
        head += ["", facts]
    lines = [
        *head,
        "",
        "| Leg | Where it ran | Result |",
        "| --- | --- | --- |",
        *rows,
        "",
        tally,
        "",
        (
            "Each leg's output is in the log of this job, in lines that start with "
            "`[leg machine]`. A machine that goes silent hands its leg back to GitHub; "
            "the jobs below show those runs."
        ),
        "",
    ]
    return "\n".join(lines)


def machine_md(result: SubmitResult, os_name: str) -> str:
    """`**name** (login)` for a table cell."""
    name = clean(result.names.get(os_name, "")) or "a team machine"
    login = clean(result.logins.get(os_name, ""))
    node = clean(result.handled.get(os_name, ""))[:12]
    who = f"**{name}**" + (f" ({login})" if login else "")
    return f"{who}, node `{node}`" if node else who


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
