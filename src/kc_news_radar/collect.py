"""``python -m kc_news_radar.collect`` — run collectors and refresh pipeline.

Usage:

    python -m kc_news_radar.collect
    python -m kc_news_radar.collect --source nws_kc
    python -m kc_news_radar.collect --list
"""

from __future__ import annotations

import argparse
import logging
from .collectors import build_all
from .collection_runtime import execute_collection_cycle
from .config import load_settings
from .models import CollectionRunState, CollectionTrigger


log = logging.getLogger("kc_news_radar.collect")


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
    selection = "explicit KC_NEWS_RADAR_DB" if settings.db_path_explicit else "default path"
    print(f"database ({selection}): {settings.db_path}", flush=True)
    result = execute_collection_cycle(
        trigger_type=CollectionTrigger.MANUAL,
        only_source=args.source,
        settings=settings,
        reset_demo=settings.demo_mode,
    )
    print(
        f"run {result.run_id}: {result.state.value}; "
        f"sources={result.sources_succeeded}/{result.sources_attempted} succeeded, "
        f"failed={result.sources_failed}; items={result.items_collected}; "
        f"updated={result.items_updated}",
        flush=True,
    )
    if result.pipeline_result:
        print(
            f"pipeline: {result.pipeline_result['signals_written']} signals, "
            f"{result.pipeline_result['forecasts_written']} forecast versions "
            f"from {result.pipeline_result['items_considered']} items",
            flush=True,
        )
    if result.failure_summary:
        print(f"note: {result.failure_summary}", flush=True)
    if result.state == CollectionRunState.BLOCKED_OVERLAP:
        return 3
    if result.state == CollectionRunState.FAILED:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
