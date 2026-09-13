"""Command line interface.

This is a composition root: it builds settings and adapters and hands them to
the pipeline. It contains no triage logic of its own.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from proofpr import __version__
from proofpr.composition import (
    build_approver,
    build_apps,
    build_memory_apps,
    build_model,
    load_report,
    open_ledger,
)
from proofpr.doctor import run_doctor
from proofpr.domain.enums import Outcome
from proofpr.domain.errors import ProofPRError
from proofpr.domain.models import HealthCheck
from proofpr.domain.run import RunState
from proofpr.evaluation import (
    build_fault_runner,
    build_runner,
    build_scripted_model_factory,
    check_clean,
    datasets_root,
    load_case,
    load_cases,
    load_split,
    write_report,
)
from proofpr.observability.logging import configure_logging
from proofpr.pipeline import Config as PipelineConfig
from proofpr.pipeline import Pipeline
from proofpr.post.reconciler import Reconciler, ReconcileReport
from proofpr.prompts import version_hash as prompt_version
from proofpr.settings import Settings, load_settings

app = typer.Typer(
    name="proofpr",
    help="Turn Discord bug reports into proof-carrying pull requests, or an honest decline.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _version_callback(value: bool) -> None:
    """Print the version and exit when ``--version`` is passed."""
    if value:
        console.print(__version__)
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
    log_level: Annotated[str, typer.Option(help="Logging level.")] = "INFO",
    json_logs: Annotated[bool, typer.Option(help="Emit JSON logs.")] = False,
) -> None:
    """Configure logging before any subcommand runs.

    Args:
        version: Handled entirely by the eager callback; present so Typer
            registers the flag.
        log_level: Logging level name.
        json_logs: Emit JSON lines instead of console output.
    """
    del version
    configure_logging(level=log_level, json_output=json_logs)


@app.command()
def doctor() -> None:
    """Verify the environment by doing one real write and readback per application.

    Also runs a test inside the sandbox and writes to the ledger. This is the
    only command that proves the environment is genuinely wired up, and it exits
    non-zero unless every check passes.
    """
    settings = load_settings()
    _render_config_table(settings)
    checks = asyncio.run(_doctor(settings))
    _render_checks(checks)

    failed = [check for check in checks if not check.ok]
    if failed:
        console.print(f"\n[red]{len(failed)} of {len(checks)} checks failed.[/red]")
        console.print("See docs/SETUP.md for the credential each one needs.")
        raise typer.Exit(code=1)
    console.print(f"\n[green]all {len(checks)} checks passed[/green]")


async def _doctor(settings: Settings) -> list[HealthCheck]:
    """Build the applications, run every check, and close cleanly."""
    apps = build_apps(settings)
    ledger = open_ledger(settings)
    try:
        return await run_doctor(apps, ledger)
    finally:
        await apps.aclose()
        ledger.close()


def _render_checks(checks: list[HealthCheck]) -> None:
    """Print one row per check, failures included."""
    table = Table(title="Checks", show_header=True)
    table.add_column("")
    table.add_column("App")
    table.add_column("Check")
    table.add_column("Detail", overflow="fold")
    table.add_column("ms", justify="right")
    for check in checks:
        table.add_row(
            "[green]ok[/green]" if check.ok else "[red]fail[/red]",
            check.app,
            check.check,
            check.detail,
            str(check.elapsed_ms or ""),
        )
    console.print(table)


@app.command()
def run(
    source: Annotated[str, typer.Argument(help="Discord message link or a fixture path.")],
    dry_run: Annotated[
        bool, typer.Option(help="Use in-memory applications instead of writing anywhere.")
    ] = False,
) -> None:
    """Triage one report and print its terminal state.

    `source` is either a Discord message link or a path to a YAML fixture. A
    fixture run performs the same steps and the same writes; only the report's
    origin differs.
    """
    settings = load_settings()
    try:
        state = asyncio.run(_run_once(settings, source, dry_run=dry_run))
    except ProofPRError as error:
        # A configuration or credential problem is the user's to fix, and a
        # traceback is a worse way to tell them than a sentence.
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from error

    console.print(_outcome_panel(state))
    if state.outcome is Outcome.FAILED_CLOSED:
        raise typer.Exit(code=1)


async def _run_once(settings: Settings, source: str, *, dry_run: bool) -> RunState:
    """Build the pipeline and triage one report."""
    apps = build_memory_apps() if dry_run else build_apps(settings)
    ledger = open_ledger(settings)
    pipeline = Pipeline(
        apps=apps,
        ledger=ledger,
        model=build_model(settings)
        if settings.models.openrouter_api_key.get_secret_value()
        else None,
        config=PipelineConfig.from_document(apps.config),
        approver=build_approver(settings, apps.config),
    )
    try:
        if dry_run:
            console.print(
                "[yellow]dry run: in-memory applications, same guard and same pipeline[/yellow]"
            )
        return await pipeline.run(load_report(source))
    finally:
        await apps.aclose()
        ledger.close()


def _outcome_panel(state: RunState) -> Table:
    """Render the terminal state of a run."""
    table = Table(title=f"Run {state.run_id}", show_header=False)
    table.add_column("Key")
    table.add_column("Value", overflow="fold")
    table.add_row("outcome", str(state.outcome.value if state.outcome else "none"))
    table.add_row("reason", str(state.reason.value if state.reason else ""))
    table.add_row("intent", f"{state.intent} ({state.intent_decided_by})")
    if state.exception:
        table.add_row("localized", f"{state.exception} in {state.path}:{state.function}")
    if state.exists.found:
        table.add_row("exists", f"{state.exists.kind} {state.exists.reference or ''}")
    if state.linear_url:
        table.add_row("issue", f"{state.linear_identifier} {state.linear_url}")
    if state.proof_complete:
        table.add_row("proof", f"mutants {state.mutants_killed}/{state.mutants_total} killed")
    if state.pr_url:
        table.add_row("pull request", f"{state.pr_url} ({state.ci_summary})")
    if state.consistent is not None:
        table.add_row(
            "four-way consistent",
            "yes" if state.consistent else f"no: {'; '.join(state.consistency_problems)}",
        )
    if state.injection_flags:
        table.add_row("injection flags", ", ".join(state.injection_flags))
    table.add_row("receipt", state.receipt or "")
    return table


@app.command(name="eval")
def eval_(
    split: Annotated[str, typer.Option(help="Frozen split to run: dev or test.")] = "dev",
    arms: Annotated[
        str, typer.Option(help="Comma separated arms, or 'both' for the paired comparison.")
    ] = "both",
    repo: Annotated[
        Path, typer.Option(help="Target repository checkout to evaluate against.")
    ] = Path("tests/fixtures/validkit"),
    package: Annotated[str, typer.Option(help="Importable package under test.")] = "validkit",
    out: Annotated[Path, typer.Option(help="Where results are written.")] = Path("eval/out"),
    model: Annotated[
        str,
        typer.Option(
            help="'real' for the configured model, or 'scripted' to exercise the harness "
            "with fixed answers and no credentials."
        ),
    ] = "real",
) -> None:
    """Run a frozen split through both arms and write the results.

    Nothing here computes a number. The runner produces a ledger; `proofpr
    report` reads it. Keeping them apart is what stops a runner bug from quietly
    improving a result.
    """
    settings = load_settings()
    scripted = model == "scripted"
    if model not in ("real", "scripted"):
        console.print(f"[red]unknown model mode {model!r}: use 'real' or 'scripted'[/red]")
        raise typer.Exit(code=1)
    if not scripted and not settings.models.openrouter_api_key.get_secret_value():
        console.print(
            "[red]no OPENROUTER_API_KEY: an evaluation without a model measures nothing[/red]"
        )
        console.print(
            "to exercise the harness itself without credentials: "
            "[bold]proofpr eval --model scripted[/bold]"
        )
        raise typer.Exit(code=1)
    if scripted:
        console.print(
            "[yellow]scripted model: the pipeline, sandbox, guard, and ledger are real, the "
            "model is not. These numbers measure the harness and may not be quoted as "
            "results.[/yellow]"
        )

    labels = ["proofpr", "no_gate"] if arms == "both" else [a.strip() for a in arms.split(",")]
    try:
        frozen = load_split(split)
        cases = load_cases(frozen)
    except ProofPRError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from error

    console.print(
        f"{len(cases)} cases in the [bold]{split}[/bold] split, arms {', '.join(labels)}, "
        f"frozen {frozen.frozen_at or 'never'}"
    )
    if split == "test" and frozen.prompt_version_hash not in (None, prompt_version()):
        console.print(
            "[yellow]prompts have changed since this split was frozen; results will be "
            "labelled as a different prompt version[/yellow]"
        )

    results = asyncio.run(_evaluate(settings, cases, labels, repo, package, scripted=scripted))
    _render_eval(results)

    paths = write_report(
        open_ledger(settings),
        out_dir=out,
        split=split,
        prompt_version="scripted-stub" if scripted else prompt_version(),
    )
    console.print(f"\nwrote {paths['markdown']} and {paths['html']}")


async def _evaluate(
    settings: Settings,
    cases: list[Any],
    labels: list[str],
    repo: Path,
    package: str,
    *,
    scripted: bool = False,
) -> list[Any]:
    """Run every case in every arm."""
    ledger = open_ledger(settings)
    factory = (
        build_scripted_model_factory() if scripted else (lambda case: build_model(settings))  # noqa: ARG005
    )
    try:
        runner = build_runner(
            settings=settings,
            ledger=ledger,
            repo_path=repo,
            package=package,
            model_factory=factory,
            datasets_root=datasets_root(),
        )
        report_ = await runner.run_split(cases, arms=labels)
        return list(report_.results)
    finally:
        ledger.close()


@app.command(name="eval-faults")
def eval_faults(
    repo: Annotated[
        Path, typer.Option(help="Target repository checkout to evaluate against.")
    ] = Path("tests/fixtures/validkit"),
    package: Annotated[str, typer.Option(help="Importable package under test.")] = "validkit",
    out: Annotated[Path, typer.Option(help="Where results are written.")] = Path("eval/out"),
    model: Annotated[
        str, typer.Option(help="'real' for the configured model, or 'scripted'.")
    ] = "scripted",
) -> None:
    """Run the fault matrix: one planned failure per write, then count what stuck.

    Every cell runs the same publishable case with one injected failure and
    records what the applications actually hold afterwards, in the same ledger
    `proofpr eval` uses. `proofpr report` picks up the fault section from there;
    this command does not compute anything itself.
    """
    settings = load_settings()
    scripted = model == "scripted"
    if model not in ("real", "scripted"):
        console.print(f"[red]unknown model mode {model!r}: use 'real' or 'scripted'[/red]")
        raise typer.Exit(code=1)
    if not scripted and not settings.models.openrouter_api_key.get_secret_value():
        console.print("[red]no OPENROUTER_API_KEY: pass --model scripted instead[/red]")
        raise typer.Exit(code=1)

    results = asyncio.run(_run_faults(settings, repo, package, scripted=scripted))
    table = Table(title="Fault matrix", show_header=True)
    table.add_column("Operation")
    table.add_column("Mode")
    table.add_column("Recovered")
    table.add_column("Duplicates", justify="right")
    table.add_column("Missing", justify="right")
    for result in results:
        table.add_row(
            result.operation,
            result.mode,
            "invalid" if not result.valid else ("yes" if result.recovered else "no"),
            str(result.duplicates),
            str(result.missing),
        )
    console.print(table)

    paths = write_report(
        open_ledger(settings),
        out_dir=out,
        split="faults",
        prompt_version="scripted-stub" if scripted else prompt_version(),
    )
    console.print(f"\nwrote {paths['markdown']} and {paths['html']}")


async def _run_faults(settings: Settings, repo: Path, package: str, *, scripted: bool) -> list[Any]:
    """Run every cell of the fault matrix once."""
    ledger = open_ledger(settings)
    factory = (
        build_scripted_model_factory() if scripted else (lambda case: build_model(settings))  # noqa: ARG005
    )
    try:
        runner = build_runner(
            settings=settings,
            ledger=ledger,
            repo_path=repo,
            package=package,
            model_factory=factory,
            datasets_root=datasets_root(),
        )
        matrix_case = load_case("seeded-001")
        fault_runner = build_fault_runner(runner, matrix_case)
        return list(await fault_runner.run_all())
    finally:
        ledger.close()


def _render_eval(results: list[Any]) -> None:
    """Print one row per case, per arm."""
    table = Table(title="Cases", show_header=True)
    table.add_column("Case")
    table.add_column("Arm")
    table.add_column("Outcome")
    table.add_column("Expected")
    table.add_column("Hidden test")
    table.add_column("Cost", justify="right")
    for result in results:
        judged = (
            "[green]passed[/green]"
            if result.hidden_test_passed
            else ("[red]failed[/red]" if result.hidden_test_passed is False else "unjudged")
        )
        table.add_row(
            result.case_id,
            result.arm,
            result.outcome if result.outcome_correct else f"[red]{result.outcome}[/red]",
            result.expected_outcome,
            judged if result.wrote_code else "no patch",
            f"${result.cost_usd:.3f}",
        )
    console.print(table)


@app.command()
def report(
    out: Annotated[Path, typer.Option(help="Where results are written.")] = Path("eval/out"),
    split: Annotated[str, typer.Option(help="Label for the split being reported.")] = "all",
    assert_clean: Annotated[
        bool, typer.Option(help="Exit non-zero on an unsafe pull request or a broken receipt.")
    ] = False,
) -> None:
    """Regenerate results.md and report.html from run ledgers only."""
    settings = load_settings()
    ledger = open_ledger(settings)
    try:
        paths = write_report(ledger, out_dir=out, split=split, prompt_version=prompt_version())
        console.print(paths["markdown"].read_text(encoding="utf-8"))
        problems = list(check_clean(ledger)) if assert_clean else []
    finally:
        ledger.close()

    if problems:
        for problem in problems:
            console.print(f"[red]{problem}[/red]")
        raise typer.Exit(code=1)


@app.command()
def reconcile(
    limit: Annotated[int, typer.Option(help="How many recent runs to re-check.")] = 50,
) -> None:
    """Compare every application against the ledger and repair drift.

    The same sweep the service runs every six hours. Exits non-zero when a
    divergence needs a human, so it can be used as a check rather than only as a
    repair.
    """
    settings = load_settings()
    try:
        report = asyncio.run(_reconcile(settings, limit))
    except ProofPRError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from error

    table = Table(title="Reconciliation", show_header=True)
    table.add_column("Run")
    table.add_column("App")
    table.add_column("Problem", overflow="fold")
    table.add_column("Outcome")
    for repair in report.repairs:
        table.add_row(
            repair.run_id,
            repair.app,
            repair.problem,
            "[green]repaired[/green]" if repair.repaired else f"[red]{repair.detail}[/red]",
        )
    if report.repairs:
        console.print(table)

    console.print(
        f"checked {report.checked}, diverged {report.diverged}, repaired {report.repaired}"
    )
    if report.unrepaired:
        raise typer.Exit(code=1)


async def _reconcile(settings: Settings, limit: int) -> ReconcileReport:
    """Run one reconciliation pass and close cleanly."""
    apps = build_apps(settings)
    ledger = open_ledger(settings)
    try:
        return await Reconciler(apps=apps, ledger=ledger).run_once(limit=limit)
    finally:
        await apps.aclose()
        ledger.close()


@app.command(name="verify-receipt")
def verify_receipt(
    run_id: Annotated[str, typer.Argument(help="Run identifier, for example r-7f3a.")],
    show_events: Annotated[bool, typer.Option(help="Print every event in the chain.")] = False,
) -> None:
    """Recompute a run's hash chain and compare it to the published receipt."""
    ledger = open_ledger(load_settings())
    try:
        verified, detail = ledger.verify_receipt(run_id)
        if show_events:
            table = Table(title=f"Chain for {run_id}", show_header=True)
            table.add_column("#", justify="right")
            table.add_column("event")
            table.add_column("step")
            table.add_column("hash")
            for event in ledger.events(run_id):
                table.add_row(
                    str(event["seq"]),
                    str(event["kind"]),
                    str(event["step"] or ""),
                    str(event["chain_hash"])[:16],
                )
            console.print(table)
    finally:
        ledger.close()

    if verified:
        console.print(f"[green]verified[/green] {detail}")
        return
    console.print(f"[red]not verified[/red] {detail}")
    raise typer.Exit(code=1)


@app.command()
def resume(
    run_id: Annotated[
        str | None, typer.Argument(help="Run to resume. Omit to resume every unfinished run.")
    ] = None,
) -> None:
    """Continue runs that died, from the last step that finished."""
    settings = load_settings()
    ledger = open_ledger(settings)
    try:
        targets = [run_id] if run_id else [row["run_id"] for row in ledger.unfinished_runs()]
        if not targets:
            console.print("no unfinished runs")
            return
    finally:
        ledger.close()

    for target in targets:
        state = asyncio.run(_resume_one(settings, str(target)))
        console.print(_outcome_panel(state))


async def _resume_one(settings: Settings, run_id: str) -> RunState:
    """Resume one run and close cleanly."""
    apps = build_apps(settings)
    ledger = open_ledger(settings)
    pipeline = Pipeline(
        apps=apps,
        ledger=ledger,
        model=build_model(settings)
        if settings.models.openrouter_api_key.get_secret_value()
        else None,
        config=PipelineConfig.from_document(apps.config),
        approver=build_approver(settings, apps.config),
    )
    try:
        return await pipeline.resume(run_id)
    finally:
        await apps.aclose()
        ledger.close()


def _render_config_table(settings: Settings) -> None:
    """Print non-secret configuration so `doctor` output is useful on its own."""
    table = Table(title="ProofPR configuration", show_header=True)
    table.add_column("Key")
    table.add_column("Value")
    table.add_row("env", settings.env)
    table.add_row("arm", settings.arms.value)
    table.add_row("db_path", str(settings.db_path))
    table.add_row("model.cheap", settings.models.cheap)
    table.add_row("model.strong", settings.models.strong)
    table.add_row("github repo", f"{settings.github.owner}/{settings.github.repo}")
    table.add_row("linear team", settings.linear.team_id or "(unset)")
    table.add_row("max spend", f"${settings.caps.max_spend_usd:.2f}")
    console.print(table)


if __name__ == "__main__":  # pragma: no cover
    app()
