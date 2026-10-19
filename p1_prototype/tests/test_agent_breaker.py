"""Agent 熔断单元测试：用假 LLM 精确制造各类失控场景，验证熔断是否真的生效。

为什么必须测这个：
  "熔断"是防止 agent 死循环的关键机制，但正常对话下永远不会触发，
  只能用桩对象构造"模型反复调用同一工具"等异常行为来验证。
  不测的熔断就等于没有熔断。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app_v2.core.settings import settings          # noqa: E402
from app_v2.services import agent_service          # noqa: E402


def tool_call(name: str, args: str, cid: str = "c1") -> dict:
    return {"id": cid, "name": name, "arguments": args}


_ENDLESS = object()      # 哨兵：表示"脚本无限循环且每次换参数"


class FakeLLM:
    """按脚本返回预设响应，记录调用次数，用于模拟失控模型。

    script 中每个元素是一轮的工具调用列表；元素为 None 表示"该轮直接作答"。
    """

    def __init__(self, script: list, final: str = "最终回答"):
        self.script = script
        self.final = final
        self.calls = 0
        self.saw_tools = []

    def __call__(self, messages, tools=None, temperature=None):
        self.calls += 1
        self.saw_tools.append(bool(tools))
        if tools is None:                     # 收尾调用（不带工具）
            return {"content": self.final, "tool_calls": None, "finish_reason": "stop", "usage": {}}
        if self.script is _ENDLESS:
            # 无限模式：每次换新参数，专门用于验证"轮次上限"而非"重复调用"
            return {"content": "", "tool_calls": [tool_call("knowledge_search", f'{{"query":"q{self.calls}"}}')],
                    "finish_reason": "tool_calls", "usage": {}}
        step = self.script[self.calls - 1] if self.calls <= len(self.script) else None
        if step is None:                      # 脚本给出的"作答"指令，或脚本耗尽
            return {"content": self.final, "tool_calls": None, "finish_reason": "stop", "usage": {}}
        return {"content": "", "tool_calls": step,
                "finish_reason": "tool_calls", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def run_agent(question: str, fake: FakeLLM, perm=("all",)) -> dict:
    with patch.object(agent_service.llm, "is_available", return_value=True), \
         patch.object(agent_service.llm, "chat", side_effect=fake), \
         patch.object(agent_service, "run_tool", return_value={"content": "资料A", "data": {"hits": []}, "error": None}):
        return agent_service.run(question, list(perm))


class TestAgentCircuitBreaker(unittest.TestCase):

    def test_round_limit(self):
        """模型每轮都换新参数、永不作答：应在轮次上限熔断并给出收尾回答。"""
        fake = FakeLLM(_ENDLESS, final="已达轮次上限，基于已有资料回答")
        out = run_agent("测试轮次熔断", fake)
        trace = out["trace"]
        self.assertEqual(trace["break_reason"], "round_limit")
        self.assertEqual(trace["rounds"], settings.agent_max_rounds)
        self.assertEqual(out["answer"], "已达轮次上限，基于已有资料回答")
        self.assertIn(False, fake.saw_tools)      # 确认发生过"无工具收尾"    def test_repeat_call_breaker(self):
        """模型反复调用同一工具同一参数：第二次即熔断。"""
        same = '[{"query": "住宿标准"}]'[1:-1]  # 保持 JSON 稳定
        script = [
            [tool_call("knowledge_search", same, "c1")],
            [tool_call("knowledge_search", same, "c2")],
        ]
        fake = FakeLLM(script, final="重复调用已终止")
        out = run_agent("测试重复熔断", fake)
        self.assertEqual(out["trace"]["break_reason"], "repeat_call")
        self.assertLessEqual(out["trace"]["rounds"], 2)

    def test_repeat_detection_ignores_formatting(self):
        """参数字符串格式不同但语义相同，仍应识别为重复调用。"""
        script = [
            [tool_call("knowledge_search", '{"query": "abc", "top_k": 3}', "c1")],
            [tool_call("knowledge_search", '{"top_k":3,"query":"abc"}', "c2")],
        ]
        out = run_agent("测试格式差异", FakeLLM(script, final="ok"))
        self.assertEqual(out["trace"]["break_reason"], "repeat_call")

    def test_tool_call_limit(self):
        """单轮返回大量工具调用：超过总数上限即熔断。"""
        many = [tool_call("knowledge_search", f'{{"query":"q{i}"}}', f"c{i}") for i in range(10)]
        out = run_agent("测试工具总数", FakeLLM([many], final="ok"))
        self.assertEqual(out["trace"]["break_reason"], "tool_limit")

    def test_bad_arguments_do_not_abort(self):
        """非法 JSON 参数应回填错误并继续，而不是抛异常中断整个请求。"""
        script = [
            [tool_call("knowledge_search", '{"query": broken', "c1")],
            [tool_call("calculator", '{"expression":"1+1"}', "c2")],
            None,                                    # 第三轮直接作答
        ]
        out = run_agent("测试坏参数", FakeLLM(script, final="已处理"))
        self.assertEqual(out["answer"], "已处理")
        errors = [c for c in out["trace"]["tool_calls"] if not c["ok"]]
        self.assertTrue(errors, "应记录一次参数解析失败")
        self.assertEqual(out["trace"]["break_reason"], "")

    def test_normal_path_no_break(self):
        """正常一次检索后作答：不应误触发熔断。"""
        script = [[tool_call("knowledge_search", '{"query":"住宿"}', "c1")], None]
        fake = FakeLLM(script, final="上海住宿上限 600 元/晚")
        out = run_agent("住宿标准", fake)
        self.assertEqual(out["trace"]["break_reason"], "")
        self.assertEqual(out["trace"]["rounds"], 2)
        self.assertEqual(out["answer"], "上海住宿上限 600 元/晚")

    def test_llm_error_fallback(self):
        """LLM 全程不可用：应返回兜底文案而不是抛 500。"""
        def boom(messages, tools=None, temperature=None):
            from app_v2.core.response import UpstreamError
            raise UpstreamError("模拟上游故障")

        with patch.object(agent_service.llm, "is_available", return_value=True), \
             patch.object(agent_service.llm, "chat", side_effect=boom):
            out = agent_service.run("任意问题", ["all"])
        self.assertEqual(out["trace"]["break_reason"], "llm_error")
        self.assertTrue(out["answer"])
        self.assertFalse(out["answer"].startswith("Traceback"))


class TestCalculator(unittest.TestCase):

    def test_safe_eval(self):
        from app_v2.services.agent_tools import run_tool
        self.assertEqual(run_tool("calculator", {"expression": "(600*2+120*3)"}, ["all"])["data"]["value"], 1560)
        self.assertEqual(run_tool("calculator", {"expression": "2**10"}, ["all"])["data"]["value"], 1024)

    def test_reject_dangerous(self):
        from app_v2.services.agent_tools import run_tool
        for expr in ["__import__('os').system('echo x')", "open('/etc/passwd')", "9**999"]:
            res = run_tool("calculator", {"expression": expr}, ["all"])
            self.assertIsNotNone(res["error"], f"应拒绝执行：{expr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
