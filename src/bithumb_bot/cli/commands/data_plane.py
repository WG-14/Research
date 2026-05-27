from __future__ import annotations

import argparse

from bithumb_bot.cli.registry import CommandSpec

from ._helpers import make_spec


def _missing(args: argparse.Namespace, _context) -> int:
    from bithumb_bot.research.data_plane import write_missing_candle_ranges_artifact

    try:
        payload = write_missing_candle_ranges_artifact(manifest_path=str(args.manifest), out_path=str(args.out))
    except Exception as exc:
        print(f"[RESEARCH-MISSING-CANDLES] error={exc}")
        return 1
    total_missing = sum(int(split.get("missing_buckets") or 0) for split in (payload.get("splits") or {}).values())
    print(
        "[RESEARCH-MISSING-CANDLES] "
        f"status=COMPLETE out={args.out} manifest_hash={payload['manifest_hash']} "
        f"db_path={payload['db_path']} market={payload['market']} interval={payload['interval']} "
        f"missing_buckets={total_missing}"
    )
    return 0


def _retry(args: argparse.Namespace, _context) -> int:
    from bithumb_bot.research.data_plane import retry_missing_candles_from_artifact

    try:
        payload = retry_missing_candles_from_artifact(
            manifest_path=str(args.manifest),
            missing_ranges_path=str(args.missing_ranges),
            min_buckets=int(args.min_buckets),
            max_attempts=int(args.max_attempts),
            split=str(args.split) if args.split else None,
            limit=int(args.limit) if args.limit is not None else None,
            out_path=str(args.out),
        )
    except Exception as exc:
        print(f"[RETRY-MISSING-CANDLES] error={exc}")
        return 1
    summary = payload["summary"]
    print(
        "[RETRY-MISSING-CANDLES] "
        f"status=COMPLETE out={args.out} attempts={payload['attempt_count']} "
        f"retried_recovered={summary['retried_recovered']} "
        f"retry_persistent_missing={summary['retry_persistent_missing']}"
    )
    return 0


def _classify(args: argparse.Namespace, _context) -> int:
    from bithumb_bot.research.data_plane import (
        persistent_missing_overall_next_action,
        write_persistent_missing_candle_classification_artifact,
    )

    try:
        payload = write_persistent_missing_candle_classification_artifact(
            manifest_path=str(args.manifest),
            missing_ranges_path=str(args.missing_ranges),
            retry_attempts_path=str(args.retry_attempts),
            out_path=str(args.out),
        )
    except Exception as exc:
        print(f"[CLASSIFY-PERSISTENT-MISSING-CANDLES] error={exc}")
        return 1
    summary = payload["summary"]
    next_action = persistent_missing_overall_next_action(summary)
    print(
        "[CLASSIFY-PERSISTENT-MISSING-CANDLES] "
        f"status=COMPLETE out={args.out} artifact_hash={payload['content_hash']} "
        f"exchange_gap_candidate={summary['exchange_gap_candidate']} "
        f"api_unavailable_candidate={summary['api_unavailable_candidate']} "
        f"no_trade_missing_candidate={summary['no_trade_missing_candidate']} "
        f"unclassified_missing={summary['unclassified_missing']} "
        f"persistent_range_count={summary['persistent_range_count']} "
        "production_gate_effect=none synthetic_ohlcv_authorized=0 "
        f"next_action={next_action}"
    )
    return 0


def command_specs() -> list[CommandSpec]:
    common = dict(domain="data_plane", read_only=False, mutating=True, produces_artifact=True)
    return [
        make_spec("research-missing-candles", handler=_missing, help="write a missing candle range artifact for a research manifest", description="Read-only SQL scan that writes UTC/KST missing candle ranges and retry UTC-day plans as a reports artifact.", build=_build_missing, **common),
        make_spec("retry-missing-candles", handler=_retry, help="retry selected missing candle ranges from a missing range artifact", description="Execute bounded targeted candle retries and write before/after coverage classification as an artifact.", build=_build_retry, writes_db=True, **common),
        make_spec("classify-persistent-missing-candles", handler=_classify, help="classify persistent missing candle ranges from retry evidence", description="Create a diagnostic-only persistent missing candle classification artifact. This never mutates the DB, generates synthetic OHLCV, or relaxes readiness gates.", build=_build_classify, **common),
    ]


def _build_missing(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)


def _build_retry(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--missing-ranges", required=True)
    parser.add_argument("--min-buckets", type=int, default=1)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--split")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--out", required=True)


def _build_classify(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--missing-ranges", required=True)
    parser.add_argument("--retry-attempts", required=True)
    parser.add_argument("--out", required=True)
