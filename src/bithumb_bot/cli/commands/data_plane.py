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
            request_interval_ms=int(args.request_interval_ms),
            max_retries=int(args.max_retries),
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
        f"retry_persistent_missing={summary['retry_persistent_missing']} "
        f"request_interval_ms={payload['filters']['request_interval_ms']} "
        f"max_retries={payload['filters']['max_retries']}"
    )
    return 0


def _probe(args: argparse.Namespace, _context) -> int:
    from bithumb_bot.research.data_plane import write_missing_candle_source_probe_artifact

    try:
        payload = write_missing_candle_source_probe_artifact(
            manifest_path=str(args.manifest),
            missing_ranges_path=str(args.missing_ranges),
            split=str(args.split) if args.split else None,
            limit=int(args.limit) if args.limit is not None else None,
            count=int(args.count),
            out_path=str(args.out),
        )
    except Exception as exc:
        print(f"[PROBE-MISSING-CANDLES] error={exc}")
        return 1
    summary = payload["summary"]
    print(
        "[PROBE-MISSING-CANDLES] "
        f"status=COMPLETE out={args.out} artifact_hash={payload['content_hash']} "
        f"target_present_true={summary['target_present_true']} "
        f"target_present_false={summary['target_present_false']} "
        f"api_error_count={summary['api_error_count']}"
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
            source_probe_path=str(args.source_probe) if args.source_probe else None,
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


def _clean_segments(args: argparse.Namespace, _context) -> int:
    from bithumb_bot.research.data_plane import write_clean_candle_segments_artifact

    try:
        payload = write_clean_candle_segments_artifact(
            market=str(args.market),
            interval=str(args.interval),
            min_days=int(args.min_days),
            out_path=str(args.out),
        )
    except Exception as exc:
        print(f"[FIND-CLEAN-CANDLE-SEGMENTS] error={exc}")
        return 1
    print(
        "[FIND-CLEAN-CANDLE-SEGMENTS] "
        f"status=COMPLETE out={args.out} artifact_hash={payload['content_hash']} "
        f"market={payload['market']} interval={payload['interval']} "
        f"min_segment_minutes={payload['min_segment_minutes']} segments={len(payload['segments'])}"
    )
    return 0


def command_specs() -> list[CommandSpec]:
    common = dict(domain="data_plane", read_only=False, mutating=True, produces_artifact=True)
    return [
        make_spec("research-missing-candles", handler=_missing, help="write a missing candle range artifact for a research manifest", description="Read-only SQL scan that writes UTC/KST missing candle ranges and retry UTC-day plans as a reports artifact.", build=_build_missing, **common),
        make_spec("retry-missing-candles", handler=_retry, help="retry selected missing candle ranges from a missing range artifact", description="Execute bounded targeted candle retries and write before/after coverage classification as an artifact.", build=_build_retry, writes_db=True, **common),
        make_spec("probe-missing-candles", handler=_probe, help="write direct Bithumb source probes for selected missing candle ranges", description="Probe selected missing candle targets directly against the public minute-candle API and write diagnostic source evidence as a reports artifact.", build=_build_probe, **common),
        make_spec("classify-persistent-missing-candles", handler=_classify, help="classify persistent missing candle ranges from retry evidence", description="Create a diagnostic-only persistent missing candle classification artifact. This never mutates the DB, generates synthetic OHLCV, or relaxes readiness gates.", build=_build_classify, **common),
        make_spec("find-clean-candle-segments", handler=_clean_segments, help="write clean contiguous candle segment discovery artifact", description="Read-only SQL scan that finds contiguous dense candle ranges and writes a clean_candle_segments reports artifact.", build=_build_clean_segments, **common),
    ]


def _build_missing(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)


def _build_retry(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--missing-ranges", required=True)
    parser.add_argument("--min-buckets", type=int, default=1)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--request-interval-ms", type=int, default=0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--split")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--out", required=True)


def _build_probe(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--missing-ranges", required=True)
    parser.add_argument("--split")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--out", required=True)


def _build_classify(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--missing-ranges", required=True)
    parser.add_argument("--retry-attempts", required=True)
    parser.add_argument("--source-probe")
    parser.add_argument("--out", required=True)


def _build_clean_segments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--market", required=True)
    parser.add_argument("--interval", required=True)
    parser.add_argument("--min-days", type=int, default=30)
    parser.add_argument("--out", required=True)
