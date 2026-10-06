"""Run the nodes inside a real ComfyUI, started headless on a spare port. Not part of the unit tests.

  python tests/e2e_comfyui.py fake         # against the fake broker; uses a scratch user folder
  python tests/e2e_comfyui.py plan         # the plan latentpost.com sees for your key file (never prints the key)
  python tests/e2e_comfyui.py live --yes   # REAL: 1 plain image to vault folder "LatentPost test", plus a
                                           # subscribers-only post 60 minutes out. Delete it in Fanvue after.

COMFY_DIR is the folder with ComfyUI's main.py. It defaults to ComfyUI Desktop's install. Its Python is
COMFY_DIR/.venv, or COMFY_PY. The live and plan modes read the key from COMFY_DIR/user, as the nodes do.
Close the ComfyUI app first: the headless copy shares its user folder.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

from fake_broker import API_KEY, FakeBroker
from support import client

COMFY_DIR = os.environ.get("COMFY_DIR") or os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "Comfy-Desktop", "ComfyUI-Installs", "ComfyUI", "ComfyUI")
COMFY_PY = os.environ.get("COMFY_PY") or os.path.join(COMFY_DIR, ".venv", "Scripts", "python.exe")
PORT = int(os.environ.get("COMFY_PORT", "8190"))
COMFY = f"http://127.0.0.1:{PORT}"


def request(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(COMFY + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def run(workflow, timeout=120, interrupt_after=None):
    """Queue a workflow; returns (status, [(message type, details)]) once it finishes."""
    prompt_id = request("/prompt", {"prompt": workflow})["prompt_id"]
    start = time.time()
    while time.time() - start < timeout:
        if interrupt_after and time.time() - start > interrupt_after:
            request("/interrupt", {})
            interrupt_after = None
        entry = request(f"/history/{prompt_id}").get(prompt_id, {})
        status = entry.get("status", {})
        if status.get("status_str") in ("success", "error"):
            return status["status_str"], [
                (kind, {k: v for k, v in details.items() if k in ("node_type", "exception_type", "exception_message")})
                for kind, details in status["messages"] if kind not in ("execution_start", "execution_cached")]
        time.sleep(0.3)
    raise SystemExit("timed out waiting for ComfyUI")


def workflow(text, folder="E2E", size=64, batch=2, color=0x336699):
    return {
        "1": {"class_type": "EmptyImage", "inputs": {"width": size, "height": size, "batch_size": batch, "color": color}},
        "2": {"class_type": "LatentPostSaveToVault", "inputs": {
            "images": ["1", 0], "folder": folder, "filename_prefix": "latentpost-test", "file_paths": ""}},
        "3": {"class_type": "LatentPostSchedulePost", "inputs": {
            "media_uuids": ["2", 0], "text": text, "audience": "subscribers",
            "publish_in_minutes": 60, "publish_at": "", "price_cents": 0}},
    }


class Comfy:
    def __init__(self, env, args):
        env = dict(env, PYTHONUTF8="1")  # other node packs log emoji, which cp1252 can't write
        self.log_path = os.path.join(tempfile.gettempdir(), f"latentpost_e2e_comfy_{PORT}.log")
        self.log = open(self.log_path, "w", encoding="utf-8")
        self.process = subprocess.Popen(
            [COMFY_PY, "main.py", "--port", str(PORT), "--listen", "127.0.0.1", "--disable-auto-launch", *args],
            cwd=COMFY_DIR, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(600):
            if self.process.poll() is not None:
                raise SystemExit(f"ComfyUI exited; see {self.log_path}")
            try:
                request("/object_info/LatentPostSaveToVault")
                return
            except OSError:
                time.sleep(0.5)
        raise SystemExit("ComfyUI didn't start")

    def node_log(self):
        with open(self.log_path, encoding="utf-8", errors="replace") as f:
            return [line.strip() for line in f if "LatentPost: " in line]

    def close(self):
        self.process.terminate()
        self.process.wait(15)
        self.log.close()


def fake():
    broker = FakeBroker(part_size=1000)
    env = dict(os.environ, LATENTPOST_URL=broker.url, LATENTPOST_API_KEY=API_KEY)
    comfy = Comfy(env, ["--user-directory", tempfile.mkdtemp()])
    try:
        print("1. 2 images to the vault, then a post:", run(workflow("hello")))
        print("   uploads:", len(broker.uploads), "vault:", broker.vault, "posts:", broker.posts)
        print("2. same again, so cached:", run(workflow("hello"))[0], "posts:", len(broker.posts))
        broker.plan = "free"
        print("3. free plan:", run(workflow("free")))
        broker.plan = "pro"
        broker.fail("POST", "/posts$", 502, "fanvue_unavailable", "Fanvue returned an error (500).", times=5)
        before = len(broker.calls_to("POST", "/posts$"))
        print("4. 502 on the post:", run(workflow("502")), "attempts:", len(broker.calls_to("POST", "/posts$")) - before)
        broker.polls_until_ready = 10 ** 6
        print("5. cancelled while Fanvue processes:", run(workflow("cancel", color=0x112233), interrupt_after=3))
    finally:
        comfy.close()
        broker.close()


def plan():
    try:
        me = client.load_broker(os.path.join(COMFY_DIR, "user")).call("GET", "/api/v1/me")
    except client.LatentPostError as err:
        raise SystemExit(f"{err.code}: {err}")
    print(f"plan={me['plan']} uploads this month={me['uploadsThisMonth']}/{me['uploadsPerMonth']}")


def live():
    if "--yes" not in sys.argv:
        raise SystemExit("This creates a real scheduled post. Run again with --yes.")
    env = {k: v for k, v in os.environ.items() if k not in ("LATENTPOST_URL", "LATENTPOST_API_KEY")}
    comfy = Comfy(env, [])
    try:
        print(run(workflow("LatentPost test post. Please ignore.", folder="LatentPost test", size=512, batch=1,
                           color=0x5A7FA8), timeout=900))
        for line in comfy.node_log():
            print(re.sub(r"^.*?LatentPost: ", "LatentPost: ", line))
    finally:
        comfy.close()


if __name__ == "__main__":
    {"fake": fake, "plan": plan, "live": live}[sys.argv[1]]()
