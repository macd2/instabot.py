"""Configuration for igtool.

Values come from an INI file (default ``igtool.ini``, section ``[igtool]``)
and can be overridden by environment variables prefixed with ``IGTOOL_``.
The INI file and the SQLite database are both covered by .gitignore so that
tokens never end up in a commit.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from typing import List, Optional

DEFAULT_CONFIG_FILE = "igtool.ini"
DEFAULT_API_VERSION = "v26.0"
HOST_FACEBOOK_LOGIN = "graph.facebook.com"
HOST_INSTAGRAM_LOGIN = "graph.instagram.com"

ENV_PREFIX = "IGTOOL_"


def _split_list(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Config:
    access_token: str = ""
    ig_user_id: str = ""
    api_host: str = HOST_FACEBOOK_LOGIN
    api_version: str = DEFAULT_API_VERSION
    database: str = "igtool.db"
    report_dir: str = "reports"
    timezone: str = "UTC"
    # Metric names change over time (impressions became views in 2025), so
    # they are configurable rather than hard-coded in the report module.
    account_series_metrics: List[str] = field(default_factory=lambda: ["reach", "follower_count"])
    account_total_metrics: List[str] = field(
        default_factory=lambda: [
            "views",
            "reach",
            "accounts_engaged",
            "total_interactions",
            "likes",
            "comments",
            "shares",
            "saves",
            "replies",
            "profile_views",
            "website_clicks",
            "follows_and_unfollows",
        ]
    )
    media_metrics_feed: List[str] = field(
        default_factory=lambda: [
            "views",
            "reach",
            "likes",
            "comments",
            "shares",
            "saved",
            "total_interactions",
            "follows",
            "profile_visits",
        ]
    )
    media_metrics_reels: List[str] = field(
        default_factory=lambda: [
            "views",
            "reach",
            "likes",
            "comments",
            "shares",
            "saved",
            "total_interactions",
            "ig_reels_avg_watch_time",
            "ig_reels_video_view_total_time",
        ]
    )
    media_metrics_story: List[str] = field(
        default_factory=lambda: [
            "views",
            "reach",
            "replies",
            "shares",
            "total_interactions",
            "follows",
            "profile_visits",
        ]
    )

    @property
    def uses_instagram_login(self) -> bool:
        return self.api_host == HOST_INSTAGRAM_LOGIN

    @property
    def user_path(self) -> str:
        """Path segment used for the Instagram professional account.

        With the Instagram Login flavour of the API ``/me`` always resolves to
        the account the token belongs to, so the numeric id is optional there.
        """
        if self.ig_user_id:
            return self.ig_user_id
        if self.uses_instagram_login:
            return "me"
        raise ValueError(
            "ig_user_id is not set. Run 'python -m igtool whoami' to find it, "
            "then put it in igtool.ini or IGTOOL_USER_ID."
        )

    def validate(self) -> None:
        if not self.access_token:
            raise ValueError("access_token is not set (igtool.ini or IGTOOL_ACCESS_TOKEN).")
        if self.api_host not in (HOST_FACEBOOK_LOGIN, HOST_INSTAGRAM_LOGIN):
            raise ValueError(
                f"api_host must be {HOST_FACEBOOK_LOGIN} or {HOST_INSTAGRAM_LOGIN}, got {self.api_host!r}"
            )
        if not self.api_version.startswith("v"):
            raise ValueError("api_version must look like v26.0")


_LIST_FIELDS = {
    "account_series_metrics",
    "account_total_metrics",
    "media_metrics_feed",
    "media_metrics_reels",
    "media_metrics_story",
}


def load_config(path: Optional[str] = None, environ: Optional[dict] = None) -> Config:
    """Build a Config from the INI file (if present) plus environment overrides."""
    environ = os.environ if environ is None else environ
    cfg = Config()
    path = path or environ.get(ENV_PREFIX + "CONFIG", DEFAULT_CONFIG_FILE)

    parser = configparser.ConfigParser()
    if path and os.path.isfile(path):
        parser.read(path, encoding="utf-8")
        if parser.has_section("igtool"):
            for key, value in parser.items("igtool"):
                _apply(cfg, key, value)

    for key in cfg.__dataclass_fields__:
        env_value = environ.get(ENV_PREFIX + key.upper())
        if env_value is not None:
            _apply(cfg, key, env_value)
    # Friendlier aliases.
    if environ.get(ENV_PREFIX + "USER_ID"):
        cfg.ig_user_id = environ[ENV_PREFIX + "USER_ID"]
    return cfg


def _apply(cfg: Config, key: str, value: str) -> None:
    if key not in cfg.__dataclass_fields__:
        return
    if key in _LIST_FIELDS:
        setattr(cfg, key, _split_list(value))
    else:
        setattr(cfg, key, value.strip())
