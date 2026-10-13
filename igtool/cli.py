"""Command line interface: python -m igtool <command>."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from .client import GraphAPIError, GraphClient
from .config import Config, load_config
from .publish import KINDS, Publisher
from .report import Reporter, write_report
from .scheduler import ScheduledPost, Scheduler, ScheduleStore


def parse_when(value: str, tz_name: str) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM' (or ISO 8601) in the given zone; return UTC."""
    text = value.strip().replace("T", " ")
    dt: Optional[datetime] = None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        dt = datetime.fromisoformat(value.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(timezone.utc)


def parse_options(pairs: Optional[List[str]]) -> dict:
    options = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise argparse.ArgumentTypeError(f"option {pair!r} must look like key=value")
        key, value = pair.split("=", 1)
        options[key.strip()] = value.strip()
    return options


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m igtool",
        description="Schedule posts and pull insights for your own Instagram professional account via the official Graph API.",
    )
    parser.add_argument("--config", help="path to igtool.ini (default: igtool.ini or IGTOOL_CONFIG)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", help="show the account(s) the token can act for")
    sub.add_parser("limit", help="show the rolling 24h publishing quota")

    refresh = sub.add_parser("refresh-token", help="get a fresh long-lived token")
    refresh.add_argument("--app-id", default="", help="Facebook app id (Facebook Login only)")
    refresh.add_argument("--app-secret", default="", help="Facebook app secret (Facebook Login only)")

    def add_post_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--kind", choices=KINDS, required=True)
        p.add_argument("--media", action="append", required=True, help="public URL of the image/video; repeat for carousels")
        p.add_argument("--caption", default=None)
        p.add_argument("--option", action="append", metavar="KEY=VALUE", help="extra API field, e.g. share_to_feed=true, alt_text=...")

    publish = sub.add_parser("publish", help="publish a post right now")
    add_post_args(publish)

    schedule = sub.add_parser("schedule", help="manage the post queue")
    ssub = schedule.add_subparsers(dest="schedule_command", required=True)
    add = ssub.add_parser("add", help="queue a post")
    add.add_argument("--at", required=True, help="when to publish, e.g. '2026-10-14 09:00'")
    add.add_argument("--tz", default=None, help="IANA time zone for --at (default: timezone from config, else UTC)")
    add_post_args(add)
    lst = ssub.add_parser("list", help="list queued posts")
    lst.add_argument("--status", default=None, help="filter: pending, publishing, published, failed, cancelled")
    cancel = ssub.add_parser("cancel", help="cancel a pending post")
    cancel.add_argument("id", type=int)
    ssub.add_parser("run-once", help="publish everything that is due, then exit")
    run = ssub.add_parser("run", help="keep running and publish posts as they come due")
    run.add_argument("--interval", type=float, default=60.0, help="seconds between checks")

    report = sub.add_parser("report", help="build an insights report (markdown, JSON, CSV)")
    report.add_argument("--days", type=int, default=28, help="look back this many days (default 28)")
    report.add_argument("--since", help="start date YYYY-MM-DD (overrides --days)")
    report.add_argument("--until", help="end date YYYY-MM-DD (default: yesterday)")
    report.add_argument("--media-limit", type=int, default=25, help="max posts to include")
    report.add_argument("--out", default=None, help="output directory (default: report_dir from config)")
    report.add_argument("--print", action="store_true", dest="print_md", help="also print the markdown to stdout")
    return parser


def make_client(cfg: Config) -> GraphClient:
    cfg.validate()
    return GraphClient(cfg.access_token, host=cfg.api_host, version=cfg.api_version)


def cmd_whoami(cfg: Config) -> int:
    client = make_client(cfg)
    info = client.whoami(cfg.uses_instagram_login)
    print(json.dumps(info, indent=2))
    if info["mode"] == "facebook_login" and not info["accounts"]:
        print("No Instagram professional account is linked to a Page this token can see.", file=sys.stderr)
    return 0


def cmd_limit(cfg: Config) -> int:
    pub = Publisher(make_client(cfg), cfg.user_path)
    print(json.dumps(pub.publishing_limit(), indent=2))
    return 0


def cmd_refresh(cfg: Config, args: argparse.Namespace) -> int:
    client = make_client(cfg)
    body = client.refresh_long_lived_token(cfg.uses_instagram_login, args.app_id, args.app_secret)
    print(json.dumps(body, indent=2))
    print("Put the new access_token into igtool.ini.", file=sys.stderr)
    return 0


def cmd_publish(cfg: Config, args: argparse.Namespace) -> int:
    pub = Publisher(make_client(cfg), cfg.user_path)
    result = pub.publish_post(args.kind, args.media, args.caption, parse_options(args.option))
    print(json.dumps(result, indent=2))
    return 0


def cmd_schedule(cfg: Config, args: argparse.Namespace) -> int:
    store = ScheduleStore(cfg.database)
    try:
        if args.schedule_command == "add":
            when = parse_when(args.at, args.tz or cfg.timezone or "UTC")
            post = ScheduledPost(
                scheduled_at=when, kind=args.kind, media_urls=args.media, caption=args.caption, options=parse_options(args.option)
            )
            post_id = store.add(post)
            print(f"queued post {post_id} for {when.isoformat()} (UTC)")
            return 0
        if args.schedule_command == "list":
            posts = store.list(args.status)
            if not posts:
                print("queue is empty")
                return 0
            for p in posts:
                extra = f" media={p.media_id}" if p.media_id else ""
                extra += f" error={p.last_error}" if p.last_error else ""
                print(f"#{p.id} {p.scheduled_at.isoformat()} {p.status:<10} {p.kind:<8} {len(p.media_urls)} url(s) {p.caption or ''!r}{extra}")
            return 0
        if args.schedule_command == "cancel":
            ok = store.cancel(args.id)
            print("cancelled" if ok else "not cancelled (not found or not pending)")
            return 0 if ok else 1
        scheduler = Scheduler(store, Publisher(make_client(cfg), cfg.user_path))
        if args.schedule_command == "run-once":
            handled = scheduler.run_once()
            for p in handled:
                print(f"#{p.id} -> {p.status}" + (f" media={p.media_id}" if p.media_id else f" error={p.last_error}"))
            if not handled:
                print("nothing due")
            return 0
        scheduler.run_forever(args.interval)
        return 0
    finally:
        store.close()


def cmd_report(cfg: Config, args: argparse.Namespace) -> int:
    until = date.fromisoformat(args.until) if args.until else date.today() - timedelta(days=1)
    since = date.fromisoformat(args.since) if args.since else until - timedelta(days=args.days - 1)
    reporter = Reporter(make_client(cfg), cfg)
    data = reporter.build(since, until, args.media_limit)
    paths = write_report(data, args.out or cfg.report_dir)
    if args.print_md:
        from .report import render_markdown

        print(render_markdown(data))
    for kind, path in paths.items():
        print(f"{kind}: {path}", file=sys.stderr)
    if data["warnings"]:
        print(f"{len(data['warnings'])} metric(s) unavailable, see the Notes section.", file=sys.stderr)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(args.config)
    try:
        if args.command == "whoami":
            return cmd_whoami(cfg)
        if args.command == "limit":
            return cmd_limit(cfg)
        if args.command == "refresh-token":
            return cmd_refresh(cfg, args)
        if args.command == "publish":
            return cmd_publish(cfg, args)
        if args.command == "schedule":
            return cmd_schedule(cfg, args)
        if args.command == "report":
            return cmd_report(cfg, args)
    except (GraphAPIError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    parser.error("unknown command")
    return 2
