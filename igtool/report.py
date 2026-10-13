"""Account and post insights pulled from the Graph API, rendered as files."""

from __future__ import annotations

import csv
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .client import GraphAPIError, GraphClient
from .config import Config

log = logging.getLogger("igtool.report")

# The API refuses time-series requests spanning more than 30 days.
MAX_SERIES_WINDOW_DAYS = 30
MEDIA_FIELDS = "id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count"


def chunk_range(since: date, until: date, days: int = MAX_SERIES_WINDOW_DAYS) -> List[Tuple[date, date]]:
    """Split [since, until] (inclusive) into windows of at most ``days`` days."""
    if until < since:
        raise ValueError("until must not be before since")
    chunks = []
    start = since
    while start <= until:
        end = min(start + timedelta(days=days - 1), until)
        chunks.append((start, end))
        start = end + timedelta(days=1)
    return chunks


def parse_timestamp(value: str) -> datetime:
    """Parse Graph API timestamps such as 2026-10-02T10:00:00+0000."""
    text = value.strip().replace("Z", "+00:00")
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _unix(d: date, end_of_day: bool = False) -> int:
    dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    if end_of_day:
        dt += timedelta(days=1, seconds=-1)
    return int(dt.timestamp())


def _metric_value(entry: Dict[str, Any]) -> Any:
    """Insights entries come in two shapes: total_value or values[]."""
    if "total_value" in entry:
        return entry["total_value"].get("value")
    values = entry.get("values") or []
    if not values:
        return None
    if len(values) == 1:
        return values[0].get("value")
    return [v.get("value") for v in values]


class Reporter:
    def __init__(self, client: GraphClient, config: Config):
        self.client = client
        self.config = config
        self.user_path = config.user_path
        self.warnings: List[str] = []

    # --------------------------------------------------------------- account
    def account_summary(self) -> Dict[str, Any]:
        fields = "id,username,name,biography,website,followers_count,follows_count,media_count,profile_picture_url"
        return self.client.get(self.user_path, fields=fields)

    def account_series(self, since: date, until: date) -> Dict[str, Dict[str, Any]]:
        """Daily values keyed by date string, e.g. {'2026-10-01': {'reach': 12, 'follower_count': 345}}."""
        daily: Dict[str, Dict[str, Any]] = {}
        for metric in self.config.account_series_metrics:
            for start, end in chunk_range(since, until):
                try:
                    body = self.client.get(
                        f"{self.user_path}/insights",
                        metric=metric,
                        period="day",
                        since=_unix(start),
                        until=_unix(end, end_of_day=True),
                    )
                except GraphAPIError as exc:
                    self._warn(f"account metric {metric!r} ({start}..{end}) skipped: {exc.message}")
                    break
                for entry in body.get("data", []):
                    for point in entry.get("values", []):
                        day = str(point.get("end_time", ""))[:10]
                        daily.setdefault(day, {})[entry["name"]] = point.get("value")
        return dict(sorted(daily.items()))

    def account_totals(self, since: date, until: date) -> Dict[str, Any]:
        """Totals over the period for metrics that only support metric_type=total_value."""
        totals: Dict[str, Any] = {}
        for start, end in chunk_range(since, until):
            chunk = self._totals_chunk(self.config.account_total_metrics, start, end)
            for name, value in chunk.items():
                if isinstance(value, (int, float)) and isinstance(totals.get(name), (int, float)):
                    totals[name] += value
                else:
                    totals.setdefault(name, value)
        return totals

    def _totals_chunk(self, metrics: List[str], start: date, end: date) -> Dict[str, Any]:
        params = dict(period="day", metric_type="total_value", since=_unix(start), until=_unix(end, True))
        try:
            body = self.client.get(f"{self.user_path}/insights", metric=",".join(metrics), **params)
            return {e["name"]: _metric_value(e) for e in body.get("data", [])}
        except GraphAPIError as exc:
            if len(metrics) == 1:
                self._warn(f"account total {metrics[0]!r} skipped: {exc.message}")
                return {}
            # One bad metric name fails the whole call; retry one at a time.
            result: Dict[str, Any] = {}
            for metric in metrics:
                result.update(self._totals_chunk([metric], start, end))
            return result

    # ----------------------------------------------------------------- media
    def recent_media(self, since: date, limit: int = 25) -> List[Dict[str, Any]]:
        since_dt = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
        items = []
        for item in self.client.paginate(f"{self.user_path}/media", fields=MEDIA_FIELDS, limit=limit):
            ts = item.get("timestamp")
            if ts and parse_timestamp(ts) < since_dt:
                break
            items.append(item)
        return items

    def metrics_for(self, media: Dict[str, Any]) -> List[str]:
        product = (media.get("media_product_type") or "FEED").upper()
        if product == "REELS":
            return self.config.media_metrics_reels
        if product == "STORY":
            return self.config.media_metrics_story
        return self.config.media_metrics_feed

    def media_insights(self, media: Dict[str, Any]) -> Dict[str, Any]:
        metrics = self.metrics_for(media)
        return self._media_insights(media["id"], metrics)

    def _media_insights(self, media_id: str, metrics: List[str]) -> Dict[str, Any]:
        if not metrics:
            return {}
        try:
            body = self.client.get(f"{media_id}/insights", metric=",".join(metrics))
            return {e["name"]: _metric_value(e) for e in body.get("data", [])}
        except GraphAPIError as exc:
            if len(metrics) == 1:
                self._warn(f"media {media_id} metric {metrics[0]!r} skipped: {exc.message}")
                return {}
            result: Dict[str, Any] = {}
            for metric in metrics:
                result.update(self._media_insights(media_id, [metric]))
            return result

    # ---------------------------------------------------------------- bundle
    def build(self, since: date, until: date, media_limit: int = 25) -> Dict[str, Any]:
        self.warnings = []
        account = self.account_summary()
        daily = self.account_series(since, until)
        totals = self.account_totals(since, until)
        media = self.recent_media(since, media_limit)
        for item in media:
            item["insights"] = self.media_insights(item)
        return {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "since": since.isoformat(),
            "until": until.isoformat(),
            "account": account,
            "totals": totals,
            "daily": daily,
            "media": media,
            "warnings": list(self.warnings),
        }

    def _warn(self, message: str) -> None:
        log.warning(message)
        self.warnings.append(message)


# ------------------------------------------------------------------ rendering
def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.1f}"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _table(headers: List[str], rows: Iterable[List[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(" --- " for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(_fmt(c).replace("|", "\\|") for c in row) + " |")
    return "\n".join(lines)


def _engagement(item: Dict[str, Any]) -> int:
    ins = item.get("insights") or {}
    total = ins.get("total_interactions")
    if isinstance(total, int):
        return total
    return int(item.get("like_count") or 0) + int(item.get("comments_count") or 0)


def render_markdown(data: Dict[str, Any]) -> str:
    acct = data.get("account", {})
    out = [f"# Instagram report for @{acct.get('username', '?')}", ""]
    out.append(f"Period: {data['since']} to {data['until']}. Generated {data['generated_at']}.")
    out.append("")
    out.append("## Account")
    out.append(
        _table(
            ["Followers", "Following", "Posts"],
            [[acct.get("followers_count"), acct.get("follows_count"), acct.get("media_count")]],
        )
    )
    out.append("")

    totals = data.get("totals") or {}
    if totals:
        out.append("## Totals for the period")
        out.append(_table(["Metric", "Value"], [[k, v] for k, v in totals.items()]))
        out.append("")

    daily = data.get("daily") or {}
    if daily:
        metrics = sorted({m for day in daily.values() for m in day})
        out.append("## Daily")
        out.append(_table(["Date", *metrics], [[day, *[vals.get(m) for m in metrics]] for day, vals in daily.items()]))
        out.append("")

    media = data.get("media") or []
    if media:
        ranked = sorted(media, key=_engagement, reverse=True)
        out.append("## Posts in the period")
        out.append(f"{len(media)} posts. Ranked by total interactions (or likes + comments when unavailable).")
        out.append("")
        rows = []
        for item in ranked:
            ins = item.get("insights") or {}
            caption = (item.get("caption") or "").replace("\n", " ")
            if len(caption) > 60:
                caption = caption[:57] + "..."
            rows.append(
                [
                    str(item.get("timestamp", ""))[:10],
                    item.get("media_product_type") or item.get("media_type"),
                    caption,
                    item.get("like_count"),
                    item.get("comments_count"),
                    ins.get("views"),
                    ins.get("reach"),
                    ins.get("saved"),
                    ins.get("shares"),
                    _engagement(item),
                    item.get("permalink"),
                ]
            )
        out.append(
            _table(
                ["Date", "Type", "Caption", "Likes", "Comments", "Views", "Reach", "Saves", "Shares", "Interactions", "Link"],
                rows,
            )
        )
        out.append("")

    warnings = data.get("warnings") or []
    if warnings:
        out.append("## Notes")
        out.append("Some metrics were not available for this account or API version:")
        out.extend(f"- {w}" for w in warnings)
        out.append("")
    return "\n".join(out)


def write_report(data: Dict[str, Any], out_dir: str, basename: Optional[str] = None) -> Dict[str, str]:
    """Write markdown, JSON and CSV files. Returns the paths written."""
    os.makedirs(out_dir, exist_ok=True)
    basename = basename or f"report_{data['since']}_{data['until']}"
    paths = {}

    md_path = os.path.join(out_dir, basename + ".md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(data))
    paths["markdown"] = md_path

    json_path = os.path.join(out_dir, basename + ".json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    paths["json"] = json_path

    daily = data.get("daily") or {}
    if daily:
        metrics = sorted({m for day in daily.values() for m in day})
        daily_path = os.path.join(out_dir, basename + "_daily.csv")
        with open(daily_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["date", *metrics])
            for day, vals in daily.items():
                writer.writerow([day, *[vals.get(m) for m in metrics]])
        paths["daily_csv"] = daily_path

    media = data.get("media") or []
    if media:
        metric_names = sorted({m for item in media for m in (item.get("insights") or {})})
        media_path = os.path.join(out_dir, basename + "_media.csv")
        with open(media_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["id", "timestamp", "type", "product_type", "permalink", "like_count", "comments_count", "caption", *metric_names])
            for item in media:
                ins = item.get("insights") or {}
                writer.writerow(
                    [
                        item.get("id"),
                        item.get("timestamp"),
                        item.get("media_type"),
                        item.get("media_product_type"),
                        item.get("permalink"),
                        item.get("like_count"),
                        item.get("comments_count"),
                        item.get("caption"),
                        *[_fmt(ins.get(m)) if isinstance(ins.get(m), (dict, list)) else ins.get(m) for m in metric_names],
                    ]
                )
        paths["media_csv"] = media_path
    return paths
