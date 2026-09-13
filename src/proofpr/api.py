"""The webhook service.

Two endpoints that receive events, and two that report health. Everything here
follows the same shape: verify the signature against the raw body, decide whether
the event is one we act on, hand it to the object that knows what to do, and
return 200 either way so the provider stops retrying.

Returning 200 for an event we ignore is deliberate. A provider that receives 4xx
disables the webhook after enough of them, and most of what arrives is genuinely
not ours.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Header, Request, Response, status

from proofpr.composition import build_apps, open_ledger
from proofpr.observability.logging import configure_logging, get_logger
from proofpr.post.reconciler import Reconciler
from proofpr.post.release_notify import ReleaseNotifier
from proofpr.settings import load_settings
from proofpr.webhooks import signatures

logger = get_logger("proofpr.api")

#: How often the reconciler sweeps for drift, in hours.
RECONCILE_INTERVAL_HOURS = 6


class Service:
    """Everything the endpoints need, built once at start-up."""

    def __init__(self) -> None:
        """Build the service from settings."""
        self.settings = load_settings()
        self.apps = build_apps(self.settings)
        self.ledger = open_ledger(self.settings)
        self.notifier = ReleaseNotifier(apps=self.apps, ledger=self.ledger)
        self.reconciler = Reconciler(apps=self.apps, ledger=self.ledger)
        self.scheduler = AsyncIOScheduler()

    async def aclose(self) -> None:
        """Shut down cleanly."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        await self.apps.aclose()
        self.ledger.close()

    async def reconcile(self) -> None:
        """The scheduled drift sweep."""
        report = await self.reconciler.run_once()
        if report.unrepaired:
            logger.warning(
                "drift_needs_a_human",
                count=len(report.unrepaired),
                problems=[repair.problem for repair in report.unrepaired],
            )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the service, start the reconciler, and tear both down."""
    configure_logging(json_output=True)
    service = Service()
    app.state.service = service
    service.scheduler.add_job(
        service.reconcile,
        "interval",
        hours=RECONCILE_INTERVAL_HOURS,
        id="reconcile",
        max_instances=1,
        coalesce=True,
    )
    service.scheduler.start()
    logger.info("service_started", reconcile_every_hours=RECONCILE_INTERVAL_HOURS)
    try:
        yield
    finally:
        await service.aclose()


app = FastAPI(
    title="ProofPR",
    summary="Proof-carrying pull requests from chat bug reports.",
    lifespan=lifespan,
)


def _service(request: Request) -> Service:
    """Return the service built at start-up."""
    service: Service = request.app.state.service
    return service


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(request: Request) -> Response:
    """Readiness: the ledger is writable and the schema is present."""
    service = _service(request)
    try:
        service.ledger.migrate()
        unfinished = len(service.ledger.unfinished_runs())
    except Exception as error:  # noqa: BLE001 - readiness reports, never raises
        logger.warning("not_ready", error=str(error))
        return Response(
            content=f'{{"status":"not ready","error":"{error}"}}',
            media_type="application/json",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return Response(
        content=f'{{"status":"ready","unfinished_runs":{unfinished}}}',
        media_type="application/json",
    )


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Receive a GitHub event, and act on a merged pull request of ours."""
    service = _service(request)
    body = await request.body()
    verification = signatures.verify_github(
        service.settings.github.webhook_secret.get_secret_value(), body, x_hub_signature_256
    )
    if not verification.valid:
        logger.warning("webhook_rejected", provider="github", reason=verification.reason)
        return _rejected(verification.reason)

    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": f"event {x_github_event}"}

    notification = await service.notifier.on_pull_request_event(await request.json())
    if notification is None:
        return {"status": "ignored", "reason": "not a merged ProofPR pull request"}
    return {"status": "notified", "run_id": notification.run_id}


@app.post("/webhooks/linear")
async def linear_webhook(
    request: Request,
    linear_signature: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Receive a Linear event.

    Accepted and verified, but not yet acted on: the useful Linear events land
    with the maintainer-facing work in M9, and an endpoint that verified nothing
    would be worse than one that does nothing.
    """
    service = _service(request)
    body = await request.body()
    verification = signatures.verify_linear(
        service.settings.linear.webhook_secret.get_secret_value(), body, linear_signature
    )
    if not verification.valid:
        logger.warning("webhook_rejected", provider="linear", reason=verification.reason)
        return _rejected(verification.reason)
    return {"status": "ignored", "reason": "Linear events are not acted on yet"}


def _rejected(reason: str) -> dict[str, Any]:
    """Return the body for a request that failed verification.

    200 rather than 401, so a provider does not disable the webhook over a
    misconfigured secret. The rejection is logged and counted; nothing is acted
    on.
    """
    return {"status": "rejected", "reason": reason}
