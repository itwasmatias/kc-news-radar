"""``python -m kc_news_radar.collect`` — run collectors and refresh pipeline.

Usage:

    python -m kc_news_radar.collect
    python -m kc_news_radar.collect --source nws_kc
    python -m kc_news_radar.collect --list
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from . import db as dbmod
from .collectors import build_all
from .config import load_settings
from .demo import load_demo_fixtures
from .pipeline.forecasting import run_pipeline


log = logging.getLogger("kc_news_radar.collect")


def _run_live_collect(only: str | None) -> tuple[int, int, list[dict]]:
    settings = load_settings()
    dbmod.init_db(settings.db_path)
    collectors = build_all(http_timeout=settings.http_timeout, user_agent=settings.user_agent)
    if only:
        collectors = [c for c in collectors if c.name == only]
        if not collectors:
            print(f"error: no collector named {only!r}", file=sys.stderr)
            sys.exit(2)

    total_items = 0
    total_updated = 0
    health_summary: list[dict] = []

    with dbmod.connect(settings.db_path) as conn:
        for collector in collectors:
            result = collector.run()
            with dbmod.transaction(conn):
                for item in result.items:
                    _, was_updated = dbmod.upsert_source_item(conn, item)
                    total_items += 1
                    if was_updated:
                        total_updated += 1
                dbmod.upsert_source_health(conn, result.health)
            health_summary.append({
                "source_name": result.source_name,
                "status": result.health.status.value,
                "items": result.health.item_count,
                "latency_ms": result.health.latency_ms,
                "error": result.health.error_message,
            })
            print(
                f"  {result.source_name:22} {result.health.status.value:8} "
                f"items={result.health.item_count:>4} lat={result.health.latency_ms:>5}ms",
                flush=True,
            )
        pipeline_result = run_pipeline(conn)

    print(
        f"\ncollected {total_items} items ({total_updated} content-updated)\n"
        f"pipeline: {pipeline_result['signals_written']} signals, "
        f"{pipeline_result['forecasts_written']} forecast versions (from "
        f"{pipeline_result['items_considered']} items considered)"
    )
    return total_items, total_updated, health_summary


def _run_demo() -> None:
    settings = load_settings()
    dbmod.init_db(settings.db_path)
    with dbmod.connect(settings.db_path) as conn:
        load_demo_fixtures(conn)
        with dbmod.transaction(conn):
            pipeline_result = run_pipeline(conn)
    print(
        f"DEMO MODE: loaded fixtures. "
        f"{pipeline_result['signals_written']} signals, "
        f"{pipeline_result['forecasts_written']} forecast versions."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kc-news-radar collect",
        description="Collect KC-area public sources and refresh the radar pipeline.",
    )
    parser.add_argument("--source", help="Run only the named collector (e.g. nws_kc)")
    parser.add_argument("--list", action="store_true", help="List available collectors and exit")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.list:
        for c in build_all(http_timeout=10.0, user_agent="KCNewsRadar/0.1"):
            print(f"  {c.name:22} {c.label}")
        return 0

    settings = load_settings()
    if settings.demo_mode:
        _run_demo()
        return 0

    _run_live_collect(args.source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
