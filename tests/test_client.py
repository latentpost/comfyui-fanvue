import io
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from support import clean_env, client
from fake_broker import API_KEY, FakeBroker

LatentPostError = client.LatentPostError


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBroker(part_size=4)
        self.addCleanup(self.fake.close)
        self.waits = []
        self.broker = client.Broker(API_KEY, self.fake.url, key_source="the key file", wait=self.waits.append)

    def upload(self, data=b"hello world", progress=None):
        return self.broker.upload("out.png", "image", io.BytesIO(data), len(data), on_progress=progress)

    def slept(self):
        return [w for w in self.waits if w]  # wait(0) is the cancel check between parts

    def failure(self, *args, **kwargs):
        with self.assertRaises(LatentPostError) as caught:
            self.upload(*args, **kwargs)
        return caught.exception


class UploadSequenceTest(BrokerTest):
    def test_sends_the_bytes_to_storage_and_completes_with_the_etags(self):
        progress = []
        self.assertEqual(self.upload(b"hello world", progress.append), "media-1")
        upload = self.fake.uploads["upload/1"]
        self.assertEqual(self.fake.stored("upload/1"), b"hello world")
        self.assertEqual(upload["completed"], [{"partNumber": n, "etag": f'"etag-{n}"'} for n in (1, 2, 3)])
        self.assertEqual((upload["filename"], upload["mediaType"], upload["size"]), ("out.png", "image", 11))
        self.assertEqual(progress, [4 / 11, 8 / 11, 1.0])

    def test_media_goes_only_to_storage_and_the_key_only_to_the_broker(self):
        self.upload()
        self.assertEqual([m for m, _ in self.fake.calls], ["POST", "POST", "GET", "GET"])  # no PUTs to the broker
        self.assertTrue(self.fake.storage_headers)
        for headers in self.fake.storage_headers:
            self.assertNotIn("Authorization", headers)
            self.assertEqual(headers["Content-Type"], "application/octet-stream")

    def test_url_encodes_upload_ids(self):
        self.upload()
        self.assertEqual(self.fake.calls_to("POST", "complete"), ["/api/v1/uploads/upload%2F1/complete"])

    def test_fetches_more_part_urls_past_the_first_100(self):
        data = os.urandom(4 * 250 - 1)  # 250 parts
        self.upload(data)
        self.assertEqual(self.fake.stored("upload/1"), data)
        self.assertEqual(self.fake.calls_to("GET", "/parts"), [
            "/api/v1/uploads/upload%2F1/parts?from=101&to=200",
            "/api/v1/uploads/upload%2F1/parts?from=201&to=250",
        ])

    def test_polls_media_with_backoff_until_ready(self):
        self.fake.polls_until_ready = 7
        self.upload()
        self.assertEqual(self.slept(), [1, 2, 4, 8, 16, 30, 30])

    def test_gives_up_on_processing_after_about_ten_minutes(self):
        self.fake.polls_until_ready = 10 ** 6
        err = self.failure()
        self.assertEqual(err.code, "media_timeout")
        self.assertIn("still processing out.png after 10 minutes", str(err))
        self.assertTrue(600 <= sum(self.slept()) < 630)

    def test_media_error_fails(self):
        self.fake.final_status = "error"
        err = self.failure()
        self.assertEqual(err.code, "media_error")
        self.assertIn("Fanvue couldn't process out.png", str(err))

    def test_retries_storage_server_errors(self):
        self.fake.storage_errors = [500, 503]
        self.upload()
        self.assertEqual(self.slept()[:2], [2, 4])
        self.assertEqual(self.fake.stored("upload/1"), b"hello world")

    def test_storage_refusal_stops(self):
        self.fake.storage_errors = [403]
        err = self.failure()
        self.assertEqual(str(err), "Fanvue's storage didn't accept part 1 of out.png (HTTP 403). Try again.")
        self.assertEqual(self.fake.calls_to("POST", "complete"), [])

    def test_files_media_in_the_vault_100_at_a_time(self):
        uuids = [f"m{n}" for n in range(250)]
        self.assertEqual(self.broker.add_to_vault("Drafts", uuids), 250)
        self.assertEqual([(folder, len(batch)) for folder, batch in self.fake.vault],
                         [("Drafts", 100), ("Drafts", 100), ("Drafts", 50)])


class ErrorTableTest(BrokerTest):
    """docs/broker-design.md, "Error codes, and what the node should do"."""

    def test_rate_limited_waits_retry_after_then_retries(self):
        self.fake.fail("POST", "/uploads$", 429, "rate_limited", "Fanvue's rate limit was reached. Retry in 7 s.",
                       {"Retry-After": "7"})
        self.assertEqual(self.upload(), "media-1")
        self.assertEqual(self.slept()[0], 7)

    def test_rate_limited_gives_up_after_five_waits(self):
        message = "Fanvue's rate limit was reached. Retry in 1 s."
        self.fake.fail("POST", "/uploads$", 429, "rate_limited", message, {"Retry-After": "1"}, times=99)
        self.assertEqual(str(self.failure()), message)
        self.assertEqual(len(self.fake.calls), 6)

    def test_busy_waits_retry_after_then_retries(self):
        self.fake.fail("GET", "/media/", 503, "busy", "Still refreshing your Fanvue sign-in.", {"Retry-After": "5"})
        self.upload()
        self.assertEqual(self.slept()[0], 5)

    def test_fanvue_unavailable_backs_off_for_three_tries(self):
        message = "Fanvue returned an error (500). Try again shortly."
        self.fake.fail("POST", "/complete$", 502, "fanvue_unavailable", message, times=2)
        self.upload()
        self.assertEqual(self.slept()[:2], [2, 4])
        self.fake.fail("POST", "/complete$", 502, "fanvue_unavailable", message, times=3)
        self.assertEqual(str(self.failure()), message)
        self.assertEqual(len(self.fake.calls_to("POST", "upload%2F2/complete")), 3)

    def test_internal_error_retries_once_then_shows(self):
        message = "Something went wrong on our side. Try again."
        self.fake.fail("POST", "/uploads$", 500, "internal_error", message, times=1)
        self.upload()
        self.fake.fail("POST", "/uploads$", 500, "internal_error", message, times=2)
        self.assertEqual(str(self.failure()), message)
        self.assertEqual(len(self.fake.calls_to("POST", "/uploads$")), 2 + 2)

    def test_stop_codes_show_the_message_as_written_without_retrying(self):
        cases = [
            (429, "daily_post_limit", "You've created 50 posts today, the daily maximum. Try again tomorrow (UTC)."),
            (402, "quota_exceeded", "You've used all 30 free uploads this month. Upgrade on the dashboard: x"),
            (402, "plan_required", "Scheduled and paid posts need Pro. Upgrade on the dashboard: x"),
            (400, "fanvue_rejected", "Fanvue rejected the request: text contains prohibited language"),
            (400, "bad_request", "audience must be one of: subscribers, followers-and-subscribers"),
            (404, "not_found", "Not found"),
        ]
        for status, code, message in cases:
            with self.subTest(code):
                self.fake.calls.clear()
                self.fake.fail("POST", "/posts$", status, code, message, {"Retry-After": "60"})
                with self.assertRaises(LatentPostError) as caught:
                    self.broker.create_post({"audience": "subscribers", "text": "hi"})
                self.assertEqual((str(caught.exception), caught.exception.code), (message, code))
                self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.slept(), [])

    def post(self):
        return self.broker.create_post({"audience": "subscribers", "text": "hi"})

    def test_posts_wait_out_rate_limits_and_busy(self):
        self.fake.fail("POST", "/posts$", 429, "rate_limited", "Fanvue's rate limit was reached.", {"Retry-After": "3"})
        self.fake.fail("POST", "/posts$", 503, "busy", "Still refreshing your Fanvue sign-in.", {"Retry-After": "5"})
        self.assertEqual(self.post()["uuid"], "post-1")
        self.assertEqual(self.slept(), [3, 5])

    def test_posts_are_never_retried_after_errors_that_could_follow_a_created_post(self):
        note = "\nThe post may have been created anyway. Check your Fanvue posts before you try again."
        cases = [
            (502, "fanvue_unavailable", "Fanvue returned an error (500). Try again shortly.", None),
            (500, "internal_error", "Something went wrong on our side. Try again.", None),
            (502, None, "LatentPost sent an unexpected response (HTTP 502). Try again shortly. "
                        "If it keeps happening, email support@latentpost.com.", b"<html>Bad gateway</html>"),
        ]
        for status, code, message, raw in cases:
            with self.subTest(code or "html"):
                self.fake.calls.clear()
                self.fake.fail("POST", "/posts$", status, code, message, raw=raw)
                with self.assertRaises(LatentPostError) as caught:
                    self.post()
                self.assertEqual(str(caught.exception), message + note)
                self.assertEqual(len(self.fake.calls), 1)
        with mock.patch.object(client, "_http", side_effect=TimeoutError("timed out")) as send:
            with self.assertRaises(LatentPostError) as caught:
                self.post()
        self.assertEqual(send.call_count, 1)
        self.assertTrue(str(caught.exception).endswith(note))
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(self.slept(), [])

    def test_invalid_api_key_says_where_the_key_comes_from(self):
        self.broker.api_key = "fvc_wrong"
        self.assertEqual(str(self.failure()), f"API key not recognised. Copy a new one from {self.fake.url}/dashboard"
                                              "\nLatentPost reads your key from the key file.")
        self.assertEqual(len(self.fake.calls), 1)

    def test_reconnect_required_shows_the_dashboard_link(self):
        message = "Your Fanvue sign-in expired or was revoked. Reconnect your account on the dashboard."
        self.fake.fail("POST", "/uploads$", 401, "reconnect_required", message)
        self.assertEqual(str(self.failure()), f"{message}\n{self.fake.url}/dashboard")
        self.assertEqual(len(self.fake.calls), 1)

    def test_unreachable_broker_backs_off_for_three_tries(self):
        refused = urllib.error.URLError(ConnectionRefusedError(10061, "No connection could be made"))
        with mock.patch.object(client, "_http", side_effect=refused) as send:
            err = self.failure()
        self.assertEqual((err.code, send.call_count), ("unreachable", 3))
        self.assertEqual(str(err), f"Couldn't reach LatentPost at {self.fake.url} ([Errno 10061] No connection "
                                   "could be made). Check your internet connection and try again.")
        self.assertEqual(self.slept(), [2, 4])

    def test_responses_that_arent_broker_json_are_explained(self):
        self.fake.fail("POST", "/uploads$", 403, raw=b"<html>Attention Required</html>")
        err = self.failure()
        self.assertEqual(str(err), "LatentPost sent an unexpected response (HTTP 403). Try again shortly. "
                                   "If it keeps happening, email support@latentpost.com.")
        self.assertEqual(len(self.fake.calls), 1)
        self.fake.calls.clear()
        self.fake.fail("POST", "/uploads$", 502, raw=b"<html>Bad gateway</html>", times=3)
        self.failure()
        self.assertEqual(len(self.fake.calls), 3)


class LoadBrokerTest(unittest.TestCase):
    def setUp(self):
        self.user_dir = tempfile.mkdtemp()
        self.key_file = os.path.join(self.user_dir, "latentpost_api_key.txt")

    def write_key(self, text, path=None):
        with open(path or self.key_file, "w", encoding="utf-8-sig") as f:
            f.write(text)

    def error(self):
        with self.assertRaises(LatentPostError) as caught:
            client.load_broker(self.user_dir)
        return str(caught.exception)

    def test_reads_the_key_file(self):
        clean_env(self)
        self.write_key(f"  {API_KEY}\r\n")  # with a BOM, as Notepad may save it
        broker = client.load_broker(self.user_dir)
        self.assertEqual((broker.api_key, broker.base_url, broker.key_source),
                         (API_KEY, "https://latentpost.com", self.key_file))

    def test_finds_a_key_file_saved_as_txt_txt(self):
        clean_env(self)
        self.write_key(API_KEY, self.key_file + ".txt")
        self.assertEqual(client.load_broker(self.user_dir).api_key, API_KEY)

    def test_environment_wins(self):
        clean_env(self, LATENTPOST_API_KEY="fvc_from_env", LATENTPOST_URL="http://localhost:8787/")
        self.write_key(API_KEY)
        broker = client.load_broker(self.user_dir)
        self.assertEqual((broker.api_key, broker.base_url), ("fvc_from_env", "http://localhost:8787"))
        self.assertEqual(broker.key_source, "the LATENTPOST_API_KEY environment variable")

    def test_missing_key_says_where_to_put_it(self):
        clean_env(self)
        self.assertEqual(self.error(), "Add your LatentPost API key first. Copy it from "
                                       f"https://latentpost.com/dashboard and save it as a text file here:\n{self.key_file}")

    def test_rejects_something_that_isnt_a_key(self):
        clean_env(self)
        self.write_key("Copy your key from the dashboard")
        self.assertIn("doesn't look right: keys start with fvc_", self.error())

    def test_refuses_plain_http_except_on_localhost(self):
        clean_env(self, LATENTPOST_API_KEY=API_KEY, LATENTPOST_URL="http://latentpost.com")
        self.assertEqual(self.error(), "LATENTPOST_URL must start with https:// (http:// only works for localhost).")


if __name__ == "__main__":
    unittest.main()
