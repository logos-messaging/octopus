# Install or upgrade Octopus on Windows. Re-run it to upgrade.
#
#   irm https://raw.githubusercontent.com/logos-messaging/octopus/main/scripts/install.ps1 | iex
#
# $env:OCTOPUS_REF picks the branch, tag or commit to install (default: main).
$ErrorActionPreference = "Stop"

$ref = if ($env:OCTOPUS_REF) { $env:OCTOPUS_REF } else { "main" }
# An archive, not git+: pip would clone every submodule, recursively.
$spec = "octopus[logos] @ https://github.com/logos-messaging/octopus/archive/$ref.tar.gz"

$python = $null
foreach ($candidate in @("py", "python3", "python")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) {
        & $cmd.Source -c "import sys; sys.exit(sys.version_info < (3, 10))" 2>$null
        if ($LASTEXITCODE -eq 0) { $python = $cmd.Source; break }
    }
}
if (-not $python) { throw "could not find Python 3.10 or newer: install it, then re-run" }

if (Get-Command pipx -ErrorAction SilentlyContinue) {
    Write-Host "installing with pipx"
    pipx install --force "$spec"
} else {
    Write-Host "installing into a private virtualenv"
    $root = if ($env:OCTOPUS_HOME) { $env:OCTOPUS_HOME } else { Join-Path $env:LOCALAPPDATA "octopus" }
    $venv = Join-Path $root "venv"
    & $python -m venv $venv
    & (Join-Path $venv "Scripts\pip.exe") install --quiet --upgrade "$spec"
    Write-Host "note: add $venv\Scripts to your PATH"
}

Write-Host @"

Octopus is installed. Next:
  octopus runner setup --coordinator <key>
  octopus runner allow <org/repo>
  octopus runner install-service

The logos-delivery Python bindings only load a Linux/macOS library today, so
`octopus lib install` has nothing to fetch on Windows: see docs/install.md.
"@
