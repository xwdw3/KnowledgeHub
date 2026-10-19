"""Agent 工具定义与执行。

工具设计原则：
  - 每个工具参数简单、可校验、失败返回结构化错误而不是抛异常
  - 工具返回文本（喂给模型）+ 结构化数据（用于前端展示引用）
"""
from __future__ import annotations

import ast
import logging
import operator
from typing import Any

from app_v2.core.settings import settings
from app_v2.services import retrieval_service
from app_v2.storage.meta_store import meta_store

logger = logging.getLogger(__name__)

# ---------------- 工具 Schema（OpenAI Function Calling 格式） ----------------
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "knowledge_search",
            "description": (
                "在企业知识库中检索与问题相关的文档片段。"
                "当用户询问制度、流程、规定、技术资料等企业文档内容时必须调用。"
                "可用不同的关键词多次检索以补充信息，但不要重复相同查询。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词或问题，尽量使用文档中可能出现的表述"},
                    "top_k": {"type": "integer", "description": "返回片段数量，默认 3，最大 10", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": "列出知识库中已入库的文档清单。当用户问'知识库里有什么'或需要确认某文档是否存在时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "文件名关键词，留空表示全部"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "计算数学表达式，用于差旅费用、天数、比例等数值计算。仅支持 + - * / // % ** 与括号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "数学表达式，例如 (600*3+120*3)"},
                },
                "required": ["expression"],
            },
        },
    },
]

TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


# ---------------- 安全计算器 ----------------
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}
MAX_EXPONENT = 100


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _safe_eval(node.left), _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
            raise ValueError("指数过大")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("表达式包含不支持的语法")


# ---------------- 工具执行 ----------------
def run_tool(name: str, args: dict[str, Any], perm_groups: list[str],
             spaces: list[str] | None = None, tenant_id: str = "") -> dict:
    """返回 {content, data, error}。永不抛异常，错误也交给模型处理。

    P4：租户从这里透传到检索与文档列表。工具调用是模型自主发起的，
    最容易绕过路由层校验——如果这里不给租户，模型一句
    "列出所有文档" 就能跨租户读取。
    """
    try:
        if name == "knowledge_search":
            return _tool_search(args, perm_groups, spaces, tenant_id)
        if name == "list_documents":
            return _tool_list_docs(args, tenant_id)
        if name == "calculator":
            return _tool_calc(args)
        return {"content": f"未知工具：{name}", "data": None, "error": "unknown_tool"}
    except Exception as exc:
        logger.exception("工具执行异常 name=%s", name)
        return {"content": f"工具执行失败：{exc}", "data": None, "error": str(exc)}


def _tool_search(args: dict, perm_groups: list[str],
                 spaces: list[str] | None = None, tenant_id: str = "") -> dict:
    query = str(args.get("query", "")).strip()
    if not query:
        return {"content": "检索词为空，请提供 query 参数。", "data": None, "error": "empty_query"}
    try:
        top_k = max(1, min(int(args.get("top_k", settings.final_top_k)), 10))
    except (TypeError, ValueError):
        top_k = settings.final_top_k

    result = retrieval_service.search(query, perm_groups, top_k, spaces=spaces,
                                      tenant_id=tenant_id or None)
    hits = result["hits"]
    if not hits:
        return {"content": "未检索到相关内容。", "data": {"hits": [], "query": query}, "error": None}

    lines = []
    for i, h in enumerate(hits, start=1):
        lines.append(f"[资料{i}] 来源：{h['file_name']}（第{h['seq'] + 1}段）\n{h['text']}")
    return {"content": "\n\n".join(lines), "data": {"hits": hits, "query": query}, "error": None}


def _tool_list_docs(args: dict, tenant_id: str = "") -> dict:
    keyword = str(args.get("keyword", "") or "").strip()
    docs = meta_store.list_documents(tenant_id, keyword=keyword, limit=50)
    if not docs:
        return {"content": "知识库中暂无匹配文档。", "data": {"docs": []}, "error": None}
    lines = [f"- {d['file_name']}（{d['chunk_count']} 片段，{d['char_count']} 字）" for d in docs]
    return {
        "content": f"共 {len(docs)} 个文档：\n" + "\n".join(lines),
        "data": {"docs": docs},
        "error": None,
    }


def _tool_calc(args: dict) -> dict:
    expr = str(args.get("expression", "")).strip()
    if not expr:
        return {"content": "表达式为空。", "data": None, "error": "empty_expression"}
    try:
        tree = ast.parse(expr, mode="eval")
        value = _safe_eval(tree)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return {"content": f"{expr} = {value}", "data": {"expression": expr, "value": value}, "error": None}
    except Exception as exc:
        return {"content": f"计算失败：{exc}", "data": None, "error": str(exc)}
