"""The installer's .env handling, on a temporary file: never the real one."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dotenv import dotenv_values

import install


class EnvFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = Path(self.dir.name) / ".env"
        patcher = mock.patch.object(install, "ENV", self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_value_survives_a_rewrite(self):
        values = {"LIVEKIT_URL": "wss://x.livekit.cloud", "GROQ_API_KEY": "gsk_abc", "OPIK_WORKSPACE": "my space",
                  "SIP_AUTH_PASSWORD": "p#ss'word", "LLM_PAID_FALLBACK": "off"}
        install.write_env(values)
        self.assertEqual({k: v for k, v in dotenv_values(self.env).items() if v}, values)

    def test_readable_only_by_the_owner(self):
        install.write_env({"GROQ_API_KEY": "gsk_abc"})
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)

    def test_follows_the_example_file_order(self):
        install.write_env({"DEEPGRAM_API_KEY": "d", "LIVEKIT_URL": "wss://x"})
        keys = [line.split("=")[0] for line in self.env.read_text().splitlines() if "=" in line and not line.startswith("#")]
        self.assertLess(keys.index("LIVEKIT_URL"), keys.index("DEEPGRAM_API_KEY"))

    def test_trunk_needs_all_four_sip_values(self):
        values = {"SIP_TRUNK_ADDRESS": "x.pstn.twilio.com", "SIP_CALLER_ID": "+14155550100"}
        with mock.patch.object(install, "_create_trunk") as create:
            self.assertFalse(install.make_trunk(values))
        create.assert_not_called()
        self.assertNotIn("SIP_OUTBOUND_TRUNK_ID", values)

    def test_masking_shows_only_the_ends(self):
        self.assertEqual(install.masked("gsk_1234567890abcdef"), "gsk_…ef")
        self.assertEqual(install.masked(""), "not set")


if __name__ == "__main__":
    unittest.main()
