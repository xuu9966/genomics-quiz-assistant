"""组卷接口解析用例（假 transport，绝不触网）。

来源：2026-11-21 真机跑出「三章全 0 分」事故——平台 `/功能/api/selftest/generate`
返回的 `questions` 是「题型 → 分桶对象」，而旧实现按「题型 → 数组」解析，
于是每桶都被判成空列表，最终交了空卷：状态 success、得分 0。
本文件的用例锁死真实返回形态，防止再次回归。
"""
from __future__ import annotations

import json
import unittest

from fixtures import GENERATE_RESPONSE, GENERATE_RESPONSE_EMPTY, GENERATE_RESPONSE_FLAT, config_copy
from models import AppConfig
from platform_client import GenerateError, PlatformClient, Response


class RecordingTransport:
    """记录请求并按预设 JSON 返回；不发起任何网络请求。"""

    def __init__(self, payload: dict):
        self.payload = json.dumps(payload, ensure_ascii=False)
        self.calls = []

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append({"method": method, "url": url,
                           "body": json.loads(data.decode("utf-8")) if data else None})
        return Response(status_code=200, text=self.payload, url=url)


def make_client(payload: dict):
    transport = RecordingTransport(payload)
    config = AppConfig._from_dict(config_copy())
    config.password = "占位口令"
    client = PlatformClient(config, transport=transport)
    return client, transport


class GeneratePaperParsingTest(unittest.TestCase):
    def test_bucket_object_form_is_parsed(self) -> None:
        """真实形态：题型 → {count, answer_count, qtype_name, questions:[...]}。"""
        client, _ = make_client(GENERATE_RESPONSE)
        paper = client.generate_paper("node_A", "2_测序/基因组测序.md", None)
        self.assertEqual(len(paper.questions["qt01"]), 1)
        self.assertEqual(len(paper.questions["qt02"]), 1)
        self.assertEqual(len(paper.all_questions()), 2)
        first = paper.questions["qt01"][0]
        self.assertEqual(first["id"], "q1")
        self.assertIn("Sanger测序", first["options"])

    def test_flat_list_form_still_supported(self) -> None:
        """兼容形态：题型 → 直接数组（老夹具/其他部署）。"""
        client, _ = make_client(GENERATE_RESPONSE_FLAT)
        paper = client.generate_paper("node_A", "2_测序/基因组测序.md", None)
        self.assertEqual(len(paper.all_questions()), 2)

    def test_empty_paper_raises_generate_error(self) -> None:
        """组卷成功但 0 题：必须报错中止，绝不交空卷刷 0 分。"""
        client, transport = make_client(GENERATE_RESPONSE_EMPTY)
        with self.assertRaises(GenerateError) as ctx:
            client.generate_paper("node_A", "2_测序/基因组测序.md", None)
        self.assertIn("0 道题", str(ctx.exception))
        self.assertEqual(ctx.exception.error_type, "GENERATE")
        # 只有一次组卷请求，没有额外动作
        self.assertEqual(len(transport.calls), 1)

    def test_generate_request_body_contract(self) -> None:
        """请求体仍是 node_id / md_path / config / duration 四项。"""
        client, transport = make_client(GENERATE_RESPONSE)
        client.generate_paper("node_A", "2_测序\\基因组测序.md", None)
        body = transport.calls[0]["body"]
        self.assertEqual(transport.calls[0]["method"], "POST")
        self.assertEqual(body["node_id"], "node_A")
        self.assertEqual(body["md_path"], "2_测序\\基因组测序.md")
        self.assertEqual([item["qtype_id"] for item in body["config"]], ["qt01", "qt02"])
        self.assertIn("duration", body)

    def test_non_dict_questions_is_empty_and_raises(self) -> None:
        """畸形返回（questions 不是对象）同样要报错，而不是静默成功。"""
        client, _ = make_client({"success": True, "session_id": "sess-x", "questions": []})
        with self.assertRaises(GenerateError):
            client.generate_paper("node_A", "a.md", None)


if __name__ == "__main__":
    unittest.main()
