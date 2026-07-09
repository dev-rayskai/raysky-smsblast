#!/usr/bin/env python3
"""
Bulk SMS campaign sender for a one-time clinic campaign (Canadian patients).
"""

import argparse
import csv
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter

DEFAULT_TEMPLATE = (
    "Hi {first_name}, this is a message from your clinic. "
    "Reply STOP to unsubscribe."
)

RESULTS_FIELDS = [
    "phone", "first_name", "last_name", "status", "message_sid",
    "error_code", "error_message", "timestamp",
]

PERMANENT_ERROR_CODES = {
    21211,
    21610,
    21612,
    21614,
    21408,
    30006,
}

log = logging.getLogger("campaign")


@dataclass
class Config:
    csv_path: Path
    results_path: Path
    template: str
    batch_size: int
    batch_pause: float
    rate: float
    dry_run: bool
    resume: bool
    limit: int | None
    yes: bool
    account_sid: str = ""
    auth_token: str = ""
    messaging_service_sid: str = ""
    from_number: str = ""


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def build_config(args: argparse.Namespace) -> Config:
    load_dotenv(Path(__file__).parent / ".env")

    if args.template_file:
        template = Path(args.template_file).read_text().strip()
    else:
        template = args.template or os.environ.get("MESSAGE_TEMPLATE") or DEFAULT_TEMPLATE

    cfg = Config(
        csv_path=Path(args.csv),
        results_path=Path(args.results),
        template=template,
        batch_size=args.batch_size,
        batch_pause=args.batch_pause,
        rate=args.rate,
        dry_run=args.dry_run,
        resume=args.resume,
        limit=args.limit,
        yes=args.yes,
        account_sid=os.environ.get("TWILIO_ACCOUNT_SID", ""),
        auth_token=os.environ.get("TWILIO_AUTH_TOKEN", ""),
        messaging_service_sid=os.environ.get("TWILIO_MESSAGING_SERVICE_SID", ""),
        from_number=os.environ.get("TWILIO_FROM_NUMBER", ""),
    )

    if cfg.batch_size < 1:
        sys.exit("error: --batch-size must be >= 1")
    if cfg.rate <= 0:
        sys.exit("error: --rate must be > 0")
    needs_credentials = args.command == "status" or not cfg.dry_run
    if needs_credentials:
        missing = [k for k, v in [
            ("TWILIO_ACCOUNT_SID", cfg.account_sid),
            ("TWILIO_AUTH_TOKEN", cfg.auth_token),
        ] if not v]
        if missing:
            sys.exit(f"error: missing environment variables: {', '.join(missing)}")
    if args.command == "send" and not cfg.dry_run:
        if not cfg.messaging_service_sid and not cfg.from_number:
            sys.exit("error: set TWILIO_MESSAGING_SERVICE_SID or TWILIO_FROM_NUMBER")
    return cfg


PHONE_COLUMNS = ("phone", "phone_number", "mobile", "cell", "number")
NAME_COLUMNS = ("first_name", "firstname", "given_name", "name")
LAST_NAME_COLUMNS = ("last_name", "lastname", "surname", "family_name")


@dataclass
class Recipient:
    phone: str
    first_name: str
    last_name: str
    row: int


def normalize_canadian_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    if digits[0] in "01" or digits[3] in "01":
        return None
    return f"+1{digits}"


def pick_column(fieldnames: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {f.lower().strip(): f for f in fieldnames}
    for c in candidates:
        if c in lowered:
            return lowered[c]
    return None


def load_recipients(cfg: Config) -> list[Recipient]:
    if not cfg.csv_path.exists():
        sys.exit(f"error: CSV file not found: {cfg.csv_path}")

    with open(cfg.csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            sys.exit("error: CSV file is empty")
        phone_col = pick_column(reader.fieldnames, PHONE_COLUMNS)
        name_col = pick_column(reader.fieldnames, NAME_COLUMNS)
        last_name_col = pick_column(reader.fieldnames, LAST_NAME_COLUMNS)
        if not phone_col:
            sys.exit(f"error: no phone column found. Expected one of {PHONE_COLUMNS}")
        if not name_col:
            log.warning("No first-name column found.")

        recipients: list[Recipient] = []
        seen: set[str] = set()
        skipped_invalid = skipped_dupe = 0

        for i, row in enumerate(reader, start=1):
            phone = normalize_canadian_phone(row.get(phone_col, ""))
            if not phone:
                skipped_invalid += 1
                log.warning("Row %d: invalid phone %r -- skipped", i, row.get(phone_col))
                continue
            if phone in seen:
                skipped_dupe += 1
                log.warning("Row %d: duplicate phone %s -- skipped", i, phone)
                continue
            seen.add(phone)
            first_name = (row.get(name_col, "") or "").strip() if name_col else ""
            last_name = (row.get(last_name_col, "") or "").strip() if last_name_col else ""
            recipients.append(Recipient(phone=phone, first_name=first_name, last_name=last_name, row=i))

    log.info("Loaded %d valid recipients (%d invalid, %d duplicates skipped)",
             len(recipients), skipped_invalid, skipped_dupe)
    return recipients


class ResultsLog:
    def __init__(self, path: Path):
        self.path = path
        exists = path.exists() and path.stat().st_size > 0
        self._fh = open(path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=RESULTS_FIELDS)
        if not exists:
            self._writer.writeheader()
            self._fh.flush()

    def already_sent(self) -> set[str]:
        if not self.path.exists():
            return set()
        with open(self.path, newline="", encoding="utf-8") as f:
            return {r["phone"] for r in csv.DictReader(f) if r.get("status") == "sent"}

    def record(self, r: Recipient, status: str, sid: str = "",
               error_code: str = "", error_message: str = "") -> None:
        self._writer.writerow({
            "phone": r.phone,
            "first_name": r.first_name,
            "last_name": r.last_name,
            "status": status,
            "message_sid": sid,
            "error_code": error_code,
            "error_message": error_message[:500],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class RateLimiter:
    def __init__(self, rate_per_sec: float):
        self.min_interval = 1.0 / rate_per_sec
        self._last = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


def render_message(template: str, r: Recipient) -> str:
    fields = {
        "first_name": r.first_name or "there",
        "last_name": r.last_name or "",
        "phone": r.phone
    }
    try:
        return template.format(**fields)
    except (KeyError, IndexError) as e:
        raise ValueError(f"template references unknown placeholder: {e}") from e


def validate_template(template: str) -> None:
    placeholders = {name for _, name, _, _ in Formatter().parse(template) if name}
    unknown = placeholders - {"first_name", "last_name", "phone"}
    if unknown:
        sys.exit(f"error: template uses unsupported placeholders: {sorted(unknown)}")


def make_twilio_client(cfg: Config):
    try:
        from twilio.rest import Client
    except ImportError:
        sys.exit("error: twilio package not installed. Run: pip install twilio")
    return Client(cfg.account_sid, cfg.auth_token)


def send_one(client, cfg: Config, r: Recipient, body: str, max_retries: int = 3):
    from twilio.base.exceptions import TwilioRestException

    delay = 2.0
    for attempt in range(1, max_retries + 1):
        try:
            kwargs = {"to": r.phone, "body": body}
            if cfg.messaging_service_sid:
                kwargs["messaging_service_sid"] = cfg.messaging_service_sid
            else:
                kwargs["from_"] = cfg.from_number
            msg = client.messages.create(**kwargs)
            return "sent", msg.sid, "", ""
        except TwilioRestException as e:
            code = e.code or 0
            transient = e.status == 429 or e.status >= 500 or code == 20429
            if code in PERMANENT_ERROR_CODES or not transient:
                return "failed", "", str(code), str(e.msg)
            if attempt == max_retries:
                return "failed", "", str(code), f"gave up after {max_retries} retries: {e.msg}"
            log.warning("Transient error %s for %s, retrying in %.0fs", code, r.phone, delay)
            time.sleep(delay)
            delay *= 2
        except Exception as e:
            if attempt == max_retries:
                return "failed", "", "", f"unexpected error: {e}"
            log.warning("Error sending to %s: %s -- retrying in %.0fs", r.phone, e, delay)
            time.sleep(delay)
            delay *= 2


def run_campaign(cfg: Config) -> int:
    validate_template(cfg.template)
    recipients = load_recipients(cfg)
    results = ResultsLog(cfg.results_path)

    if cfg.resume:
        done = results.already_sent()
        before = len(recipients)
        recipients = [r for r in recipients if r.phone not in done]
        log.info("Resume: skipping %d already-sent recipients", before - len(recipients))

    if cfg.limit:
        recipients = recipients[:cfg.limit]

    if not recipients:
        log.info("Nothing to send.")
        return 0

    n = len(recipients)
    n_batches = (n + cfg.batch_size - 1) // cfg.batch_size
    est_secs = n / cfg.rate + (n_batches - 1) * cfg.batch_pause
    sender = cfg.messaging_service_sid or cfg.from_number or "(dry run)"

    print(f"\n{'DRY RUN -- nothing will be sent' if cfg.dry_run else 'LIVE SEND'}")
    print(f"  Recipients : {n}")
    print(f"  Batches    : {n_batches} x {cfg.batch_size}")
    print(f"  Rate       : {cfg.rate:g} msg/sec")
    print(f"  Sender     : {sender}")
    print(f"  Est. time  : {est_secs / 60:.0f} min")
    print(f"  Sample msg : {render_message(cfg.template, recipients[0])!r}\n")

    if not cfg.dry_run and not cfg.yes:
        answer = input(f"Type 'send' to confirm sending {n} live SMS messages: ")
        if answer.strip().lower() != "send":
            print("Aborted.")
            return 1

    client = None if cfg.dry_run else make_twilio_client(cfg)
    limiter = RateLimiter(cfg.rate)
    sent = failed = 0
    interrupted = {"flag": False}

    def on_sigint(_sig, _frame):
        interrupted["flag"] = True

    signal.signal(signal.SIGINT, on_sigint)
    start = time.monotonic()

    try:
        for b in range(n_batches):
            batch = recipients[b * cfg.batch_size:(b + 1) * cfg.batch_size]
            log.info("--- Batch %d/%d (%d messages) ---", b + 1, n_batches, len(batch))

            for r in batch:
                if interrupted["flag"]:
                    raise KeyboardInterrupt
                body = render_message(cfg.template, r)
                limiter.wait()
                if cfg.dry_run:
                    status, sid, ecode, emsg = "dry-run", "", "", ""
                else:
                    status, sid, ecode, emsg = send_one(client, cfg, r, body)
                results.record(r, status, sid, ecode, emsg)
                if status == "failed":
                    failed += 1
                    log.error("FAILED %s: [%s] %s", r.phone, ecode, emsg)
                else:
                    sent += 1
                    log.info("%s %s", status.upper(), r.phone)

            if b < n_batches - 1 and not interrupted["flag"]:
                log.info("Batch %d done -- pausing %.0fs", b + 1, cfg.batch_pause)
                time.sleep(cfg.batch_pause)
    except KeyboardInterrupt:
        pass
    finally:
        results.close()

    elapsed = time.monotonic() - start
    print(f"\nDone in {elapsed / 60:.1f} min: {sent} sent, {failed} failed.")
    print(f"Full results: {cfg.results_path}")
    return 0 if failed == 0 else 2


def check_status(cfg: Config) -> int:
    if not cfg.results_path.exists():
        sys.exit(f"error: results file not found: {cfg.results_path}")
    client = make_twilio_client(cfg)

    with open(cfg.results_path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("message_sid")]

    counts: dict[str, int] = {}
    undelivered: list[tuple[str, str, str]] = []
    for row in rows:
        try:
            msg = client.messages(row["message_sid"]).fetch()
            status = msg.status
            if status in ("undelivered", "failed"):
                undelivered.append((row["phone"], status, str(msg.error_code or "")))
        except Exception as e:
            status = "lookup-error"
            log.warning("Could not fetch %s: %s", row["message_sid"], e)
        counts[status] = counts.get(status, 0) + 1

    print(f"\nDelivery status for {len(rows)} messages:")
    for status, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {status:>14}: {count}")
    if undelivered:
        print("\nUndelivered/failed:")
        for phone, status, code in undelivered:
            print(f"  {phone}  {status}  error_code={code}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk SMS campaign sender (Twilio).")
    parser.add_argument("--csv", default="patients.csv")
    parser.add_argument("--results", default="results.csv")
    parser.add_argument("--template")
    parser.add_argument("--template-file")
    parser.add_argument("--batch-size", type=int, default=300)
    parser.add_argument("--batch-pause", type=float, default=60)
    parser.add_argument("--rate", type=float, default=1.0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--check-status", action="store_true")
    parser.add_argument("--log-file", default="campaign.log")
    args = parser.parse_args()
    args.command = "status" if args.check_status else "send"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(args.log_file, encoding="utf-8"),
        ],
    )

    cfg = build_config(args)
    if args.command == "status":
        return check_status(cfg)
    return run_campaign(cfg)


if __name__ == "__main__":
    sys.exit(main())