"""The ComfyUI nodes. ComfyUI's own modules, numpy and Pillow are imported inside functions,
so the tests can import this file without ComfyUI."""

import hashlib
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

from .client import MAX_UPLOAD_BYTES, MEDIA_TYPES, SUPPORT_EMAIL, LatentPostError, load_broker

CATEGORY = "LatentPost"
AUDIENCES = ["subscribers", "followers-and-subscribers"]
MIN_PRICE_CENTS = 300
MAX_TEXT = 5000
MAX_POST_MEDIA = 100

log = logging.getLogger("latentpost")

# What the nodes already did in this ComfyUI process: a digest of a node's inputs -> (result, status
# text). Running again with the same inputs returns the earlier result, so nothing is uploaded or posted
# twice. ComfyUI's own cache can't promise that: it drops a prompt's results once another prompt runs a
# node, and from 0.35 also when RAM runs low.
_done = {}


def _digest(*inputs):
    return hashlib.sha256(json.dumps(inputs).encode()).hexdigest()


def _image_digest(image):
    pixels = image.cpu().numpy()
    digest = hashlib.sha256(f"{pixels.dtype} {pixels.shape}".encode())
    digest.update(pixels.tobytes())
    return digest.hexdigest()


def _file_digest(path):
    """A file counts as changed when its size or modified time does."""
    stat = os.stat(path)
    return [os.path.normcase(path), stat.st_size, stat.st_mtime_ns]


def _again(key, unique_id, verb):
    """The earlier result for the same inputs, saying so on the node."""
    result, done = _done[key]
    status = _Status(unique_id, 1)
    status.progress(1)
    message = f"Already done in an earlier run: {done}. Change an input to {verb} again."
    status.say(message)
    log.info("LatentPost: %s", message)
    return result


def _wait(seconds):
    """Sleep, but stop promptly when the run is cancelled in ComfyUI."""
    import comfy.model_management

    end = time.monotonic() + seconds
    while True:
        comfy.model_management.throw_exception_if_processing_interrupted()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(left, 0.25))


def _broker():
    import folder_paths
    from comfy.cli_args import args

    if args.multi_user:
        # Each user has their own settings, and a run doesn't say which user queued it.
        raise LatentPostError("LatentPost doesn't work with ComfyUI's --multi-user mode yet: the nodes can't tell "
                              f"whose Settings hold the API key. Start ComfyUI without it, or email {SUPPORT_EMAIL}.",
                              "multi_user")
    return load_broker(folder_paths.get_user_directory(), wait=_wait)


class _Status:
    """The node's progress bar, plus a line of text on the node where ComfyUI supports it."""

    def __init__(self, node_id, total):
        import comfy.utils

        self.node_id = node_id
        self.total = total
        self.bar = comfy.utils.ProgressBar(total * 100)

    def progress(self, done):
        self.bar.update_absolute(int(done * 100))

    def say(self, text):
        try:
            from server import PromptServer

            PromptServer.instance.send_progress_text(text, self.node_id)
        except Exception:  # older ComfyUI, or no server (tests)
            pass


def _png(image):
    """One IMAGE from a batch, as PNG bytes. No workflow metadata, unlike Save Image."""
    import numpy as np
    from PIL import Image

    pixels = np.clip(255.0 * image.cpu().numpy(), 0, 255).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(pixels).save(buffer, format="PNG", compress_level=4)
    return buffer


def _paths(file_paths):
    """One path per line. Quotes from Windows' "Copy as path" are fine; relative means the output folder."""
    paths = []
    for line in file_paths.splitlines():
        path = line.strip().strip('"').strip()
        if not path:
            continue
        if not os.path.isabs(path):
            import folder_paths

            path = os.path.join(folder_paths.get_output_directory(), path)
        media_type = MEDIA_TYPES.get(os.path.splitext(path)[1].lower())
        if media_type is None:
            raise LatentPostError(f"Can't upload {os.path.basename(path)}: use one of {' '.join(MEDIA_TYPES)}.")
        if not os.path.isfile(path):
            raise LatentPostError(f"File not found: {path}")
        size = os.path.getsize(path)
        if not 0 < size <= MAX_UPLOAD_BYTES:
            raise LatentPostError(f"Can't upload {os.path.basename(path)}: Fanvue takes files from 1 byte to 1.5 GB.")
        paths.append((path, media_type))
    return paths


class SaveToVault:
    DESCRIPTION = ("Uploads images, or files on disk such as videos, to your Fanvue vault. Media goes straight "
                   "from this computer to Fanvue; LatentPost never sees it. Workflow metadata isn't included.")
    CATEGORY = CATEGORY
    FUNCTION = "save"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("media_uuids",)
    OUTPUT_TOOLTIPS = ("Fanvue media IDs, one per line. Connect to Schedule Fanvue Post.",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "folder": ("STRING", {"default": "ComfyUI", "tooltip":
                           "Vault folder for the uploads, created if it doesn't exist. Leave empty for no folder."}),
                "filename_prefix": ("STRING", {"default": "LatentPost", "tooltip":
                                    "Images are named <prefix>_<date>-<time>_<n>.png. Files keep their own names."}),
            },
            "optional": {
                "images": ("IMAGE", {"tooltip": "Images to upload, as PNG."}),
                "file_paths": ("STRING", {"multiline": True, "default": "", "tooltip":
                               "Files to upload, one path per line. Relative paths are inside ComfyUI's output folder."}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    def save(self, folder, filename_prefix, images=None, file_paths="", unique_id=None):
        folder = folder.strip()
        prefix = re.sub(r"[\\/]", "_", filename_prefix.strip()) or "LatentPost"
        images = list(images) if images is not None else []
        paths = _paths(file_paths)
        if not images and not paths:
            raise LatentPostError("Nothing to upload. Connect images, or enter file paths.")
        key = _digest("save", folder, prefix, [_image_digest(image) for image in images],
                      [_file_digest(path) for path, _ in paths])
        if key in _done:
            return _again(key, unique_id, "upload")

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        # (filename, media type, open) for each item. Images are encoded one at a time, as they're sent.
        items = [(f"{prefix}_{stamp}_{n:02}.png", "image", lambda i=image: _png(i))
                 for n, image in enumerate(images, 1)]
        items += [(os.path.basename(path), media_type, lambda p=path: open(p, "rb")) for path, media_type in paths]

        broker = _broker()
        status = _Status(unique_id, len(items))
        uploaded = []
        try:
            for index, (filename, media_type, open_item) in enumerate(items):
                status.say(f"Uploading {index + 1} of {len(items)}: {filename}")
                with open_item() as stream:
                    size = stream.seek(0, io.SEEK_END)
                    uploaded.append(broker.upload(filename, media_type, stream, size,
                                                  on_progress=lambda f, i=index: status.progress(i + f)))
        except LatentPostError as err:
            if not uploaded:
                raise
            note = f"{len(uploaded)} of {len(items)} uploaded before this"
            if folder:
                try:
                    broker.add_to_vault(folder, uploaded)
                    note += f", and filed in '{folder}'"
                except LatentPostError:
                    pass
            raise LatentPostError(f"{err}\n({note}.)", err.code) from None

        if folder:
            broker.add_to_vault(folder, uploaded)
        done = f"Uploaded {len(uploaded)} to " + (f"vault folder '{folder}'" if folder else "your Fanvue media")
        status.say(done)
        log.info("LatentPost: %s", done)
        _done[key] = (("\n".join(uploaded),), done)
        return _done[key][0]


def _media_uuids(text):
    return [uuid for uuid in re.split(r"[\s,]+", text or "") if uuid]


def _publish_at(publish_at, publish_in_minutes, now):
    """Fanvue's publishAt (UTC ISO 8601), or None to publish right away."""
    if publish_at.strip():
        text = publish_at.strip()
        if text[-1] in "Zz":
            text = text[:-1] + "+00:00"  # fromisoformat only reads "Z" from Python 3.11
        try:
            when = datetime.fromisoformat(text)
        except ValueError:
            raise LatentPostError(f"Can't read publish_at '{publish_at}'. Write it like 2026-10-07 18:00.") from None
        if when.tzinfo is None:
            when = when.astimezone()  # this computer's time zone
        if when <= now:
            raise LatentPostError(f"publish_at ({publish_at}) is in the past. "
                                  "Set a later time, or clear it and use publish_in_minutes.")
    elif publish_in_minutes > 0:
        when = now + timedelta(minutes=publish_in_minutes)
    else:
        return None
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_time(iso):
    try:
        local = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return iso  # the post exists; don't fail over how to display its time
    return f"{local:%a %d %b %Y, %H:%M} (this computer's time)"


class SchedulePost:
    DESCRIPTION = ("Creates a Fanvue post, published now or at a set time. Fanvue does the scheduling, "
                   "so ComfyUI doesn't need to stay open. Needs LatentPost Pro.")
    CATEGORY = CATEGORY
    FUNCTION = "post"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("post_uuid",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {"multiline": True, "default": "", "tooltip":
                         f"The post's caption, up to {MAX_TEXT:,} characters."}),
                "audience": (AUDIENCES, {"default": "subscribers"}),
                "publish_in_minutes": ("INT", {"default": 60, "min": 0, "max": 525600, "tooltip":
                                       "0 publishes right away. Ignored when publish_at is set."}),
                "publish_at": ("STRING", {"default": "", "tooltip":
                               "A set time like 2026-10-07 18:00, in this computer's time zone "
                               "(add Z for UTC). Overrides publish_in_minutes."}),
                "price_cents": ("INT", {"default": 0, "min": 0, "max": 10_000_000, "tooltip":
                                f"0 for a free post. Paid posts start at {MIN_PRICE_CENTS} ($3.00) and need media."}),
            },
            "optional": {
                "media_uuids": ("STRING", {"forceInput": True, "tooltip":
                                "Fanvue media IDs, from Save to Fanvue Vault. Leave unconnected for a text-only post."}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    def post(self, text, audience, publish_in_minutes, publish_at, price_cents, media_uuids="", unique_id=None):
        media = _media_uuids(media_uuids)
        if len(text) > MAX_TEXT:
            raise LatentPostError(f"The caption is {len(text):,} characters; Fanvue allows {MAX_TEXT:,}.")
        if len(media) > MAX_POST_MEDIA:
            raise LatentPostError(f"A post can have up to {MAX_POST_MEDIA} media items; this one has {len(media)}.")
        if 0 < price_cents < MIN_PRICE_CENTS:
            raise LatentPostError(f"Paid posts start at {MIN_PRICE_CENTS} cents ($3.00). Use 0 for a free post.")
        if price_cents and not media:
            raise LatentPostError("A paid post needs media. Connect media_uuids from Save to Fanvue Vault.")
        if not text.strip() and not media:
            raise LatentPostError("The post is empty. Add a caption, or connect media_uuids.")

        key = _digest("post", text, audience, publish_in_minutes, publish_at, price_cents, media)
        if key in _done:  # before the time checks: an earlier publish_at may have passed since
            return _again(key, unique_id, "post")

        post = {"audience": audience}
        if text.strip():
            post["text"] = text
        if media:
            post["mediaUuids"] = media
        if price_cents:
            post["price"] = price_cents
        when = _publish_at(publish_at, publish_in_minutes, datetime.now(timezone.utc))
        if when:
            post["publishAt"] = when

        status = _Status(unique_id, 1)
        status.say("Creating the post")
        created = _broker().create_post(post)
        status.progress(1)
        done = f"Scheduled for {_local_time(created.get('publishAt') or when)}" if when else "Published"
        status.say(done)
        log.info("LatentPost: post %s. %s", created["uuid"], done)
        _done[key] = ((created["uuid"],), done)
        return _done[key][0]


NODE_CLASS_MAPPINGS = {
    "LatentPostSaveToVault": SaveToVault,
    "LatentPostSchedulePost": SchedulePost,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "LatentPostSaveToVault": "Save to Fanvue Vault",
    "LatentPostSchedulePost": "Schedule Fanvue Post",
}
