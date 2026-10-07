"""The LatentPost broker client used by the nodes. Standard library only.

Media never goes through the broker: upload() asks it for presigned part URLs, PUTs the bytes
straight to Fanvue's storage, then tells the broker the upload is complete.
The contract is docs/broker-design.md, "API for the nodes", in the LatentPost repo.
"""

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.2.2"
DEFAULT_URL = "https://latentpost.com"
SUPPORT_EMAIL = "support@latentpost.com"
SETTING_ID = "LatentPost.APIKey"  # registered by web/latentpost.js
# Points the nodes at another broker, such as `wrangler dev`. Not on the Settings page: add it to the file by hand.
URL_SETTING_ID = "LatentPost.BrokerURL"
SETTING_NAME = "Settings > LatentPost > API key"  # ASCII: ComfyUI logs errors to consoles that may be cp1252
USER_AGENT = f"LatentPost-ComfyUI/{VERSION}"

MAX_UPLOAD_BYTES = 1_610_612_736  # Fanvue's 1.5 GB limit
PART_URL_BATCH = 100  # the broker returns at most this many part URLs per call
VAULT_BATCH = 100  # media per vault call
READY_TIMEOUT = 600  # seconds to wait for Fanvue to process an upload
MEDIA_TYPES = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "image",
    ".mp4": "video", ".mov": "video", ".webm": "video", ".m4v": "video",
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".ogg": "audio", ".flac": "audio",
}

# What to do with each broker error code (the table in docs/broker-design.md). Codes not listed
# here are shown as written and not retried. Posts are stricter: see create_post().
WAIT_THEN_RETRY = {"rate_limited", "busy"}  # wait Retry-After
MAX_WAITS = 5
BACKOFF_ATTEMPTS = {"fanvue_unavailable": 3, "internal_error": 2, "unreachable": 3}
NETWORK_ERRORS = (OSError, http.client.HTTPException)  # URLError and timeouts are OSErrors


class LatentPostError(Exception):
    """A problem to show in ComfyUI. The message is written to be read as-is."""

    # ComfyUI titles the error dialog with module + class name, and our module's name is the pack's
    # full path on disk, which would put the user's folder names in every screenshot sent to support.
    __module__ = "latentpost"

    def __init__(self, message, code=None, retry_after=None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib would forward the Authorization header to wherever a redirect points.
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _http(method, url, headers, body=None, timeout=60):
    """Send one request; returns (status, headers, body bytes). Raises only on network errors."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _opener.open(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as err:
        with err:
            return err.code, err.headers, err.read()


def _reason(err):
    return str(getattr(err, "reason", None) or err or type(err).__name__)


def _seconds(value, default=5):
    try:
        return min(max(int(value), 0), 300)
    except (TypeError, ValueError):
        return default


def _settings(user_dir):
    """The default user's ComfyUI settings, or {} if there are none yet or the file is broken."""
    try:
        with open(os.path.join(user_dir, "default", "comfy.settings.json"), encoding="utf-8-sig") as f:
            settings = json.load(f)
    except (OSError, ValueError):  # ComfyUI replaces a broken file
        return {}
    return settings if isinstance(settings, dict) else {}


def _text_setting(settings, setting_id):
    value = settings.get(setting_id)
    return value.strip() if isinstance(value, str) else ""


def load_broker(user_dir, wait=time.sleep):
    """A Broker with the API key from ComfyUI's settings. Never from environment variables: the Registry's scan
    flags packs that read them."""
    settings = _settings(user_dir)
    base_url = _text_setting(settings, URL_SETTING_ID).rstrip("/") or DEFAULT_URL
    url = urllib.parse.urlsplit(base_url)
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")):
        raise LatentPostError(f"{URL_SETTING_ID} in ComfyUI's settings must start with https:// "
                              "(http:// only works for localhost).")
    dashboard = f"{base_url}/dashboard"

    key = _text_setting(settings, SETTING_ID)
    if not key:
        raise LatentPostError(f"Add your LatentPost API key first. Copy it from {dashboard} "
                              f"and paste it into ComfyUI's {SETTING_NAME}.", "no_api_key")
    if not key.startswith("fvc_") or any(c.isspace() for c in key):
        raise LatentPostError(f"The API key in {SETTING_NAME} doesn't look right: keys start with fvc_ "
                              f"and have no spaces. Copy it again from {dashboard}", "no_api_key")
    return Broker(key, base_url, key_source=SETTING_NAME, wait=wait)


class Broker:
    """Calls the broker's /api/v1 endpoints, retrying the way the error table says."""

    def __init__(self, api_key, base_url=DEFAULT_URL, key_source=None, wait=time.sleep):
        self.api_key = api_key
        self.base_url = base_url
        self.key_source = key_source
        self.wait = wait  # wait(seconds); the nodes pass one that stops when the run is cancelled

    def call(self, method, path, body=None, backoff=True):
        waits, failures = 0, 0
        while True:
            try:
                return self._request(method, path, body)
            except LatentPostError as err:
                if err.code in WAIT_THEN_RETRY and waits < MAX_WAITS:
                    waits += 1
                    self.wait(err.retry_after)
                    continue
                failures += 1
                if backoff and failures < BACKOFF_ATTEMPTS.get(err.code, 0):
                    self.wait(2 ** failures)  # 2 s, then 4 s
                    continue
                raise self._explain(err) from None

    def _request(self, method, path, body):
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json",
                   "User-Agent": USER_AGENT}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            status, response_headers, raw = _http(method, self.base_url + path, headers, data)
        except NETWORK_ERRORS as err:
            raise LatentPostError(f"Couldn't reach LatentPost at {self.base_url} ({_reason(err)}). "
                                  "Check your internet connection and try again.", "unreachable")
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = None
        if 200 <= status < 300 and isinstance(payload, dict):
            return payload
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict) or not error.get("message"):
            raise LatentPostError(f"LatentPost sent an unexpected response (HTTP {status}). Try again shortly. "
                                  f"If it keeps happening, email {SUPPORT_EMAIL}.",
                                  "unreachable" if status >= 500 else "unexpected_response")
        raise LatentPostError(str(error["message"]), str(error.get("code")),
                              retry_after=_seconds(response_headers.get("Retry-After")))

    def _explain(self, err):
        """Add what only the node knows: where the key lives, and the dashboard link."""
        if err.code == "invalid_api_key" and self.key_source:
            return LatentPostError(f"{err}\nLatentPost reads your key from {self.key_source}.", err.code)
        if err.code == "reconnect_required" and "/dashboard" not in str(err):
            return LatentPostError(f"{err}\n{self.base_url}/dashboard", err.code)
        return err

    def upload(self, filename, media_type, stream, size, on_progress=None):
        """Upload one file from a binary stream; returns its media UUID once Fanvue has processed it."""
        session = self.call("POST", "/api/v1/uploads",
                            {"filename": filename, "mediaType": media_type, "sizeBytes": size})
        upload_path = "/api/v1/uploads/" + urllib.parse.quote(session["uploadId"], safe="")
        part_size, total = session["partSize"], session["totalParts"]
        urls = {part["partNumber"]: part["url"] for part in session["parts"]}
        etags = []
        for number in range(1, total + 1):
            if number not in urls:
                last = min(total, number + PART_URL_BATCH - 1)
                more = self.call("GET", f"{upload_path}/parts?from={number}&to={last}")
                urls.update({part["partNumber"]: part["url"] for part in more["parts"]})
            if number not in urls:
                raise LatentPostError(f"LatentPost didn't send an upload URL for part {number} of {filename}. "
                                      "Try again.", "bad_response")
            stream.seek((number - 1) * part_size)
            etag = self._put_part(urls.pop(number), stream.read(part_size), number, filename)
            etags.append({"partNumber": number, "etag": etag})
            if on_progress:
                on_progress(min(number * part_size, size) / size)
            self.wait(0)  # lets a cancelled run stop between parts
        self.call("POST", upload_path + "/complete", {"parts": etags})
        self._wait_until_ready(session["mediaUuid"], filename)
        return session["mediaUuid"]

    def _put_part(self, url, data, number, filename):
        # Straight to Fanvue's storage. The presigned URL is the credential; no API key here.
        for attempt in (1, 2, 3):
            try:
                status, headers, _ = _http("PUT", url, {"Content-Type": "application/octet-stream"}, data, timeout=300)
            except NETWORK_ERRORS as err:
                status, problem = None, f"couldn't reach it: {_reason(err)}"
            else:
                if 200 <= status < 300 and headers.get("ETag"):
                    return headers["ETag"]
                problem = f"HTTP {status}"
            if attempt == 3 or (status is not None and status < 500):
                raise LatentPostError(f"Fanvue's storage didn't accept part {number} of {filename} ({problem}). "
                                      "Try again.", "storage_error")
            self.wait(2 ** attempt)

    def _wait_until_ready(self, media_uuid, filename):
        # There's no media-ready webhook, so poll: 1 s doubling to 30 s, for about 10 minutes.
        delay, waited = 1, 0
        while True:
            status = self.call("GET", "/api/v1/media/" + urllib.parse.quote(media_uuid, safe=""))["status"]
            if status == "ready":
                return
            if status == "error":
                raise LatentPostError(f"Fanvue couldn't process {filename}. The file may be damaged "
                                      "or in a format Fanvue doesn't take.", "media_error")
            if waited >= READY_TIMEOUT:
                raise LatentPostError(f"Fanvue is still processing {filename} after 10 minutes. It may finish "
                                      "later, so check your Fanvue media before uploading it again.", "media_timeout")
            self.wait(delay)
            waited += delay
            delay = min(delay * 2, 30)

    def add_to_vault(self, folder, media_uuids):
        """File media in a vault folder, creating the folder if needed. One call per 100 items."""
        added = 0
        for start in range(0, len(media_uuids), VAULT_BATCH):
            batch = media_uuids[start:start + VAULT_BATCH]
            added += self.call("POST", "/api/v1/vault/media", {"folder": folder, "mediaUuids": batch})["addedCount"]
        return added

    def create_post(self, post):
        # Never post twice. rate_limited and busy come before Fanvue creates anything, so those are
        # retried. The errors other calls back off on can come after the post was made: don't retry.
        try:
            return self.call("POST", "/api/v1/posts", post, backoff=False)
        except LatentPostError as err:
            if err.code not in BACKOFF_ATTEMPTS:
                raise
            raise LatentPostError(f"{err}\nThe post may have been created anyway. "
                                  "Check your Fanvue posts before you try again.", err.code) from None
