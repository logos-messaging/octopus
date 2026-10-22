"""The job summary of the submit job: which machine took each leg, or why it fell back."""

from __future__ import annotations

import re

from .submit import SubmitResult

_UNSAFE = re.compile(r"[^A-Za-z0-9 ._()+-]")
MAX_NAME = 60


def clean(text: str) -> str:
    """A name chosen by a remote machine, safe to put in a markdown table."""
    return _UNSAFE.sub("", text).strip()[:MAX_NAME]


def render(oses: list[str], result: SubmitResult) -> str:
    lines = [
        "### Octopus CI",
        "",
        "| Leg | Where it ran | Why |",
        "| --- | --- | --- |",
    ]
    for os_name in oses:
        node = result.handled.get(os_name)
        if node is not None:
            name = clean(result.names.get(os_name, "")) or "a team machine"
            lines.append(f"| {clean(os_name)} | **{name}** (node `{clean(node)[:12]}`) | taken |")
        else:
            reason = clean(result.reasons.get(os_name, "")) or "not taken"
            lines.append(f"| {clean(os_name)} | GitHub-hosted runner | {reason} |")
    note = (
        "A leg taken by a team machine reports its result as the commit status "
        "`octopus/ci (<os>)`; it can finish after this workflow run does."
    )
    return "\n".join([*lines, "", note, ""])
