"""Operator commands.

    resolve-e init-db       apply migrations up to head
    resolve-e seed          load the deterministic demo dataset
    resolve-e reset         drop, recreate, reseed
    resolve-e verify-key    check CALLE_API_KEY (a read; places no call)
    resolve-e smoke-test    place ONE real CALL-E call and print the result
    resolve-e status        print the exception queue
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.config import get_settings
from app.db import repositories as repo
from app.db.models import Base, ExceptionRecord
from app.db.session import get_engine, session_scope
from app.domain.enums import ActorType, ExceptionState
from app.domain.errors import ProviderRateLimited, ResolveEError
from app.logging_setup import configure_logging


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DemoException:
    """One seeded purchase order.

    A dataclass rather than a dict so ``hours_overdue`` is an int the
    type checker can see, not an ``object`` that happens to work.
    """

    po_number: str
    supplier_name: str
    recipient_name: str
    recipient_phone_e164: str
    hours_overdue: int
    item_summary: str
    #: What this PO is expected to demonstrate. Recorded in the audit
    #: trail so the seeded data explains its own purpose.
    expect: str


#: The seeded demo. Each PO maps to a scripted supplier response in
#: ``app.adapters.calle.mock``, so one batch call produces every branch
#: of the decision table from a clean database, every time.
DEMO_EXCEPTIONS: list[DemoException] = [
    DemoException(
        po_number="PO-4821",
        supplier_name="Acme Components",
        recipient_name="Jordan",
        recipient_phone_e164="+15550001111",
        hours_overdue=26,
        item_summary="40x bearing assemblies",
        expect="RESOLVED_ON_TIME - supplier confirms receipt and on-time ship",
    ),
    DemoException(
        po_number="PO-4822",
        supplier_name="Globex Industrial",
        recipient_name="Sam",
        recipient_phone_e164="+15550002222",
        # Under 24h on purpose -- see the note on PO-4822 in
        # adapters/calle/mock.py for why this keeps the demo stable.
        hours_overdue=20,
        item_summary="12x hydraulic pumps",
        expect="RESOLVED_DELAYED - inventory delay, usable ship date",
    ),
    DemoException(
        po_number="PO-4823",
        supplier_name="Initech Supply",
        recipient_name="Riley",
        recipient_phone_e164="+15550003333",
        hours_overdue=42,
        item_summary="200x fasteners",
        expect="HUMAN_REVIEW - answer is second-hand and unclear",
    ),
    DemoException(
        po_number="PO-4824",
        supplier_name="Umbrella Materials",
        recipient_name="Casey",
        recipient_phone_e164="+15550004444",
        hours_overdue=19,
        item_summary="5x steel coils",
        expect="HUMAN_REVIEW - supplier raises price, needs_human=yes",
    ),
    DemoException(
        po_number="PO-4825",
        supplier_name="Stark Fabrication",
        recipient_name="Avery",
        recipient_phone_e164="+15550005555",
        hours_overdue=55,
        item_summary="8x machined housings",
        expect="RETRY - recipient never reached",
    ),
    DemoException(
        po_number="PO-4826",
        supplier_name="Wayne Metals",
        recipient_name="Quinn",
        recipient_phone_e164="+15550006666",
        hours_overdue=61,
        item_summary="60x brackets",
        expect="RECONCILE then escalate - no schema-valid result",
    ),
    # Reaches the right NUMBER but the wrong PERSON: a clean "yes, on
    # time" answer that still cannot close the exception, because an
    # authorized destination number is not the same thing as an
    # authorized person. Demonstrates the identity gate in
    # transition_policy.decide() (schema v2, spoke_with field).
    DemoException(
        po_number="PO-4827",
        supplier_name="Helix Fasteners",
        recipient_name="Priya",
        recipient_phone_e164="+15550008888",
        hours_overdue=33,
        item_summary="15x titanium bolts",
        expect="HUMAN_REVIEW - reached the desk, not the authorized PO contact",
    ),
    # Not yet overdue: proves the eligibility gate refuses to call early.
    DemoException(
        po_number="PO-4830",
        supplier_name="Cyberdyne Parts",
        recipient_name="Morgan",
        recipient_phone_e164="+15550007777",
        hours_overdue=-12,
        item_summary="24x sensors",
        expect="NOT CALLED - acknowledgement is not due yet",
    ),
]


def _alembic_config():
    """Alembic config pointed at the repository's alembic.ini."""
    from alembic.config import Config

    root = pathlib.Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "backend" / "app" / "db" / "migrations"))
    return cfg


def init_db() -> None:
    """Bring the schema to head.

    Alembic rather than ``create_all``: a schema that can only be
    created from scratch is not a schema you can evolve, and the demo is
    supposed to look like something you could deploy twice.

    The extra check afterwards guards a failure mode that is confusing
    the first time you meet it. ``alembic_version`` is not part of the
    application metadata, so a ``drop_all`` removes every table *except*
    the version marker. Alembic then reads "already at head", applies
    nothing, and the app starts against an empty database. We detect
    that, clear the stale marker, and migrate for real.
    """
    from alembic import command

    config = _alembic_config()
    command.upgrade(config, "head")

    if _schema_is_present():
        print("Migrations applied (schema at head).")
        return

    print(
        "alembic_version claims head but the schema is missing "
        "(a drop_all leaves the version marker behind). Resetting it.",
        file=sys.stderr,
    )
    with get_engine().begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    command.upgrade(config, "head")
    if not _schema_is_present():
        raise SystemExit("migrations ran but the schema is still missing")
    print("Migrations applied (schema rebuilt).")


def _schema_is_present() -> bool:
    """True when the core application table actually exists."""
    from sqlalchemy import inspect

    return inspect(get_engine()).has_table("exceptions")


def drop_db() -> None:
    from alembic import command

    command.downgrade(_alembic_config(), "base")
    # The version table survives a downgrade to base; clear it too so
    # `reset` really does return to nothing.
    Base.metadata.drop_all(get_engine())
    with get_engine().begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    print("Schema removed.")


def seed() -> int:
    created = 0
    with session_scope() as session:
        for spec in DEMO_EXCEPTIONS:
            exists = (
                session.query(ExceptionRecord)
                .filter(ExceptionRecord.po_number == spec.po_number)
                .first()
            )
            if exists is not None:
                continue
            record = ExceptionRecord(
                po_number=spec.po_number,
                supplier_name=spec.supplier_name,
                recipient_name=spec.recipient_name,
                recipient_phone_e164=spec.recipient_phone_e164,
                ack_due_at=_now() - timedelta(hours=spec.hours_overdue),
                expected_ship_date=(_now() + timedelta(days=3)).date(),
                item_summary=spec.item_summary,
                state=ExceptionState.OPEN,
            )
            session.add(record)
            session.flush()
            repo.record_audit(
                session,
                entity_type="exception",
                entity_id=record.id,
                event_type="exception.created",
                actor=ActorType.SYSTEM,
                new_state=ExceptionState.OPEN.value,
                payload={"seeded": True, "expectation": spec.expect},
            )
            created += 1
    print(f"Seeded {created} exception(s).")
    return created


def status() -> None:
    with session_scope() as session:
        rows = repo.list_exceptions(session)
        if not rows:
            print("No exceptions. Run: resolve-e seed")
            return
        print(f"{'PO':<10} {'SUPPLIER':<22} {'STATE':<20} {'OVERDUE':>9}")
        print("-" * 65)
        for r in rows:
            due = r.ack_due_at if r.ack_due_at.tzinfo else r.ack_due_at.replace(tzinfo=UTC)
            overdue = (_now() - due).total_seconds() / 3600
            print(f"{r.po_number:<10} {r.supplier_name:<22} {r.state:<20} {overdue:>8.1f}h")


def verify_key() -> int:
    """Check the API key works WITHOUT placing a call.

    ``GET /v1/goals`` is a read, so this costs no call credit and rings
    no phone. Run it before ``smoke-test`` to separate an auth problem
    from a calling problem -- debugging both at once is exactly what
    ``docs/hackathon-build/build-notes.md`` warns against.
    """
    import httpx

    settings = get_settings()
    if not settings.calle_api_key:
        print("CALLE_API_KEY is not set in your environment or .env.", file=sys.stderr)
        return 2

    url = f"{settings.calle_base_url}/v1/goals"
    print(f"GET {url}")
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {settings.calle_api_key}"},
            timeout=settings.calle_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        print(f"Could not reach CALL-E: {exc}", file=sys.stderr)
        return 1

    if response.status_code in (401, 403):
        print(f"Key REJECTED ({response.status_code}). Check CALLE_API_KEY.", file=sys.stderr)
        return 1
    if response.status_code >= 500:
        print(f"CALL-E returned {response.status_code}; try again shortly.", file=sys.stderr)
        return 1

    print(f"Key accepted (HTTP {response.status_code}). No call was placed.")
    print("Next: resolve-e smoke-test +<your authorized E.164 number>")
    return 0


def smoke_test(phone: str) -> int:
    """Place ONE real CALL-E call. The master prompt's live requirement.

    This is intentionally a separate command and not part of any test
    run: it costs a call credit and rings a real phone.
    """
    settings = get_settings()
    if not settings.calle_api_key:
        print("CALLE_API_KEY is not set. Aborting.", file=sys.stderr)
        return 2

    if not settings.disclosure_ready:
        print(
            "BUYER_COMPANY is not set. The agent must be able to say "
            "truthfully whose behalf it is calling on. Aborting.",
            file=sys.stderr,
        )
        return 2

    from app.adapters.calle.schemas import (
        RECIPIENT_RESULT_SCHEMA,
        TASK_RESULT_SCHEMA,
        render_task,
    )
    from app.domain.commands import (
        CallRecipientCommand,
        CreateCallCommand,
        compute_batch_idempotency_key,
    )

    # Each invocation is a genuinely different logical attempt, so it
    # needs its own run id. A fixed one would derive the same
    # idempotency key every time and CALL-E would correctly hand back
    # the original call instead of dialling again -- the guarantee
    # working as designed, but making this command un-rerunnable.
    run_id = f"wr_smoke_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    recipient = CallRecipientCommand(
        workflow_run_id=run_id,
        exception_id="exc_smoke",
        attempt_no=1,
        po_number="PO-SMOKE-1",
        supplier_name="Resolve-E Smoke Test",
        recipient_name="there",
        phone_e164=phone,
    )
    recipients = (recipient,)
    command = CreateCallCommand(
        recipients=recipients,
        task=render_task(
            [
                {
                    "supplier_name": recipient.supplier_name,
                    "recipient_name": recipient.recipient_name or "",
                    "po_number": recipient.po_number,
                    "phone": recipient.phone_e164,
                }
            ],
            buyer_company=settings.buyer_company,
        ),
        result_schema=TASK_RESULT_SCHEMA,
        recipient_result_schema=RECIPIENT_RESULT_SCHEMA,
        idempotency_key=compute_batch_idempotency_key(recipients),
        webhook_url=settings.calle_webhook_url or None,
        metadata={"smoke_test": True},
    )

    print(f"Calling {phone} via {settings.calle_base_url} ...")
    print(f"Logical attempt: {run_id}")
    print(f"Idempotency-Key: {command.idempotency_key}")

    # Use whatever transport production would use, so a smoke test
    # exercises the real path rather than a second one that merely
    # resembles it.
    from app.services.provider import live_provider

    client = live_provider(settings)
    print(f"Transport: {type(client).__name__}")

    try:
        handle = client.create_call(command)
    except ProviderRateLimited as exc:
        # An expected, recoverable condition -- not a crash. No call was
        # placed and no credit was spent.
        print(f"\nRate limited by CALL-E: {exc.message}", file=sys.stderr)
        print("No call was placed and no credit was used.", file=sys.stderr)
        print(
            "The free plan caps calls per 24 hours. Wait for the window to "
            "roll over, or request more at https://forms.gle/EPQttEZ1rkW8iq9q6",
            file=sys.stderr,
        )
        return 3
    except ResolveEError as exc:
        print(f"\n{exc.error_class}: {exc.message}", file=sys.stderr)
        print(f"Retryable: {exc.retryable}", file=sys.stderr)
        return 1
    print(f"call_id     : {handle.provider_call_id}")
    print(f"status      : {handle.status}")
    print(f"deduplicated: {handle.deduplicated}")
    print("\nPoll with:")
    print(f"  resolve-e get-call {handle.provider_call_id}")
    return 0


def get_call(call_id: str) -> int:
    settings = get_settings()
    from app.adapters.calle.client import CalleClient

    client = CalleClient(settings.calle_api_key, base_url=settings.calle_base_url)
    call = client.get_call(call_id)
    print(
        json.dumps(
            {
                "id": call.provider_call_id,
                "status": call.status,
                "is_terminal": call.is_terminal,
                "task_completed": call.task_completed,
                "completion_confidence": (
                    {
                        "score": call.completion_confidence.score,
                        "label": call.completion_confidence.label,
                    }
                    if call.completion_confidence
                    else None
                ),
                "summary": call.summary,
                "evidence": list(call.evidence),
                "structured_result": call.raw_structured_result,
                "recipients": [
                    {
                        "id": r.provider_recipient_id,
                        "status": r.status,
                        "structured_result": r.raw_structured_result,
                        "summary": r.summary,
                    }
                    for r in call.recipients
                ],
                "failure_code": call.failure_code,
            },
            indent=2,
        )
    )
    return 0


def main() -> int:
    configure_logging(get_settings().log_level)
    parser = argparse.ArgumentParser(prog="resolve-e", description="Resolve-E operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="apply migrations up to head")
    sub.add_parser("drop-db", help="downgrade to base and remove all tables")
    sub.add_parser("seed", help="load the deterministic demo dataset")
    sub.add_parser("reset", help="drop, recreate and reseed")
    sub.add_parser("status", help="print the exception queue")
    sub.add_parser("verify-key", help="check CALLE_API_KEY without placing a call")

    smoke = sub.add_parser("smoke-test", help="place ONE real CALL-E call")
    smoke.add_argument("phone", help="an E.164 number you are authorized to call")

    getc = sub.add_parser("get-call", help="fetch a CALL-E call by id")
    getc.add_argument("call_id")

    args = parser.parse_args()

    match args.command:
        case "init-db":
            init_db()
        case "drop-db":
            drop_db()
        case "seed":
            seed()
        case "reset":
            drop_db()
            init_db()
            seed()
        case "status":
            status()
        case "verify-key":
            return verify_key()
        case "smoke-test":
            return smoke_test(args.phone)
        case "get-call":
            return get_call(args.call_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
