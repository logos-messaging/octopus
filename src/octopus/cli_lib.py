"""`octopus lib ...`: find and install liblogosdelivery, which the transport loads."""

from __future__ import annotations

import tempfile
from pathlib import Path

import typer

from .transport import library

lib_app = typer.Typer(no_args_is_help=True, help="The native library (liblogosdelivery).")


FileOption = typer.Option(None, "--file", help="Install a library you built.")
TagOption = typer.Option(library.DEFAULT_TAG, "--tag", help="Octopus release to fetch from.")


def _fail(message: str) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=1)


@lib_app.command("status")
def lib_status() -> None:
    """Show where the library is expected and whether it is there."""
    st = library.status()
    if st.root is None:
        typer.echo("bindings: not found (install with: pip install 'octopus[logos]')")
        raise typer.Exit(code=1)
    typer.echo(f"bindings: {st.root}")
    typer.echo(f"library: {st.library} ({'present' if st.library_present else 'missing'})")
    typer.echo(f"release asset: {st.asset or 'none for this platform'}")
    if not st.library_present:
        typer.echo("install it with: octopus lib install")
        raise typer.Exit(code=1)


@lib_app.command("install")
def lib_install(file: Path | None = FileOption, tag: str = TagOption) -> None:
    """Fetch the library for this platform (or use --file) and put it where the bindings load it."""
    root = library.bindings_root()
    if root is None:
        raise _fail("could not find the bindings: install with: pip install 'octopus[logos]'")
    try:
        if file is not None:
            installed = library.install_file(file, root)
        else:
            with tempfile.TemporaryDirectory(prefix="octopus-lib-") as tmp:
                installed = library.install_file(library.download(tag, Path(tmp)), root)
    except (library.LibraryError, OSError) as e:
        raise _fail(f"could not install the library: {e}") from e
    typer.echo(f"installed: {installed}")
