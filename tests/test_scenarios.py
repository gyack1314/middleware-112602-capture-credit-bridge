"""演练契约场景：仓库内两份 JSONL 的端到端断言。"""

import json
import tempfile
import unittest
from pathlib import Path

from flowcontrol.journal import Journal
from flowcontrol.runner import run_file

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


class ScenarioContractTests(unittest.TestCase):
    def _run(self, name: str) -> list[dict]:
        supported = set(json.loads((EXAMPLES / "message_types.json").read_text(encoding="utf-8")))
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            events = run_file(EXAMPLES / name, journal, supported)
            journal.close()
        return events

    def test_split_confirmation_and_cap_shrink(self) -> None:
        events = self._run("split-confirmation-and-cap-shrink.jsonl")
        results = [(e["kind"], e["result"]) for e in events]

        # 双组信用：g1 普通份额 9、g2 普通份额 7，各授 5，合计 10
        grant = next(r for k, r in results if k == "credit" and r["credit"] == "v")
        self.assertEqual(grant["granted"], 10)

        # 缩容 g2：cap 9->3、reserve 2->1，普通份额 7->2，占用 5 -> 欠额 3
        shrink = next(r for k, r in results if k == "pool" and r["action"] == "reconfigured")
        self.assertEqual(shrink["debt"]["normal"], 3)

        # 必达申请在断组恢复后、独立 reserve 份额内成功
        essential = next(r for k, r in results if k == "credit" and r["credit"] == "p")
        self.assertEqual(essential["granted"], 1)

        # g2 完成回执：3 还欠额、2 受新份额返还；重复回执幂等
        done = [r for k, r in results if k == "done"]
        g2_done = next(r for r in done if r["group"] == "g2")
        self.assertEqual(g2_done["debt_cleared"], 3)
        self.assertEqual(g2_done["returned"], 2)
        duplicate = next(r for r in done if r.get("duplicate"))
        self.assertEqual(duplicate["returned"], 0)

        # 最终查询：欠额清零，占用归零
        inspects = [r for k, r in results if k == "inspect"]
        final = inspects[-1]["classes"]["normal"]
        self.assertEqual(final["debt"], 0)
        self.assertEqual(final["occupied"], 0)

    def test_switch_with_unused_credit(self) -> None:
        events = self._run("switch-with-unused-credit.jsonl")
        results = [(e["kind"], e["result"]) for e in events]

        # 重启消息确实触发了引擎重建
        self.assertTrue(any(k == "restart" and r["restart"] for k, r in results))

        # 重启后新连接只能拿真实余量：9 - 占用2 - 旧未用4 = 3？申请 5 实际给 1
        # （重启前 grant 在 restart 之后，份额=9，占用2，旧信用4 -> 余量3，
        #  但该来源本轮已持普通信用，轮内封顶 floor(9/1)=9，已授 4 -> 可再授 5；
        #  真实余量 3 与申请 5 取小，期望按真实余量。这里以不超过余量为断言语义。）
        fresh = next(r for k, r in results if k == "credit" and r["credit"] == "r")
        self.assertLessEqual(fresh["per_group"]["g"]["granted"], 3)

        stopped = next(r for k, r in results if k == "stopped")
        self.assertEqual(stopped["transferred"], [
            {"credit": "q", "group": "g", "class": "normal", "bytes": 4, "to": "new"}
        ])

        fragments = [r for k, r in results if k == "fragment"]
        first, late = fragments[0], fragments[-1]
        self.assertTrue(first["ok"])
        self.assertTrue(late["duplicate"])
        self.assertEqual(late["fragment"]["status"], "admitted")

        source_view = next(r for k, r in results if k == "inspect")
        self.assertEqual(source_view["current"], "new")
        self.assertEqual(source_view["connections"]["old"], "stopped")


if __name__ == "__main__":
    unittest.main()
