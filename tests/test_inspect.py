"""值班员三维查询：片段、来源、处理组的可送量、占用与阻塞原因。"""

import unittest

from flowcontrol.engine import ESSENTIAL, NORMAL, Engine


class InspectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Engine()
        self.engine.pool("g1", 10, 2)
        self.engine.pool("g2", 6, 1)
        self.engine.open_connection("a", "c1")

    def test_group_view_explains_why_it_cannot_continue(self) -> None:
        self.engine.grant_credit("a", "c1", "v", ["g1"], NORMAL, 8)
        self.engine.send_fragment("a", "c1", "v", "f", ["g1"], 3)
        view = self.engine.inspect_group("g1")
        normal = view["classes"]["normal"]
        self.assertEqual(normal["share"], 8)
        self.assertEqual(normal["occupied"], 3)
        self.assertEqual(normal["unused_credit"], 5)
        self.assertEqual(normal["available"], 0)
        self.assertEqual(normal["occupied_detail"], {"a": 3})

        self.engine.group_set_online("g1", False)
        self.assertIn("GROUP_DOWN", self.engine.inspect_group("g1")["blockers"])
        self.engine.group_set_online("g1", True)

        self.engine.pool("g1", 4, 1)  # 普通份额 3，占用 3，无欠额；必达份额 1
        self.assertEqual(self.engine.inspect_group("g1")["classes"]["normal"]["debt"], 0)

    def test_source_view_lists_credits_and_can_send(self) -> None:
        self.engine.grant_credit("a", "c1", "v", ["g1", "g2"], NORMAL, 4)
        self.engine.send_fragment("a", "c1", "v", "f1", ["g1", "g2"], 4)
        view = self.engine.inspect_source("a")
        self.assertEqual(view["current"], "c1")
        # 两组各扣 4，已无余额：零余额账户不计入 can_send
        self.assertEqual(view["can_send_bytes"], {})
        self.engine.complete("g1", "a", "f1")
        view = self.engine.inspect_source("a")
        self.assertEqual(view["can_send_bytes"]["g1"][NORMAL], 4)
        self.assertNotIn("g2", view["can_send_bytes"])

    def test_fragment_view_shows_exact_bytes_per_group(self) -> None:
        self.engine.grant_credit("a", "c1", "v", ["g1", "g2"], NORMAL, 2)
        self.engine.send_fragment("a", "c1", "v", "f", ["g1", "g2"], 2)
        view = self.engine.inspect_fragment("a", "f")
        per_group = {item["group"]: item for item in view["per_group"]}
        self.assertEqual(per_group["g1"]["occupied_bytes"], 2)
        self.assertEqual(per_group["g2"]["occupied_bytes"], 2)
        self.assertFalse(per_group["g1"]["done"])
        self.engine.complete("g1", "a", "f")
        view = self.engine.inspect_fragment("a", "f")
        g1 = next(item for item in view["per_group"] if item["group"] == "g1")
        g2 = next(item for item in view["per_group"] if item["group"] == "g2")
        self.assertTrue(g1["done"])
        self.assertFalse(g2["done"])
        self.assertEqual(view["fragment"]["status"], "admitted")
        self.engine.complete("g2", "a", "f")
        self.assertEqual(
            self.engine.inspect_fragment("a", "f")["fragment"]["status"], "completed"
        )

    def test_restricted_class_does_not_block_independent_chain(self) -> None:
        # g1 普通份额制造欠额
        self.engine.grant_credit("a", "c1", "v", ["g1"], NORMAL, 8)
        self.engine.send_fragment("a", "c1", "v", "f", ["g1"], 5)
        self.engine.pool("g1", 4, 2)  # 普通份额 2，欠 3
        blocked = self.engine.grant_credit("a", "c1", "w", ["g1"], NORMAL, 1)
        self.assertEqual(blocked["reasons"], ["GROUP_DEBT"])
        # 必达链独立，g1 reserve=2 仍可授予
        ok = self.engine.grant_credit("a", "c1", "p", ["g1"], ESSENTIAL, 2)
        self.assertTrue(ok["ok"])
        # 另一处理组完全不受影响
        ok2 = self.engine.grant_credit("a", "c1", "v2", ["g2"], NORMAL, 1)
        self.assertTrue(ok2["ok"])


if __name__ == "__main__":
    unittest.main()
