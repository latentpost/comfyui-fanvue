"""Loads the node pack the way ComfyUI does, with stand-ins for the ComfyUI modules it imports."""

import importlib.util
import os
import sys
import types
import unittest
from unittest import mock

PACK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ComfyUI imports each custom-node folder as a package, so the pack's relative imports work.
_spec = importlib.util.spec_from_file_location("latentpost_pack", os.path.join(PACK_DIR, "__init__.py"),
                                               submodule_search_locations=[PACK_DIR])
pack = importlib.util.module_from_spec(_spec)
sys.modules["latentpost_pack"] = pack
_spec.loader.exec_module(pack)
client = sys.modules["latentpost_pack.client"]
nodes = sys.modules["latentpost_pack.nodes"]


class FakeComfy:
    """folder_paths, comfy.model_management, comfy.utils and server, recording what the nodes report."""

    def __init__(self, user_dir, output_dir):
        self.progress = []  # (value, total)
        self.texts = []
        fake = self

        class ProgressBar:
            def __init__(self, total):
                self.total = total

            def update_absolute(self, value, total=None, preview=None):
                fake.progress.append((value, self.total))

        class PromptServer:
            instance = types.SimpleNamespace(send_progress_text=lambda text, node_id, sid=None: fake.texts.append(text))

        folder_paths = types.ModuleType("folder_paths")
        folder_paths.get_user_directory = lambda: user_dir
        folder_paths.get_output_directory = lambda: output_dir
        model_management = types.ModuleType("comfy.model_management")
        model_management.throw_exception_if_processing_interrupted = lambda: None
        utils = types.ModuleType("comfy.utils")
        utils.ProgressBar = ProgressBar
        comfy = types.ModuleType("comfy")
        comfy.model_management, comfy.utils = model_management, utils
        server = types.ModuleType("server")
        server.PromptServer = PromptServer
        self.modules = {"folder_paths": folder_paths, "comfy": comfy, "comfy.model_management": model_management,
                        "comfy.utils": utils, "server": server}

    def install(self, test: unittest.TestCase):
        patcher = mock.patch.dict(sys.modules, self.modules)
        patcher.start()
        test.addCleanup(patcher.stop)


def clean_env(test: unittest.TestCase, **values):
    """Run the test with LatentPost's environment variables set to exactly `values`."""
    patcher = mock.patch.dict(os.environ, values)
    patcher.start()
    test.addCleanup(patcher.stop)
    for name in ("LATENTPOST_API_KEY", "LATENTPOST_URL"):
        if name not in values:
            os.environ.pop(name, None)
