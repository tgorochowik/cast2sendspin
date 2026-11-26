import os
import unittest
from pathlib import Path
from unittest.mock import patch

from ma import MA


class DeploymentTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_file_is_configurable_without_exposing_its_contents(self):
        # Never open the real .ma_token in tests.
        with patch.dict(os.environ, {"MA_TOKEN_FILE": "/run/secrets/ma_token"}):
            with patch.object(Path, "read_text", autospec=True, return_value=" synthetic \n") as read:
                with patch("ma.MusicAssistantClient"):
                    client = MA(None, "http://ma:8095")
        self.assertEqual(read.call_args.args[0], Path("/run/secrets/ma_token"))
        self.assertEqual(client._token, "synthetic")

    async def test_native_token_path_remains_compatible(self):
        with patch.dict(os.environ):
            os.environ.pop("MA_TOKEN_FILE", None)
            with patch.object(Path, "read_text", autospec=True, return_value="synthetic") as read:
                with patch("ma.MusicAssistantClient"):
                    MA(None, "http://ma:8095")
        self.assertEqual(read.call_args.args[0], Path(".ma_token"))


if __name__ == "__main__":
    unittest.main()
