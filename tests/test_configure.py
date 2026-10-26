import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pitwall.configure import load_key, save_key


class KeyConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'private' / 'openai.json'
        self.fake = 'sk-example-only-not-a-real-key-123456789'

    @patch.dict(os.environ, {}, clear=True)
    def test_private_key_roundtrip_and_permissions(self):
        save_key(self.fake, self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(load_key(self.path), self.fake)

    def test_invalid_key_does_not_create_or_overwrite_a_file(self):
        for key in ('', 'not-a-key', 'sk-short', 'sk-example key with spaces'):
            with self.assertRaises(ValueError):
                save_key(key, self.path)
        self.assertFalse(self.path.exists())
        save_key(self.fake, self.path)
        with self.assertRaises(ValueError):
            save_key('bad', self.path)
        self.assertEqual(json.loads(self.path.read_text())['api_key'], self.fake)

    @patch.dict(os.environ, {'OPENAI_API_KEY': 'environment-key'}, clear=True)
    def test_environment_has_priority(self):
        save_key(self.fake, self.path)
        self.assertEqual(load_key(self.path), 'environment-key')

    @patch.dict(os.environ, {}, clear=True)
    def test_exact_repeated_pastes_store_only_one_copy(self):
        for copies in (2, 3, 4):
            save_key(self.fake * copies, self.path)
            self.assertEqual(load_key(self.path), self.fake)

    @patch.dict(os.environ, {}, clear=True)
    def test_insecure_corrupt_and_missing_files_are_ignored(self):
        self.assertEqual(load_key(self.path), '')
        save_key(self.fake, self.path)
        self.path.chmod(0o644)
        self.assertEqual(load_key(self.path), '')
        self.path.chmod(0o600)
        self.path.write_text('not json')
        self.assertEqual(load_key(self.path), '')


if __name__ == '__main__':
    unittest.main()
