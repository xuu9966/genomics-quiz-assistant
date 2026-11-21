"""utils.py 纯工具用例：唯一键、归一化、脱敏、退避、原子写入。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

import utils


class TestUniqueKey(unittest.TestCase):
    def test_normalize_md_path(self) -> None:
        self.assertEqual(
            utils.normalize_md_path("  2_测序\\\\子目录//基因组.md  "),
            "2_测序/子目录/基因组.md",
        )

    def test_same_path_different_node_not_collide(self) -> None:
        a = utils.make_unique_key(" node1 ", "2_测序\\基因组.md")
        b = utils.make_unique_key("node2", "2_测序/基因组.md")
        self.assertNotEqual(a, b)
        self.assertEqual(a, "node1::2_测序/基因组.md")

    def test_same_node_same_path_normalized_equal(self) -> None:
        self.assertEqual(
            utils.make_unique_key("node1", "2_测序\\基因组.md"),
            utils.make_unique_key("node1", "2_测序//基因组.md"),
        )

    def test_chapter_from_path_fallback(self) -> None:
        self.assertEqual(utils.chapter_from_path("2_测序/a.md"), "2_测序")
        self.assertEqual(utils.chapter_from_path("a.md", "自主节点"), "自主节点")


class TestDeDupAndBackoff(unittest.TestCase):
    def test_md5_is_stable_and_content_sensitive(self) -> None:
        self.assertEqual(utils.md5_text("题干"), utils.md5_text("题干"))
        self.assertNotEqual(utils.md5_text("题干"), utils.md5_text("题干 "))

    def test_backoff_exponential_and_capped(self) -> None:
        self.assertEqual(utils.backoff_delay(1, 2.0), 2.0)
        self.assertEqual(utils.backoff_delay(2, 2.0), 4.0)
        self.assertEqual(utils.backoff_delay(3, 2.0), 8.0)
        self.assertEqual(utils.backoff_delay(10, 2.0), 60.0)
        self.assertEqual(utils.backoff_delay(0, 2.0), 2.0)


class TestMask(unittest.TestCase):
    def test_credential_keys_masked(self) -> None:
        raw = (
            '{"password":"s3cr3t-value","session":"cookie-value",'
            '"csrf_token":"csrf-value","api_key":"sk-abcdef"}'
        )
        safe = utils.mask(raw)
        for secret in ("s3cr3t-value", "cookie-value", "csrf-value", "sk-abcdef"):
            self.assertNotIn(secret, safe)
        self.assertIn("***", safe)

    def test_plain_text_assignments_masked(self) -> None:
        safe = utils.mask("password=secret123; csrf_token=token123; api_key=key123")
        self.assertNotIn("secret123", safe)
        self.assertNotIn("token123", safe)
        self.assertNotIn("key123", safe)

    def test_session_id_keeps_first_eight_only(self) -> None:
        safe = utils.mask("session_id=abcdefgh1234567890")
        self.assertNotIn("abcdefgh1234567890", safe)
        self.assertIn("abcdefgh…", safe)

    def test_or_none_is_safe(self) -> None:
        self.assertEqual(utils.mask(None), "")


class TestAtomicJson(unittest.TestCase):
    def test_write_read_roundtrip_and_no_temp_left(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "nested", "state.json")
            payload = {"计划": [{"key": "值"}], "n": 3}
            utils.atomic_write_json(path, payload)
            self.assertEqual(utils.read_json(path, None), payload)
            self.assertEqual(sorted(os.listdir(os.path.dirname(path))), ["state.json"])
            with open(path, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), payload)

    def test_read_missing_returns_default(self) -> None:
        self.assertEqual(utils.read_json("不存在的路径.json", {"default": True}), {"default": True})


if __name__ == "__main__":
    unittest.main()
