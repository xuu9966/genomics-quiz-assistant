"""供单元测试注入的假数据（虚构域名 example.invalid，虚构会话标识）。

所有内容均可离线使用；任何测试都不得据此访问真实平台。
"""
from __future__ import annotations

import copy
import json
from typing import Any, Dict, Optional

# ---------------------------------------------------------------- 目录树 ----

SMALL_TREE: Dict[str, Any] = {
    "course": "虚构课程",
    "tree": {
        "id": "root",
        "name": "课程根目录",
        "children": [
            {
                "id": "node_A",
                "name": "2_测序",
                "files": ["2_测序/基因组测序.md", "2_测序/序列比对.md"],
            },
            {
                "id": "node_B",
                "name": "3_组装",
                "nodes": [
                    {
                        "id": "node_B1",
                        "name": "3_组装",
                        "files": ["3_组装/从头组装.md"],
                    }
                ],
            },
            {
                "id": "node_C",
                "name": "自主学习",
                "items": [
                    {
                        "id": "node_C1",
                        "name": "自主学习",
                        "files": ["自主学习/复习笔记.md"],
                    }
                ],
            },
        ],
    },
}

SMALL_GRAPH_HTML = (
    "<html><body><script>\n"
    "var graphData = " + json.dumps(SMALL_TREE, ensure_ascii=False) + ";\n"
    "var kpId = \"KP-0001\";\n"
    "window.selftestInfo = {\"current\": 3, \"total\": 10};\n"
    "</script></body></html>"
)

# ------------------------------------------------------------ v2 顶层大章 ----

# 8 个顶层大章权威清单（核验 §7.4）：仅在测试数据中固定，解析逻辑一律以树结构为准。
CHAPTER_NAMES = ["0_预习", "1_绪论", "2_测序", "3_组装", "4_注释", "5_数据库", "6_实验", "9_复习"]
CHAPTER_IDS = [
    "20260907180845_0001", "20260907180845_0002", "20260907180845_0005",
    "20260907180845_0008", "20260907180845_0017", "20260907180845_0030",
    "20260907180845_0034", "20260907180845_0044",
]
# 传大章 node_id 时 kpId 与大章 id 相同（核验 §7.2a）。
CHAPTER_KP_IDS = {node_id: node_id for node_id in CHAPTER_IDS}


def make_chapter_tree() -> Dict[str, Any]:
    """8 大章 tree：tree.children 即顶层全集，每章含自身代表文件与一个子文件夹节点。"""
    children = []
    for index, (name, node_id) in enumerate(zip(CHAPTER_NAMES, CHAPTER_IDS)):
        children.append({
            "id": node_id,
            "name": name,
            "files": [f"{name}\\代表文件.md", f"{name}\\补充材料.md"],
            "children": [{
                "id": f"{node_id}_sub",
                "name": f"{name}\\子目录",
                "files": [f"{name}\\子目录\\资料{index}.md"],
            }],
        })
    return {
        "course": "虚构课程",
        "tree": {"id": "root", "name": "课程根目录", "children": children},
    }


def make_chapter_graph_html() -> str:
    """由 8 大章 fixture 生成 graphData HTML（完全离线，不触网）。"""
    return (
        "<html><script>var graphData = " +
        json.dumps(make_chapter_tree(), ensure_ascii=False) + ";</script></html>"
    )


def make_tree(total_files: int = 796, as_list: bool = False) -> Dict[str, Any]:
    """生成规模可配置的课程树：4 个顶层节点 × 5 个子节点 × 每组最多 40 个 md。

    子节点数组故意混用 children / nodes / items 等字段名，用于验证递归遍历不写死字段。
    """
    names = ["genome", "assemble", "annotate", "review"]
    files = [f"2_测序/子目录{i // 40:02d}/文件{i:04d}.md" for i in range(max(0, total_files))]
    chunks = [files[i:i + 40] for i in range(0, len(files), 40)]
    mids = []
    for index, chunk in enumerate(chunks):
        field = ("children", "nodes", "items", "files_group")[index % 4]
        if field == "files_group":
            mids.append({"id": f"mid_{index}", "name": f"测序组{index}", "custom": [{"id": f"leaf_{index}", "name": f"叶子{index}", "files": chunk}]})
        else:
            mids.append({"id": f"mid_{index}", "name": f"测序组{index}", field: [{"id": f"leaf_{index}", "name": f"叶子{index}", "files": chunk}]})
    tops = []
    for top in range(4):
        group = mids[top * 5:(top + 1) * 5]
        tops.append({"id": f"top_{top}", "name": names[top], "children": group})
    tree: Any = tops if as_list else {"id": "root", "name": "课程根目录", "children": tops}
    return {"course": "虚构课程", "tree": tree}


def make_tree_html(tree: Dict[str, Any]) -> str:
    return "<html><script>var graphData = " + json.dumps(tree, ensure_ascii=False) + ";</script></html>"


# ---------------------------------------------------------------- 结果页 ----

QUESTION_TEXT = "下列哪一项属于DNA测序技术？"

RESULT_HTML = """<!DOCTYPE html><html><body>
<div class="score">最终得分：90 分</div>
<script>
window.examData = [
 {"question_id":"q1","content":"下列哪一项属于DNA测序技术？ <img src='https://example.invalid/img1.png'>",
  "options":[{"option":"Sanger测序","flag":1},{"option":"PCR扩增"}],
  "correct_answer":"Sanger测序","analysis":"这是解析文本A"},
 {"question_id":"q2","content":"测序比对的常用方法有哪些？",
  "options":[{"option":"BWA","flag":1},{"option":"Bowtie","flag":1},{"option":"Excel"}],
  "correct_answer":"BWA；Bowtie","analysis":"这是解析文本B"}
];
window.final_score = 90;
</script></body></html>"""

RESULT_HTML_NO_SCORE = """<!DOCTYPE html><html><body>
<div>评阅中，请稍后查看。</div>
<script>window.examData = [];</script></body></html>"""

CONCLUSION_DATA: Dict[str, Any] = {
    "low_rate_questions": [
        {
            "question_id": "q1",
            "qtype_name": "qt01",
            "content": QUESTION_TEXT,
            "options": [
                {"option": "Sanger测序", "flag": 1},
                {"option": "PCR扩增", "flag": 0},
            ],
            "reason": "来自评阅总结的补充解析",
        },
        {
            "question_id": "q3",
            "qtype_name": "多选题",
            "content": "基因组组装的评价指标包括？",
            "options": ["N50", "覆盖度", "学号"],
            "answer_text": "N50；覆盖度",
            "analysis": "组装质量解析",
        },
    ]
}
CONCLUSION_JSON = json.dumps(CONCLUSION_DATA, ensure_ascii=False)

# ---------------------------------------------------------------- 组卷响应 --

# ⚠️ 2026-11-21 实测修正：平台 `/功能/api/selftest/generate` 返回的 `questions`
# 是「题型 → 分桶对象」结构（与交卷 task_data 同构），桶内的 `questions` 才是数组。
# 早期夹具按「题型 → 数组」编写，与真实返回不符，导致 129 个用例全绿但真机交了空卷得 0 分。
GENERATE_RESPONSE: Dict[str, Any] = {
    "success": True,
    "session_id": "sess-1234567890abcdef",
    "redirect": "/功能/学生/学习任务/查看/sess-1234567890abcdef",
    "md_name": "基因组测序.md",
    "node_id": "node_A",
    "task_name": "测序自测",
    "duration": 0,
    "rules": ["先自测，后看解析"],
    "questions": {
        "qt01": {
            "count": 1,
            "answer_count": 1,
            "qtype_name": "单选题",
            "score": 10,
            "questions": [
                {
                    "id": "q1",
                    "content": "单选题：下列哪一项属于DNA测序技术？",
                    "options": ["Sanger测序", "PCR扩增", "电泳", "离心"],
                }
            ],
        },
        "qt02": {
            "count": 1,
            "answer_count": 1,
            "qtype_name": "多选题",
            "score": 10,
            "questions": [
                {
                    "id": "q2",
                    "content": "多选题：组装常用算法包括？",
                    "options": ["贪心算法", "De Bruijn图", "OLC", "冒泡排序"],
                }
            ],
        },
    },
}

# 旧版/假设形态（题型 → 直接数组）：仍兼容，用于回归「两种形态都能解析」。
GENERATE_RESPONSE_FLAT: Dict[str, Any] = {
    **{key: value for key, value in GENERATE_RESPONSE.items() if key != "questions"},
    "questions": {
        "qt01": [
            {
                "id": "q1",
                "content": "单选题：下列哪一项属于DNA测序技术？",
                "options": ["Sanger测序", "PCR扩增", "电泳", "离心"],
            }
        ],
        "qt02": [
            {
                "id": "q2",
                "content": "多选题：组装常用算法包括？",
                "options": ["贪心算法", "De Bruijn图", "OLC", "冒泡排序"],
            }
        ],
    },
}

# 组卷成功但 0 题（真机故障现场）：必须报 GENERATE，不得交空卷。
GENERATE_RESPONSE_EMPTY: Dict[str, Any] = {
    **{key: value for key, value in GENERATE_RESPONSE.items() if key != "questions"},
    "questions": {"qt01": {"count": 0, "answer_count": 0, "qtype_name": "单选题", "questions": []}},
}


def normalized_questions(raw: Optional[Dict[str, Any]] = None):
    """把夹具里的原始 questions 归一化为 Paper 需要的 {题型: [题目]}。"""
    import parsers  # 延迟导入，避免 fixtures 与业务模块循环依赖

    data = GENERATE_RESPONSE if raw is None else raw
    return parsers.normalize_questions(data.get("questions") or {})


# ---------------------------------------------------------------- 配置样例 --

VALID_CONFIG: Dict[str, Any] = {
    "base_url": "http://example.invalid:5000/",
    "username": "虚构用户",
    "password_env": "CQA_PASSWORD",
    "chapters": ["2_测序"],
    "limit": 10,
    "model": "deepseek-v4-flash",
    "paper": {"single_count": 5, "multi_count": 5, "score_per": 10, "duration": 0},
    "request_interval_sec": 0.0,
    "retry_times": 3,
    "retry_backoff_sec": 0.01,
    "task_timeout_sec": 180,
    "score_poll_interval_sec": 1,
    "score_poll_timeout_sec": 5,
    "web": {"host": "127.0.0.1", "port": 18765, "poll_sec": 3, "status_color": False},
    "paths": {"data_dir": "data", "log_dir": "logs", "export_dir": "exports"},
}


def config_copy() -> Dict[str, Any]:
    """返回深拷贝，避免用例之间互相污染。"""
    return copy.deepcopy(VALID_CONFIG)
