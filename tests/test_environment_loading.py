from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.environment import load_environment


class EnvironmentLoadingTest(unittest.TestCase):
    def test_environment_file_loads_without_overriding_process_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
                "WORKORDER_ENV_FILE_ONLY=from-file\n"
                "WORKORDER_PROCESS_WINS=from-file\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"WORKORDER_PROCESS_WINS": "from-process"},
                clear=False,
            ):
                os.environ.pop("WORKORDER_ENV_FILE_ONLY", None)
                try:
                    self.assertTrue(load_environment(env_file))
                    self.assertEqual(os.environ["WORKORDER_ENV_FILE_ONLY"], "from-file")
                    self.assertEqual(os.environ["WORKORDER_PROCESS_WINS"], "from-process")
                finally:
                    os.environ.pop("WORKORDER_ENV_FILE_ONLY", None)


if __name__ == "__main__":
    unittest.main()
