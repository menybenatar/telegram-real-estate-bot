"""Entry point: search -> filter -> deduplicate -> notify.

Exit codes: 0 = run completed, 1 = a critical operation failed (search blocked/failed, Telegram failure),
2 = invalid configuration or missing credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import AppConfig, ConfigError, load_config
from .deduplication import dedupe
from .filters import evaluate
from .logging_config import log_fields, register_secret, setup_logging
from .models import Listing
from .search.base import ListingProvider, ProviderError
from .search.mock import MockProvider
from .search.yad2 import Yad2Provider
from .storage import Storage
from .telegram_notifier import DryRunNotifier, TelegramError, TelegramNotifier

logger = logging.getLogger("real_estate_bot")

ERROR_NOTICE_COOLDOWN = timedelta(hours=24)


@dataclass
class RunResult:
    matched: int = 0
    new: int = 0
    sent: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0


def load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader for local development (does not override real environment variables)."""
    file = Path(path)
    if not file.is_file():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def run(config: AppConfig, provider: ListingProvider, storage: Storage, notifier, *, baseline: bool = False) -> RunResult:
    settings = config.settings
    result = RunResult()
    matched: list[Listing] = []

    for search in config.enabled_searches:
        try:
            fetched = provider.search(search)
        except ProviderError as exc:
            msg = f"search '{search.name}' failed: {type(exc).__name__}: {exc}"
            logger.error(msg, extra=log_fields(search=search.name))
            result.failures.append(msg)
            continue  # one failing search must not hide results of the others

        rejected: Counter[str] = Counter()
        accepted = 0
        for listing in fetched:
            verdict = evaluate(listing, search, settings)
            if verdict.accepted:
                matched.append(replace(listing, seller_status=verdict.seller_status))
                accepted += 1
            else:
                rejected[verdict.reason or "unknown"] += 1
        logger.info(
            "search finished",
            extra=log_fields(search=search.name, fetched=len(fetched), matched=accepted, rejected=dict(rejected)),
        )

    unique = dedupe(matched)
    result.matched = len(unique)

    status = "baseline" if baseline else "pending"
    for listing in unique:
        if storage.register(listing, status=status):
            result.new += 1

    expired = storage.expire_pending(settings.pending_expiry_days)
    purged = storage.purge(settings.retention_days)
    if expired or purged:
        logger.info("housekeeping", extra=log_fields(expired_pending=expired, purged=purged))

    if baseline:
        logger.info("baseline run: current matches recorded as seen, nothing sent", extra=log_fields(recorded=result.new))
    else:
        _send_pending(storage, notifier, settings, result)

    logger.info(
        "run finished",
        extra=log_fields(matched=result.matched, new=result.new, sent=result.sent, failures=len(result.failures),
                         pending_left=storage.pending_count()),
    )
    return result


def _send_pending(storage: Storage, notifier, settings, result: RunResult) -> None:
    queue = storage.pending(limit=settings.max_notifications_per_run)
    total_pending = storage.pending_count()
    if total_pending > len(queue):
        logger.info("more pending than the per-run cap; rest waits for next run",
                    extra=log_fields(cap=settings.max_notifications_per_run, pending=total_pending))
    for listing in queue:
        try:
            sent = notifier.send_listing(listing)
        except TelegramError as exc:
            if exc.fatal:
                msg = f"Telegram is not usable: {exc}"
                logger.error(msg)
                result.failures.append(msg)
                return  # every further send would fail the same way; listings stay pending
            status = storage.record_failure(listing.key, str(exc), settings.max_send_attempts)
            msg = f"could not send {listing.key}: {exc} (status now {status})"
            logger.error(msg)
            result.failures.append(msg)
            continue
        storage.mark_sent(listing.key, note=sent.photo_error)
        result.sent += 1
        logger.info("notification sent", extra=log_fields(listing=listing.key, photo=sent.used_photo))


def maybe_send_error_notice(storage: Storage, notifier, failures: list[str], now: datetime | None = None) -> bool:
    """Alert on Telegram about a failed run, at most once per 24h for the same problem (avoids spam every 6h)."""
    now = now or datetime.now(timezone.utc)
    text = "\n".join(failures)[:1500]
    signature = hashlib.sha256(text.encode()).hexdigest()[:16]
    last = storage.get_meta("last_error_notice")
    if last:
        last_sig, _, last_time = last.partition("|")
        try:
            if last_sig == signature and now - datetime.fromisoformat(last_time) < ERROR_NOTICE_COOLDOWN:
                return False
        except ValueError:
            pass
    if notifier.send_error(text):
        storage.set_meta("last_error_notice", f"{signature}|{now.isoformat(timespec='seconds')}")
        return True
    return False


def build_provider(args: argparse.Namespace, config: AppConfig) -> ListingProvider:
    s = config.settings
    if args.provider == "mock":
        if not args.mock_file:
            raise ConfigError(["--provider mock requires --mock-file"])
        return MockProvider(args.mock_file)
    return Yad2Provider(
        user_agent=s.user_agent,
        request_delay=s.request_delay_seconds,
        timeout=s.request_timeout_seconds,
        max_retries=s.max_http_retries,
        respect_robots=s.respect_robots_txt,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="real_estate_bot", description="Telegram alerts for new real-estate listings.")
    p.add_argument("--config", default=os.environ.get("CONFIG_PATH", "config.yaml"))
    p.add_argument("--db", default=None, help="SQLite path (default: $DB_PATH or data/listings.db)")
    p.add_argument("--provider", choices=["yad2", "mock"], default="yad2")
    p.add_argument("--mock-file", help="JSON file of normalized listings (with --provider mock)")
    p.add_argument("--dry-run", action="store_true", help="print messages instead of sending to Telegram")
    p.add_argument("--baseline", action="store_true", help="record current matches as already seen, send nothing")
    p.add_argument("--probe", action="store_true", help="make ONE request to Yad2 and print diagnostics")
    p.add_argument("--validate-config", action="store_true", help="validate config.yaml and exit")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)
    setup_logging()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    register_secret(token)

    try:
        config = load_config(args.config)
        if args.validate_config:
            print(f"OK: {len(config.searches)} searches ({len(config.enabled_searches)} enabled)")
            return 0
        provider = build_provider(args, config)

        if args.probe:
            if not isinstance(provider, Yad2Provider):
                raise ConfigError(["--probe only works with the yad2 provider"])
            report = provider.probe(config.enabled_searches[0])
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            return 0 if str(report.get("verdict", "")).startswith("OK") else 1

        if args.dry_run:
            notifier = DryRunNotifier(config.settings.language)
            db_path = args.db or "dry_run.db"
        else:
            if not token or not chat_id:
                raise ConfigError(["TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (see .env.example)"])
            notifier = TelegramNotifier(
                token, chat_id, language=config.settings.language, send_delay=config.settings.send_delay_seconds,
                timeout=config.settings.request_timeout_seconds,
            )
            db_path = args.db or os.environ.get("DB_PATH", "data/listings.db")
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    storage = Storage(db_path)
    try:
        result = run(config, provider, storage, notifier, baseline=args.baseline)
        if result.failures and config.settings.notify_on_error and not args.baseline:
            maybe_send_error_notice(storage, notifier, result.failures)
        return result.exit_code
    finally:
        storage.close()  # release the file before the workflow commits it


def cli() -> None:
    sys.exit(main())


if __name__ == "__main__":
    cli()
