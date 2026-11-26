"""普通来源公平性：轮内权重封顶、余量轮末共享、防独占。"""

import unittest

from flowcontrol.engine import NORMAL, Engine


class FairnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Engine()
        self.engine.pool("g", 10, 0)  # 普通份额 10
        self.engine.open_connection("a", "ca", normal_idle_carry=1)
        self.engine.open_connection("b", "cb", normal_idle_carry=1)

    def test_first_source_cannot_monopolise_share(self) -> None:
        a = self.engine.grant_credit("a", "ca", "va", ["g"], NORMAL, 10)
        b = self.engine.grant_credit("b", "cb", "vb", ["g"], NORMAL, 10)
        self.assertLessEqual(a["per_group"]["g"]["granted"], 5)
        self.assertGreaterEqual(b["per_group"]["g"]["granted"], 5)
        self.assertEqual(
            a["per_group"]["g"]["granted"] + b["per_group"]["g"]["granted"], 10
        )

    def test_round_cap_blocks_extra_application_same_round(self) -> None:
        self.engine.grant_credit("a", "ca", "va", ["g"], NORMAL, 5)
        again = self.engine.grant_credit("a", "ca", "va2", ["g"], NORMAL, 5)
        self.assertEqual(again["per_group"]["g"]["granted"], 0)

    def test_end_round_reaps_idle_credit_and_shares_leftover(self) -> None:
        self.engine.grant_credit("a", "ca", "va", ["g"], NORMAL, 5)
        self.engine.grant_credit("b", "cb", "vb", ["g"], NORMAL, 5)
        result = self.engine.end_round()
        reaped = sum(item["reaped"] for item in result["reaped"])
        # 每来源只留 1，共回收 8
        self.assertEqual(reaped, 8)
        view = self.engine.inspect_group("g")["classes"]["normal"]
        self.assertEqual(view["unused_credit"], 2)
        # 新一轮 a 又可以申请
        fresh = self.engine.grant_credit("a", "ca", "va3", ["g"], NORMAL, 5)
        self.assertEqual(fresh["per_group"]["g"]["granted"], 5)

    def test_single_source_may_use_leftover_when_no_other_active(self) -> None:
        engine = Engine()
        engine.pool("g", 6, 0)
        engine.open_connection("solo", "c")
        result = engine.grant_credit("solo", "c", "v", ["g"], NORMAL, 6)
        self.assertEqual(result["per_group"]["g"]["granted"], 6)

    def test_weights_split_capacity(self) -> None:
        engine = Engine()
        engine.pool("g", 9, 0)
        engine.open_connection("heavy", "c1", weight=2)
        engine.open_connection("light", "c2", weight=1)
        heavy = engine.grant_credit("heavy", "c1", "vh", ["g"], NORMAL, 9)
        light = engine.grant_credit("light", "c2", "vl", ["g"], NORMAL, 9)
        self.assertEqual(heavy["per_group"]["g"]["granted"], 6)
        self.assertEqual(light["per_group"]["g"]["granted"], 3)


if __name__ == "__main__":
    unittest.main()
