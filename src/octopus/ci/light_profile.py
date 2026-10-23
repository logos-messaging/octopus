"""The Seatbelt profile of the light isolation level (macOS `sandbox-exec`).

It is a policy on a normal process of the dedicated account, not a boundary like a VM: it shares
the host kernel. It starts from "allow everything" and denies what a CI job has no business
with, so ordinary toolchains keep working. `sandbox-exec` is deprecated by Apple but still
shipped; this module is the only place that knows the profile language.
"""

from __future__ import annotations

NETWORKS = ("internet", "none")

# Later rules win. Writes: only the job directory and the temporary directories.
_BASE = """(version 1)
(allow default)
(deny file-write*)
(allow file-write*
  (subpath (param "WORK"))
  (subpath "/private/tmp")
  (subpath "/private/var/folders")
  (literal "/dev/null")
  (literal "/dev/tty")
  (literal "/dev/dtracehelper")
  (regex #"^/dev/ttys[0-9]+$"))
(deny file-read* (subpath "/Users") (subpath "/Volumes"))
(allow file-read* (subpath (param "WORK")))
(deny process-exec
  (literal "/bin/launchctl")
  (literal "/usr/bin/at")
  (literal "/usr/bin/crontab")
  (literal "/usr/bin/sudo")
  (literal "/usr/bin/su"))
(deny mach-lookup (global-name-prefix "com.apple.xpc.launchd.domain"))
(deny mach-lookup (global-name "com.apple.xpc.smd"))
"""

_NETWORK_INTERNET = '(deny network* (remote ip "localhost:*"))\n'
_NETWORK_NONE = "(deny network*)\n"


class ProfileError(ValueError):
    pass


def render(network: str = "internet") -> str:
    """The profile text; the job directory is passed with `-D WORK=<dir>`, never spliced in."""
    if network not in NETWORKS:
        raise ProfileError(f"unknown network: {network}: use one of: {', '.join(NETWORKS)}")
    return _BASE + (_NETWORK_NONE if network == "none" else _NETWORK_INTERNET)
