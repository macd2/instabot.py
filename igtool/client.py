"""Thin client for the Instagram Graph API with retries and paging."""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Dict, Iterator, Optional

import requests

log = logging.getLogger("igtool.client")

# Graph API error codes that mean "slow down" rather than "you did it wrong".
RATE_LIMIT_CODES = {4, 17, 32, 613}
RETRYABLE_HTTP = {429, 500, 502, 503, 504}


def redact(text: str) -> str:
    """Hide access tokens that libraries echo back inside URLs."""
    return re.sub(r"access_token=[^&\s'\"]+", "access_token=***", str(text))


class GraphAPIError(Exception):
    def __init__(
        self,
        message: str,
        code: Optional[int] = None,
        subcode: Optional[int] = None,
        http_status: Optional[int] = None,
        fbtrace_id: Optional[str] = None,
        error_type: Optional[str] = None,
        user_message: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.subcode = subcode
        self.http_status = http_status
        self.fbtrace_id = fbtrace_id
        self.error_type = error_type
        self.user_message = user_message

    def __str__(self) -> str:  # pragma: no cover - formatting only
        parts = [self.message]
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.subcode is not None:
            parts.append(f"subcode={self.subcode}")
        if self.http_status is not None:
            parts.append(f"http={self.http_status}")
        if self.user_message:
            parts.append(f"detail={self.user_message}")
        return " ".join(parts)

    @property
    def is_rate_limit(self) -> bool:
        return self.code in RATE_LIMIT_CODES or self.http_status == 429

    @classmethod
    def from_response(cls, status: int, body: Any) -> "GraphAPIError":
        err = body.get("error", {}) if isinstance(body, dict) else {}
        return cls(
            message=err.get("message") or f"HTTP {status}",
            code=err.get("code"),
            subcode=err.get("error_subcode"),
            http_status=status,
            fbtrace_id=err.get("fbtrace_id"),
            error_type=err.get("type"),
            user_message=err.get("error_user_msg"),
        )


class GraphClient:
    """Minimal Graph API client.

    ``session`` only needs a ``request(method, url, params=, data=, timeout=)``
    method returning an object with ``status_code`` and ``json()``. Tests pass
    a fake; production uses ``requests.Session``.
    """

    def __init__(
        self,
        access_token: str,
        host: str = "graph.facebook.com",
        version: str = "v26.0",
        session: Optional[Any] = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.access_token = access_token
        self.host = host
        self.version = version
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep

    @property
    def base_url(self) -> str:
        return f"https://{self.host}/{self.version}"

    def url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    # ------------------------------------------------------------------ core
    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        data = {k: v for k, v in (data or {}).items() if v is not None}
        url = self.url(path)
        # Paging URLs returned by the API already carry the token.
        if "access_token" not in url:
            if method.upper() == "GET":
                params.setdefault("access_token", self.access_token)
            else:
                data.setdefault("access_token", self.access_token)

        attempt = 0
        while True:
            try:
                resp = self.session.request(
                    method.upper(), url, params=params or None, data=data or None, timeout=self.timeout
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= self.max_retries:
                    raise GraphAPIError(f"network error: {redact(exc)}") from exc
                self._backoff(attempt, f"network error: {redact(exc)}")
                attempt += 1
                continue

            try:
                body = resp.json()
            except ValueError:
                body = {}

            if resp.status_code < 400 and "error" not in body:
                return body

            err = GraphAPIError.from_response(resp.status_code, body)
            retry = err.is_rate_limit or resp.status_code in RETRYABLE_HTTP
            if retry and attempt < self.max_retries:
                self._backoff(attempt, str(err))
                attempt += 1
                continue
            raise err

    def _backoff(self, attempt: int, reason: str) -> None:
        delay = self.backoff_base * (2**attempt)
        log.warning("retrying in %.0fs after: %s", delay, reason)
        self._sleep(delay)

    def get(self, path: str, **params: Any) -> Dict[str, Any]:
        return self.request("GET", path, params=params)

    def post(self, path: str, **data: Any) -> Dict[str, Any]:
        return self.request("POST", path, data=data)

    def paginate(self, path: str, limit: Optional[int] = None, **params: Any) -> Iterator[Dict[str, Any]]:
        """Yield items from a paged edge, following ``paging.next``."""
        count = 0
        body = self.get(path, **params)
        while True:
            for item in body.get("data", []):
                yield item
                count += 1
                if limit is not None and count >= limit:
                    return
            next_url = body.get("paging", {}).get("next")
            if not next_url:
                return
            body = self.request("GET", next_url)

    # --------------------------------------------------------------- helpers
    def whoami(self, uses_instagram_login: bool) -> Dict[str, Any]:
        """Describe the account(s) this token can act for."""
        if uses_instagram_login:
            me = self.get("me", fields="id,user_id,username,account_type,followers_count,media_count")
            return {"mode": "instagram_login", "accounts": [me]}
        accounts = []
        for page in self.paginate(
            "me/accounts",
            fields="id,name,instagram_business_account{id,username,followers_count,media_count}",
        ):
            ig = page.get("instagram_business_account")
            if ig:
                accounts.append({"page_id": page["id"], "page_name": page.get("name"), **ig})
        return {"mode": "facebook_login", "accounts": accounts}

    def refresh_long_lived_token(self, uses_instagram_login: bool, app_id: str = "", app_secret: str = "") -> Dict[str, Any]:
        """Return a fresh long-lived token.

        Instagram Login tokens refresh themselves; Facebook Login tokens need the
        app id and secret to be exchanged.
        """
        if uses_instagram_login:
            return self.request(
                "GET",
                f"https://{self.host}/refresh_access_token",
                params={"grant_type": "ig_refresh_token", "access_token": self.access_token},
            )
        if not (app_id and app_secret):
            raise ValueError("app_id and app_secret are required to exchange a Facebook Login token")
        return self.get(
            "oauth/access_token",
            grant_type="fb_exchange_token",
            client_id=app_id,
            client_secret=app_secret,
            fb_exchange_token=self.access_token,
        )
