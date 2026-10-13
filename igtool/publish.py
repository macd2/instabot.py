"""Content publishing through the Graph API (container -> publish)."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from .client import GraphAPIError, GraphClient

log = logging.getLogger("igtool.publish")

KINDS = ("image", "reel", "story", "carousel")
VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v")
TERMINAL_OK = {"FINISHED", "PUBLISHED"}
TERMINAL_BAD = {"ERROR", "EXPIRED"}


def is_video_url(url: str) -> bool:
    path = url.split("?", 1)[0].lower()
    return path.endswith(VIDEO_EXTENSIONS)


class Publisher:
    def __init__(
        self,
        client: GraphClient,
        user_path: str,
        poll_interval: float = 10.0,
        poll_timeout: float = 900.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client
        self.user_path = user_path
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self._sleep = sleep

    # ------------------------------------------------------------ containers
    def create_container(
        self, kind: str, media_urls: List[str], caption: Optional[str] = None, options: Optional[Dict[str, Any]] = None
    ) -> str:
        """Create a media container and return its id (not yet published)."""
        options = dict(options or {})
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}, expected one of {KINDS}")
        if not media_urls:
            raise ValueError("at least one media URL is required")

        if kind == "image":
            if len(media_urls) != 1:
                raise ValueError("an image post takes exactly one URL")
            params = {"image_url": media_urls[0], "caption": caption}
            for key in ("alt_text", "location_id", "user_tags", "collaborators"):
                if key in options:
                    params[key] = options[key]
        elif kind == "reel":
            if len(media_urls) != 1:
                raise ValueError("a reel takes exactly one video URL")
            params = {"media_type": "REELS", "video_url": media_urls[0], "caption": caption}
            for key in ("cover_url", "thumb_offset", "share_to_feed", "location_id", "collaborators", "audio_name"):
                if key in options:
                    params[key] = options[key]
        elif kind == "story":
            if len(media_urls) != 1:
                raise ValueError("a story takes exactly one URL")
            params = {"media_type": "STORIES"}
            params["video_url" if is_video_url(media_urls[0]) else "image_url"] = media_urls[0]
        else:  # carousel
            if not 2 <= len(media_urls) <= 10:
                raise ValueError("a carousel needs between 2 and 10 URLs")
            children = [self._create_carousel_item(url) for url in media_urls]
            params = {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption}
            for key in ("location_id", "collaborators"):
                if key in options:
                    params[key] = options[key]

        body = self.client.post(f"{self.user_path}/media", **params)
        container_id = body.get("id")
        if not container_id:
            raise GraphAPIError(f"no container id in response: {body}")
        log.info("created %s container %s", kind, container_id)
        return container_id

    def _create_carousel_item(self, url: str) -> str:
        params: Dict[str, Any] = {"is_carousel_item": "true"}
        if is_video_url(url):
            params.update(media_type="VIDEO", video_url=url)
        else:
            params["image_url"] = url
        body = self.client.post(f"{self.user_path}/media", **params)
        item_id = body.get("id")
        if not item_id:
            raise GraphAPIError(f"no carousel item id in response: {body}")
        return item_id

    # ---------------------------------------------------------------- status
    def container_status(self, container_id: str) -> Dict[str, Any]:
        return self.client.get(container_id, fields="status_code,status")

    def wait_until_ready(self, container_id: str) -> Dict[str, Any]:
        """Poll until the container is FINISHED (videos take a while)."""
        waited = 0.0
        while True:
            status = self.container_status(container_id)
            code = status.get("status_code")
            if code in TERMINAL_OK:
                return status
            if code in TERMINAL_BAD:
                raise GraphAPIError(f"container {container_id} ended with {code}: {status.get('status')}")
            if waited >= self.poll_timeout:
                raise GraphAPIError(f"container {container_id} still {code} after {int(waited)}s")
            self._sleep(self.poll_interval)
            waited += self.poll_interval

    # --------------------------------------------------------------- publish
    def publish_container(self, container_id: str) -> str:
        body = self.client.post(f"{self.user_path}/media_publish", creation_id=container_id)
        media_id = body.get("id")
        if not media_id:
            raise GraphAPIError(f"no media id in publish response: {body}")
        log.info("published container %s as media %s", container_id, media_id)
        return media_id

    def publish_post(
        self, kind: str, media_urls: List[str], caption: Optional[str] = None, options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, str]:
        """Create, wait for and publish a post in one go."""
        container_id = self.create_container(kind, media_urls, caption, options)
        self.wait_until_ready(container_id)
        media_id = self.publish_container(container_id)
        return {"container_id": container_id, "media_id": media_id}

    def publishing_limit(self) -> Dict[str, Any]:
        """Current usage of the rolling 24h publishing quota."""
        body = self.client.get(f"{self.user_path}/content_publishing_limit", fields="quota_usage,config")
        data = body.get("data") or [{}]
        entry = data[0]
        config = entry.get("config", {})
        return {
            "quota_usage": entry.get("quota_usage"),
            "quota_total": config.get("quota_total"),
            "quota_duration_seconds": config.get("quota_duration"),
        }
