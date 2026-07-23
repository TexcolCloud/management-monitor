from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from common.runtime_config import load_runtime_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ConfigConsistencyTest(unittest.TestCase):
    def test_python_and_node_runtime_config_match(self) -> None:
        script = """
const c = require('./common/runtime-config').loadRuntimeConfig();
console.log(JSON.stringify({
  listApiUrl: c.listApiUrl,
  detailApiUrl: c.detailApiUrl,
  attachmentDownloadUrl: c.attachmentDownloadUrl,
  retryableApiCodes: c.retryableApiCodes,
  attachmentRetries: c.attachmentRetries,
  attachmentConcurrency: c.attachmentConcurrency,
  attachmentMaxFailures: c.attachmentMaxFailures,
  captureRoot: c.captureRoot,
  attachmentDirName: c.attachmentDirName
}));
"""
        completed = subprocess.run(
            ["node", "-e", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        node = json.loads(completed.stdout)
        python = load_runtime_config()
        expected = {
            "listApiUrl": python.list_api_url,
            "detailApiUrl": python.detail_api_url,
            "attachmentDownloadUrl": python.attachment_download_url,
            "retryableApiCodes": list(python.retryable_api_codes),
            "attachmentRetries": python.attachment_retries,
            "attachmentConcurrency": python.attachment_concurrency,
            "attachmentMaxFailures": python.attachment_max_failures,
            "captureRoot": str(python.capture_root).replace("\\", "/"),
            "attachmentDirName": python.attachment_dir_name,
        }
        node["captureRoot"] = node["captureRoot"].replace("\\", "/")
        self.assertEqual(node, expected)


if __name__ == "__main__":
    unittest.main()
