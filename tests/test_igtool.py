import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from igtool.cli import build_parser, parse_when
from igtool.client import GraphAPIError, GraphClient
from igtool.config import Config, load_config
from igtool.publish import Publisher
from igtool.report import Reporter, chunk_range, render_markdown, write_report
from igtool.scheduler import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    ScheduledPost,
    Scheduler,
    ScheduleStore,
)


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Routes requests to handler(method, path, query, data) -> (status, body)."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, params=None, data=None, timeout=None):
        parsed = urlparse(url)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        query.update({k: str(v) for k, v in (params or {}).items()})
        self.calls.append((method, parsed.path, query, data or {}))
        status, body = self.handler(method, parsed.path, query, data or {})
        return FakeResponse(status, body)


def make_client(handler, **kw):
    session = FakeSession(handler)
    client = GraphClient("TOKEN", session=session, sleep=lambda s: None, **kw)
    return client, session


class ClientTests(unittest.TestCase):
    def test_get_adds_token_and_returns_body(self):
        client, session = make_client(lambda m, p, q, d: (200, {"id": "1"}))
        self.assertEqual(client.get("me", fields="id"), {"id": "1"})
        method, path, query, _ = session.calls[0]
        self.assertEqual((method, path), ("GET", "/v26.0/me"))
        self.assertEqual(query["access_token"], "TOKEN")
        self.assertEqual(query["fields"], "id")

    def test_post_puts_token_in_body(self):
        client, session = make_client(lambda m, p, q, d: (200, {"id": "c1"}))
        client.post("123/media", image_url="https://x/y.jpg")
        self.assertEqual(session.calls[0][3]["access_token"], "TOKEN")

    def test_graph_error_is_raised_with_details(self):
        body = {"error": {"message": "Unsupported get request.", "code": 100, "error_subcode": 33, "fbtrace_id": "abc"}}
        client, _ = make_client(lambda m, p, q, d: (400, body))
        with self.assertRaises(GraphAPIError) as ctx:
            client.get("nope")
        self.assertEqual(ctx.exception.code, 100)
        self.assertEqual(ctx.exception.subcode, 33)
        self.assertEqual(ctx.exception.http_status, 400)

    def test_rate_limit_is_retried_then_succeeds(self):
        attempts = []

        def handler(m, p, q, d):
            attempts.append(1)
            if len(attempts) < 3:
                return 400, {"error": {"message": "too many calls", "code": 4}}
            return 200, {"ok": True}

        client, _ = make_client(handler, max_retries=3)
        self.assertEqual(client.get("me"), {"ok": True})
        self.assertEqual(len(attempts), 3)

    def test_retries_exhausted_raises(self):
        client, _ = make_client(lambda m, p, q, d: (503, None), max_retries=1)
        with self.assertRaises(GraphAPIError) as ctx:
            client.get("me")
        self.assertEqual(ctx.exception.http_status, 503)

    def test_paginate_follows_next_and_respects_limit(self):
        def handler(m, p, q, d):
            if "after" in q:
                return 200, {"data": [{"id": "3"}, {"id": "4"}]}
            return 200, {
                "data": [{"id": "1"}, {"id": "2"}],
                "paging": {"next": "https://graph.facebook.com/v26.0/1/media?access_token=TOKEN&after=cur"},
            }

        client, session = make_client(handler)
        self.assertEqual([i["id"] for i in client.paginate("1/media")], ["1", "2", "3", "4"])
        self.assertEqual([i["id"] for i in client.paginate("1/media", limit=3)], ["1", "2", "3"])
        # paging URL already had a token; we must not add a second one
        self.assertEqual(session.calls[1][2]["access_token"], "TOKEN")


class ConfigTests(unittest.TestCase):
    def test_ini_then_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            ini = os.path.join(tmp, "igtool.ini")
            with open(ini, "w") as fh:
                fh.write("[igtool]\naccess_token = from_ini\nig_user_id = 42\napi_version = v25.0\n"
                         "account_series_metrics = reach , follower_count\n")
            cfg = load_config(ini, environ={"IGTOOL_ACCESS_TOKEN": "from_env"})
            self.assertEqual(cfg.access_token, "from_env")
            self.assertEqual(cfg.ig_user_id, "42")
            self.assertEqual(cfg.api_version, "v25.0")
            self.assertEqual(cfg.account_series_metrics, ["reach", "follower_count"])
            self.assertEqual(cfg.user_path, "42")

    def test_user_path_falls_back_to_me_for_instagram_login(self):
        cfg = Config(access_token="t", api_host="graph.instagram.com")
        self.assertEqual(cfg.user_path, "me")
        with self.assertRaises(ValueError):
            Config(access_token="t").user_path

    def test_validate(self):
        with self.assertRaises(ValueError):
            Config().validate()
        with self.assertRaises(ValueError):
            Config(access_token="t", api_host="example.com").validate()
        Config(access_token="t").validate()


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.statuses = ["IN_PROGRESS", "IN_PROGRESS", "FINISHED"]
        self.created = []

        def handler(method, path, query, data):
            if path.endswith("/42/media") and method == "POST":
                self.created.append(data)
                return 200, {"id": f"c{len(self.created)}"}
            if path.endswith("/42/media_publish"):
                return 200, {"id": "media_" + data["creation_id"]}
            if path.endswith("/42/content_publishing_limit"):
                return 200, {"data": [{"quota_usage": 3, "config": {"quota_total": 100, "quota_duration": 86400}}]}
            if query.get("fields") == "status_code,status":
                return 200, {"status_code": self.statuses.pop(0), "status": "x"}
            return 404, {"error": {"message": "unknown " + path, "code": 100}}

        self.client, self.session = make_client(handler)
        self.pub = Publisher(self.client, "42", poll_interval=1, poll_timeout=10, sleep=lambda s: None)

    def test_image_post_full_flow(self):
        result = self.pub.publish_post("image", ["https://cdn/a.jpg"], "hello", {"alt_text": "a cat"})
        self.assertEqual(result, {"container_id": "c1", "media_id": "media_c1"})
        self.assertEqual(self.created[0]["image_url"], "https://cdn/a.jpg")
        self.assertEqual(self.created[0]["caption"], "hello")
        self.assertEqual(self.created[0]["alt_text"], "a cat")
        self.assertEqual(self.statuses, [])  # polled until FINISHED

    def test_reel_params(self):
        self.pub.create_container("reel", ["https://cdn/v.mp4"], "cap", {"share_to_feed": "true"})
        self.assertEqual(self.created[0]["media_type"], "REELS")
        self.assertEqual(self.created[0]["video_url"], "https://cdn/v.mp4")
        self.assertEqual(self.created[0]["share_to_feed"], "true")

    def test_story_detects_video(self):
        self.pub.create_container("story", ["https://cdn/v.mov?x=1"])
        self.assertEqual(self.created[0]["media_type"], "STORIES")
        self.assertIn("video_url", self.created[0])
        self.assertNotIn("image_url", self.created[0])

    def test_carousel_creates_children_then_parent(self):
        self.pub.create_container("carousel", ["https://cdn/1.jpg", "https://cdn/2.mp4"], "c")
        self.assertEqual(len(self.created), 3)
        self.assertEqual(self.created[0]["is_carousel_item"], "true")
        self.assertEqual(self.created[1]["media_type"], "VIDEO")
        self.assertEqual(self.created[2]["media_type"], "CAROUSEL")
        self.assertEqual(self.created[2]["children"], "c1,c2")

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.pub.create_container("image", ["a", "b"])
        with self.assertRaises(ValueError):
            self.pub.create_container("carousel", ["a"])
        with self.assertRaises(ValueError):
            self.pub.create_container("gif", ["a"])

    def test_container_error_status_raises(self):
        self.statuses = ["ERROR"]
        with self.assertRaises(GraphAPIError):
            self.pub.wait_until_ready("c9")

    def test_poll_timeout(self):
        self.statuses = ["IN_PROGRESS"] * 50
        self.pub.poll_timeout = 3
        with self.assertRaises(GraphAPIError):
            self.pub.wait_until_ready("c9")

    def test_publishing_limit(self):
        self.assertEqual(
            self.pub.publishing_limit(), {"quota_usage": 3, "quota_total": 100, "quota_duration_seconds": 86400}
        )


class FakePublisher:
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.calls = []

    def publish_post(self, kind, urls, caption, options):
        self.calls.append((kind, urls, caption, options))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise GraphAPIError("boom", code=1)
        return {"container_id": "c", "media_id": f"m{len(self.calls)}"}


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ScheduleStore(os.path.join(self.tmp.name, "q.db"))
        self.now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, offset_minutes, kind="image"):
        return self.store.add(
            ScheduledPost(self.now + timedelta(minutes=offset_minutes), kind, ["https://cdn/x.jpg"], "cap", {"k": "v"})
        )

    def test_due_only_returns_past_pending(self):
        past = self.add(-5)
        future = self.add(5)
        due_ids = [p.id for p in self.store.due(self.now)]
        self.assertEqual(due_ids, [past])
        self.assertTrue(self.store.cancel(future))
        self.assertFalse(self.store.cancel(future))  # already cancelled
        self.assertEqual(self.store.get(future).status, "cancelled")

    def test_run_once_publishes_and_records(self):
        pid = self.add(-1)
        pub = FakePublisher()
        Scheduler(self.store, pub, now=lambda: self.now).run_once()
        post = self.store.get(pid)
        self.assertEqual(post.status, STATUS_PUBLISHED)
        self.assertEqual(post.media_id, "m1")
        self.assertEqual(post.attempts, 1)
        self.assertEqual(post.published_at, self.now)
        self.assertEqual(pub.calls[0], ("image", ["https://cdn/x.jpg"], "cap", {"k": "v"}))
        # second tick: nothing left
        self.assertEqual(Scheduler(self.store, pub, now=lambda: self.now).run_once(), [])

    def test_failure_retries_then_fails(self):
        pid = self.add(-1)
        pub = FakePublisher(fail_times=5)
        sched = Scheduler(self.store, pub, max_attempts=3, now=lambda: self.now)
        sched.run_once()
        self.assertEqual(self.store.get(pid).status, STATUS_PENDING)
        self.assertIn("boom", self.store.get(pid).last_error)
        sched.run_once()
        self.assertEqual(self.store.get(pid).status, STATUS_PENDING)
        sched.run_once()
        self.assertEqual(self.store.get(pid).status, STATUS_FAILED)
        self.assertEqual(self.store.get(pid).attempts, 3)
        self.assertEqual(sched.run_once(), [])

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValueError):
            self.store.add(ScheduledPost(datetime(2026, 1, 1), "image", ["u"]))


class ReportTests(unittest.TestCase):
    def test_chunk_range(self):
        chunks = chunk_range(date(2026, 1, 1), date(2026, 3, 1))
        self.assertEqual(chunks[0], (date(2026, 1, 1), date(2026, 1, 30)))
        self.assertEqual(chunks[-1][1], date(2026, 3, 1))
        self.assertTrue(all((b - a).days < 30 for a, b in chunks))
        self.assertEqual(chunk_range(date(2026, 1, 1), date(2026, 1, 1)), [(date(2026, 1, 1), date(2026, 1, 1))])

    def _handler(self, method, path, query, data):
        if path.endswith("/42") and "fields" in query:
            return 200, {"id": "42", "username": "acme", "followers_count": 1200, "follows_count": 10, "media_count": 3}
        if path.endswith("/42/insights"):
            metrics = query["metric"].split(",")
            if query.get("metric_type") == "total_value":
                if "bogus" in metrics and len(metrics) > 1:
                    return 400, {"error": {"message": "bogus is not a valid metric", "code": 100}}
                if metrics == ["bogus"]:
                    return 400, {"error": {"message": "bogus is not a valid metric", "code": 100}}
                return 200, {"data": [{"name": m, "period": "day", "total_value": {"value": 7}} for m in metrics]}
            return 200, {
                "data": [
                    {
                        "name": metrics[0],
                        "period": "day",
                        "values": [
                            {"value": 5, "end_time": "2026-10-01T07:00:00+0000"},
                            {"value": 6, "end_time": "2026-10-02T07:00:00+0000"},
                        ],
                    }
                ]
            }
        if path.endswith("/42/media"):
            return 200, {
                "data": [
                    {"id": "m1", "caption": "new|pipe", "media_type": "IMAGE", "media_product_type": "FEED",
                     "timestamp": "2026-10-02T10:00:00+0000", "permalink": "https://ig/p/1", "like_count": 3, "comments_count": 1},
                    {"id": "m2", "media_type": "VIDEO", "media_product_type": "REELS",
                     "timestamp": "2026-10-01T10:00:00+0000", "permalink": "https://ig/p/2", "like_count": 30, "comments_count": 2},
                    {"id": "old", "media_type": "IMAGE", "media_product_type": "FEED",
                     "timestamp": "2026-09-01T10:00:00+0000", "permalink": "https://ig/p/old", "like_count": 999},
                ]
            }
        if path.endswith("/m1/insights"):
            return 200, {"data": [{"name": "views", "values": [{"value": 100}]}, {"name": "total_interactions", "values": [{"value": 4}]}]}
        if path.endswith("/m2/insights"):
            return 400, {"error": {"message": "metric not supported", "code": 100}}
        return 404, {"error": {"message": "unknown " + path, "code": 100}}

    def test_build_and_render(self):
        client, session = make_client(self._handler)
        cfg = Config(access_token="t", ig_user_id="42", account_total_metrics=["views", "bogus"],
                     account_series_metrics=["reach"], media_metrics_reels=["views", "reach"])
        reporter = Reporter(client, cfg)
        data = reporter.build(date(2026, 10, 1), date(2026, 10, 2), media_limit=10)

        self.assertEqual(data["account"]["username"], "acme")
        self.assertEqual(data["totals"], {"views": 7})
        self.assertEqual(data["daily"], {"2026-10-01": {"reach": 5}, "2026-10-02": {"reach": 6}})
        self.assertEqual([m["id"] for m in data["media"]], ["m1", "m2"])  # "old" is before since
        self.assertEqual(data["media"][0]["insights"], {"views": 100, "total_interactions": 4})
        self.assertEqual(data["media"][1]["insights"], {})
        self.assertTrue(any("bogus" in w for w in data["warnings"]))
        self.assertTrue(any("m2" in w for w in data["warnings"]))

        md = render_markdown(data)
        self.assertIn("# Instagram report for @acme", md)
        self.assertIn("| views | 7 |", md)
        self.assertIn("new\\|pipe", md)
        self.assertIn("## Notes", md)
        # reel (32 interactions) outranks the image (4)
        self.assertLess(md.index("https://ig/p/2"), md.index("https://ig/p/1"))

        with tempfile.TemporaryDirectory() as tmp:
            paths = write_report(data, tmp)
            self.assertEqual(set(paths), {"markdown", "json", "daily_csv", "media_csv"})
            with open(paths["json"]) as fh:
                self.assertEqual(json.load(fh)["totals"], {"views": 7})
            with open(paths["daily_csv"]) as fh:
                self.assertEqual(fh.read().splitlines()[0], "date,reach")
            with open(paths["media_csv"]) as fh:
                header = fh.read().splitlines()[0]
            self.assertTrue(header.endswith("caption,total_interactions,views"))


class CliTests(unittest.TestCase):
    def test_parse_when_with_zone(self):
        dt = parse_when("2026-10-14 09:00", "Europe/Berlin")
        self.assertEqual(dt, datetime(2026, 10, 14, 7, 0, tzinfo=timezone.utc))
        iso = parse_when("2026-10-14T09:00:00+02:00", "UTC")
        self.assertEqual(iso, datetime(2026, 10, 14, 7, 0, tzinfo=timezone.utc))

    def test_parser_accepts_schedule_add(self):
        args = build_parser().parse_args(
            ["schedule", "add", "--at", "2026-10-14 09:00", "--kind", "carousel", "--media", "a", "--media", "b", "--option", "location_id=1"]
        )
        self.assertEqual(args.media, ["a", "b"])
        self.assertEqual(args.option, ["location_id=1"])


if __name__ == "__main__":
    unittest.main()
