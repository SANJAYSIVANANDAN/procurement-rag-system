import typer
from pydantic import ValidationError

from covenantiq import __version__
from covenantiq.core.config import Settings

app = typer.Typer(help="CovenantIQ command line.", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def migrate() -> None:
    """Apply database migrations. A no-op until the schema arrives in Milestone 3."""
    typer.echo("no migrations to apply yet")


@app.command("check-config")
def check_config() -> None:
    """Load settings from the environment and report problems (secrets are never printed)."""
    try:
        settings = Settings()
    except ValidationError as exc:
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "settings"
            typer.echo(f"{location}: {error['msg']}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"configuration ok (env={settings.env.value})")
