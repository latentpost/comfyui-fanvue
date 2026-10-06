"""A stand-in for the LatentPost broker, and for Fanvue's storage, on localhost.

Response shapes, checks and messages mirror broker/src/api.ts. Tests queue errors with fail().
"""

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_KEY = "fvc_" + "k" * 43
AUDIENCES = ["subscribers", "followers-and-subscribers"]
MEDIA_TYPES = ["image", "video", "audio", "document"]


class FakeBroker:
    def __init__(self, part_size=1024, polls_until_ready=1):
        self.part_size = part_size
        self.polls_until_ready = polls_until_ready
        self.final_status = "ready"
        self.plan = "pro"
        self.uploads = {}  # uploadId -> {"filename", "size", "mediaUuid", "parts": {n: bytes}, "completed"}
        self.polls = {}  # mediaUuid -> status polls so far
        self.vault = []  # (folder, mediaUuids) per call
        self.posts = []
        self.calls = []  # (method, raw path) of every broker API call
        self.storage_headers = []  # headers of every storage PUT
        self.storage_errors = []  # HTTP statuses for the next storage PUTs to fail with
        self.failures = []  # [method, path regex, status, body, headers, times left]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def fail(self, method, path, status, code=None, message="", headers=None, times=1, raw=None, after=0):
        """Answer `times` matching calls with an error instead, after letting `after` of them through."""
        body = raw if raw is not None else json.dumps({"error": {"code": code, "message": message}}).encode()
        self.failures.append([method, re.compile(path), status, body, headers or {}, times, after])

    def stored(self, upload_id):
        upload = self.uploads[upload_id]
        return b"".join(upload["parts"][n] for n in sorted(upload["parts"]))

    def calls_to(self, method, pattern):
        return [path for m, path in self.calls if m == method and re.search(pattern, path)]


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def bad_request(message):
    return ApiError(400, "bad_request", message)


def _handler(fake):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def _send(self, status, body, headers=None):
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self, method):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            path = self.path.split("?")[0]
            if path.startswith("/storage/"):
                return self._storage(path, raw)
            fake.calls.append((method, self.path))
            for failure in fake.failures:
                if failure[0] == method and failure[1].search(path) and failure[5] > 0:
                    if failure[6] > 0:
                        failure[6] -= 1
                        break
                    failure[5] -= 1
                    return self._send(failure[2], failure[3], failure[4])
            try:
                if self.headers.get("Authorization") != f"Bearer {API_KEY}":
                    raise ApiError(401, "invalid_api_key",
                                   f"API key not recognised. Copy a new one from {fake.url}/dashboard")
                body = json.loads(raw) if raw else {}
                status, data = self._route(method, path, body)
                self._send(status, json.dumps(data).encode(), {"Content-Type": "application/json"})
            except ApiError as err:
                error = json.dumps({"error": {"code": err.code, "message": str(err)}}).encode()
                self._send(err.status, error, {"Content-Type": "application/json"})

        def _storage(self, path, raw):
            # /storage/{uploadId}/{partNumber}: a presigned PUT, so no API key is expected.
            fake.storage_headers.append(dict(self.headers))
            if fake.storage_errors:
                return self._send(fake.storage_errors.pop(0), b"<Error>AccessDenied</Error>")
            _, _, upload_id, number = path.split("/")
            upload = fake.uploads[urllib.parse.unquote(upload_id)]
            upload["parts"][int(number)] = raw
            self._send(200, b"", {"ETag": f'"etag-{number}"'})

        def _route(self, method, path, body):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            routes = [
                ("POST", r"^/api/v1/uploads$", self._create_upload),
                ("GET", r"^/api/v1/uploads/([^/]+)/parts$", self._part_urls),
                ("POST", r"^/api/v1/uploads/([^/]+)/complete$", self._complete),
                ("GET", r"^/api/v1/media/([^/]+)$", self._media),
                ("POST", r"^/api/v1/vault/media$", self._vault),
                ("POST", r"^/api/v1/posts$", self._post),
            ]
            for route_method, pattern, handler in routes:
                match = re.match(pattern, path)
                if match and route_method == method:
                    return handler(body, query, *map(urllib.parse.unquote, match.groups()))
            raise ApiError(404, "not_found", "Not found")

        def _parts(self, upload_id, first, last):
            return [{"partNumber": n, "url": f"{fake.url}/storage/{urllib.parse.quote(upload_id, safe='')}/{n}"}
                    for n in range(first, last + 1)]

        def _create_upload(self, body, query):
            text(body.get("filename"), "filename", 255)
            if body.get("mediaType") not in MEDIA_TYPES:
                raise bad_request(f"mediaType must be one of: {', '.join(MEDIA_TYPES)}")
            size = whole(body.get("sizeBytes"), "sizeBytes", 1, 1_610_612_736)
            # IDs can contain "/", so the node has to URL-encode them in paths.
            upload_id = f"upload/{len(fake.uploads) + 1}"
            media_uuid = f"media-{len(fake.uploads) + 1}"
            fake.uploads[upload_id] = {"filename": body["filename"], "mediaType": body["mediaType"], "size": size,
                                       "mediaUuid": media_uuid, "parts": {}, "completed": None}
            fake.polls[media_uuid] = 0
            total = -(-size // fake.part_size)
            return 201, {"uploadId": upload_id, "mediaUuid": media_uuid, "partSize": fake.part_size,
                         "totalParts": total, "parts": self._parts(upload_id, 1, min(total, 100))}

        def _part_urls(self, body, query, upload_id):
            first = whole(int(query.get("from", ["0"])[0]), "from", 1, 10_000)
            last = whole(int(query.get("to", ["0"])[0]), "to", first, first + 99)
            return 200, {"partSize": fake.part_size, "parts": self._parts(upload_id, first, last)}

        def _complete(self, body, query, upload_id):
            upload = fake.uploads[upload_id]
            parts = body.get("parts")
            if not isinstance(parts, list) or not parts:
                raise bad_request("parts must be a list of {partNumber, etag}")
            expected = [{"partNumber": n, "etag": f'"etag-{n}"'} for n in sorted(upload["parts"])]
            if parts != expected or len(fake.stored(upload_id)) != upload["size"]:
                raise ApiError(400, "fanvue_rejected", "Fanvue rejected the request: parts don't match")
            upload["completed"] = parts
            return 200, {"status": "processing"}

        def _media(self, body, query, media_uuid):
            fake.polls[media_uuid] += 1
            done = fake.polls[media_uuid] > fake.polls_until_ready
            return 200, {"uuid": media_uuid, "status": fake.final_status if done else "processing"}

        def _vault(self, body, query):
            folder = text(body.get("folder"), "folder", 255)
            ids(body.get("mediaUuids"), "mediaUuids", 100)
            fake.vault.append((folder, body["mediaUuids"]))
            return 200, {"folder": folder, "addedCount": len(body["mediaUuids"])}

        def _post(self, body, query):
            if body.get("audience") not in AUDIENCES:
                raise bad_request(f"audience must be one of: {', '.join(AUDIENCES)}")
            if "price" in body:
                whole(body["price"], "price", 300, 10_000_000)
                if "mediaUuids" not in body:
                    raise bad_request("a paid post needs at least one media item")
            if fake.plan != "pro":
                raise ApiError(402, "plan_required",
                               f"Scheduled and paid posts need Pro. Upgrade on the dashboard: {fake.url}/dashboard")
            fake.posts.append(body)
            return 201, {"uuid": f"post-{len(fake.posts)}", "publishAt": body.get("publishAt"),
                         "publishedAt": None if body.get("publishAt") else "2026-10-06T12:00:00.000Z"}

    return Handler


def text(value, field, limit):
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        raise bad_request(f"{field} must be text of 1 to {limit} characters")
    return value


def whole(value, field, low, high):
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise bad_request(f"{field} must be a whole number from {low} to {high}")
    return value


def ids(value, field, limit):
    if not isinstance(value, list) or not 0 < len(value) <= limit or not all(isinstance(i, str) and i for i in value):
        raise bad_request(f"{field} must be a list of 1 to {limit} IDs")
    return value
