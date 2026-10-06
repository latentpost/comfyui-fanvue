import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from support import FakeComfy, clean_env, client, nodes
from fake_broker import API_KEY, FakeBroker

LatentPostError = client.LatentPostError
HAVE_IMAGE_LIBS = all(importlib.util.find_spec(name) for name in ("numpy", "PIL"))


class NodeTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBroker(part_size=4)
        self.addCleanup(self.fake.close)
        self.user_dir, self.output_dir = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.comfy = FakeComfy(self.user_dir, self.output_dir)
        self.comfy.install(self)
        clean_env(self, LATENTPOST_URL=self.fake.url)
        with open(os.path.join(self.user_dir, client.KEY_FILENAME), "w") as f:
            f.write(API_KEY)
        patcher = mock.patch.object(nodes, "_wait", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def file(self, name, data=b"video bytes", folder=None):
        path = os.path.join(folder or self.output_dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def error(self, call, *args, **kwargs):
        with self.assertRaises(LatentPostError) as caught:
            call(*args, **kwargs)
        return str(caught.exception)


class SaveToVaultTest(NodeTest):
    save = nodes.SaveToVault().save

    def test_uploads_files_and_files_them_in_one_vault_call(self):
        elsewhere = self.file("clip.mp4", b"0123456789", folder=self.user_dir)
        self.file("still.png", b"png")
        paths = f'"{elsewhere}"\n\n  still.png  \n'  # quoted as Windows copies it; relative to output/
        self.assertEqual(self.save("Drafts", "LP", file_paths=paths), ("media-1\nmedia-2",))
        self.assertEqual([(u["filename"], u["mediaType"]) for u in self.fake.uploads.values()],
                         [("clip.mp4", "video"), ("still.png", "image")])
        self.assertEqual(self.fake.stored("upload/1"), b"0123456789")
        self.assertEqual(self.fake.vault, [("Drafts", ["media-1", "media-2"])])
        self.assertEqual(self.comfy.progress[-1], (200, 200))
        self.assertEqual(self.comfy.texts[-1], "Uploaded 2 to vault folder 'Drafts'")

    def test_an_empty_folder_skips_the_vault_call(self):
        self.save("  ", "LP", file_paths=self.file("a.webm"))
        self.assertEqual(self.fake.vault, [])
        self.assertEqual(self.comfy.texts[-1], "Uploaded 1 to your Fanvue media")

    def test_checks_inputs_before_calling_the_broker(self):
        self.assertEqual(self.error(self.save, "Drafts", "LP"), "Nothing to upload. Connect images, or enter file paths.")
        self.assertIn("Can't upload notes.txt: use one of .png", self.error(self.save, "D", "LP", file_paths="notes.txt"))
        missing = os.path.join(self.output_dir, "gone.mp4")
        self.assertEqual(self.error(self.save, "D", "LP", file_paths=missing), f"File not found: {missing}")
        empty = self.file("empty.mp4", b"")
        self.assertIn("Fanvue takes files from 1 byte to 1.5 GB", self.error(self.save, "D", "LP", file_paths=empty))
        self.assertEqual(self.fake.calls, [])

    def test_a_missing_key_stops_before_any_upload(self):
        os.remove(os.path.join(self.user_dir, client.KEY_FILENAME))
        self.assertIn("Add your LatentPost API key first", self.error(self.save, "D", "LP", file_paths=self.file("a.mp4")))
        self.assertEqual(self.fake.calls, [])

    def test_a_stop_error_mid_batch_files_what_was_uploaded(self):
        message = f"You've used all 30 free uploads this month. Upgrade on the dashboard: {self.fake.url}/dashboard"
        self.fake.fail("POST", "/uploads$", 402, "quota_exceeded", message, after=1, times=99)
        paths = "\n".join([self.file("a.mp4"), self.file("b.mp4"), self.file("c.mp4")])
        self.assertEqual(self.error(self.save, "Drafts", "LP", file_paths=paths),
                         f"{message}\n(1 of 3 uploaded before this, and filed in 'Drafts'.)")
        self.assertEqual(self.fake.vault, [("Drafts", ["media-1"])])
        self.assertEqual(len(self.fake.calls_to("POST", "/uploads$")), 2)  # stopped the batch, no retry

    @unittest.skipUnless(HAVE_IMAGE_LIBS, "needs numpy and Pillow")
    def test_uploads_images_as_png_without_workflow_metadata(self):
        import numpy as np

        class Tensor:  # just enough of a torch tensor
            def __init__(self, array):
                self.array = array

            def cpu(self):
                return self

            def numpy(self):
                return self.array

        batch = [Tensor(np.full((8, 8, 3), 0.5, dtype=np.float32)), Tensor(np.zeros((8, 8, 3), dtype=np.float32))]
        self.save("Drafts", "my/set", images=batch)
        names = [u["filename"] for u in self.fake.uploads.values()]
        self.assertRegex(names[0], r"^my_set_\d{8}-\d{6}_01\.png$")
        self.assertRegex(names[1], r"_02\.png$")
        png = self.fake.stored("upload/1")
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertNotIn(b"tEXt", png)
        self.assertNotIn(b"workflow", png)


class SchedulePostTest(NodeTest):
    post = nodes.SchedulePost().post

    def make(self, text="New set is up", audience="subscribers", minutes=60, at="", price=0, media="media-1"):
        return self.post(text, audience, minutes, at, price, media_uuids=media)

    def test_schedules_a_post_minutes_from_now(self):
        before = datetime.now(timezone.utc).replace(microsecond=0)
        self.assertEqual(self.make(media="media-1, media-2\nmedia-3"), ("post-1",))
        sent = self.fake.posts[0]
        self.assertEqual({k: sent[k] for k in ("audience", "text", "mediaUuids")},
                         {"audience": "subscribers", "text": "New set is up",
                          "mediaUuids": ["media-1", "media-2", "media-3"]})
        self.assertNotIn("price", sent)
        when = datetime.strptime(sent["publishAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        self.assertTrue(timedelta(minutes=60) <= when - before <= timedelta(minutes=60, seconds=5))
        self.assertTrue(self.comfy.texts[-1].startswith("Scheduled for "))

    def test_zero_minutes_publishes_now(self):
        self.make(minutes=0)
        self.assertNotIn("publishAt", self.fake.posts[0])
        self.assertEqual(self.comfy.texts[-1], "Published")

    def test_publish_at_is_local_time_unless_it_says_otherwise(self):
        self.make(at="2030-01-02 03:04", minutes=0)
        local = datetime(2030, 1, 2, 3, 4).astimezone().astimezone(timezone.utc)
        self.assertEqual(self.fake.posts[0]["publishAt"], local.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.make(at="2030-01-02T03:04Z")
        self.make(at="2030-01-02T03:04:00+02:00")
        self.assertEqual([p["publishAt"] for p in self.fake.posts[1:]], ["2030-01-02T03:04:00Z", "2030-01-02T01:04:00Z"])

    def test_paid_post(self):
        self.make(price=500)
        self.assertEqual(self.fake.posts[0]["price"], 500)

    def test_text_only_post(self):
        self.make(media="")
        self.assertNotIn("mediaUuids", self.fake.posts[0])

    def test_checks_inputs_before_calling_the_broker(self):
        cases = [
            ({"at": "2020-01-01 00:00"}, "publish_at (2020-01-01 00:00) is in the past. "
                                         "Set a later time, or clear it and use publish_in_minutes."),
            ({"at": "tomorrow 6pm"}, "Can't read publish_at 'tomorrow 6pm'. Write it like 2026-10-07 18:00."),
            ({"price": 299}, "Paid posts start at 300 cents ($3.00). Use 0 for a free post."),
            ({"price": 500, "media": None}, "A paid post needs media. Connect media_uuids from Save to Fanvue Vault."),
            ({"text": "x" * 5001}, "The caption is 5,001 characters; Fanvue allows 5,000."),
            ({"media": " ".join(f"m{n}" for n in range(101))}, "A post can have up to 100 media items; this one has 101."),
            ({"text": "  ", "media": ""}, "The post is empty. Add a caption, or connect media_uuids."),
        ]
        for inputs, message in cases:
            with self.subTest(message):
                self.assertEqual(self.error(self.make, **inputs), message)
        self.assertEqual(self.fake.calls, [])

    def test_free_plan_gets_the_brokers_upgrade_message(self):
        self.fake.plan = "free"
        self.assertEqual(self.error(self.make),
                         f"Scheduled and paid posts need Pro. Upgrade on the dashboard: {self.fake.url}/dashboard")


class NodeDefinitionTest(unittest.TestCase):
    def test_errors_are_titled_without_the_install_path(self):
        self.assertEqual(f"{LatentPostError.__module__}.{LatentPostError.__qualname__}", "latentpost.LatentPostError")

    def test_nodes_are_well_formed(self):
        self.assertEqual(set(nodes.NODE_CLASS_MAPPINGS), set(nodes.NODE_DISPLAY_NAME_MAPPINGS))
        for node_id, cls in nodes.NODE_CLASS_MAPPINGS.items():
            with self.subTest(node_id):
                inputs = cls.INPUT_TYPES()
                self.assertTrue(callable(getattr(cls, cls.FUNCTION)))
                self.assertEqual(len(cls.RETURN_TYPES), len(cls.RETURN_NAMES))
                self.assertTrue(set(inputs) <= {"required", "optional", "hidden"})
                self.assertNotIn("comfy", nodes.NODE_DISPLAY_NAME_MAPPINGS[node_id].lower())  # brand rule
                # An API key widget would end up in every saved workflow and PNG.
                self.assertFalse([name for group in inputs.values() for name in group if "key" in name])


if __name__ == "__main__":
    unittest.main()
