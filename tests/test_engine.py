"""引擎核心口径测试：真实容量、双组占用、回执隔离与缩容欠额。"""

import unittest

from flowcontrol.engine import ESSENTIAL, NORMAL, Engine


class PoolAndCreditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Engine(strict=True)
        self.engine.pool("g1", 12, 3)
        self.engine.pool("g2", 9, 2)
        self.engine.open_connection("a", "c1")

    def test_credit_never_exceeds_real_capacity(self) -> None:
        result = self.engine.grant_credit("a", "c1", "v", ["g1"], NORMAL, 20)
        # 普通份额 = cap - reserve = 9
        self.assertEqual(result["per_group"]["g1"]["granted"], 9)
        view = self.engine.inspect_group("g1")
        self.assertEqual(view["classes"]["normal"]["available"], 0)

    def test_essential_uses_reserved_share_only(self) -> None:
        result = self.engine.grant_credit("a", "c1", "p", ["g1"], ESSENTIAL, 10)
        self.assertEqual(result["per_group"]["g1"]["granted"], 3)
        # 必达与普通份额互不占用
        normal = self.engine.grant_credit("a", "c1", "v", ["g1"], NORMAL, 9)
        self.assertEqual(normal["per_group"]["g1"]["granted"], 9)

    def test_down_group_rejects_credit_and_fragment(self) -> None:
        self.engine.grant_credit("a", "c1", "p", ["g1"], ESSENTIAL, 2)
        self.engine.group_set_online("g1", False)
        blocked = self.engine.grant_credit("a", "c1", "p2", ["g1"], ESSENTIAL, 1)
        self.assertEqual(blocked["reasons"], ["GROUP_DOWN"])
        send = self.engine.send_fragment("a", "c1", "p", "f", ["g1"], 1)
        self.assertEqual(send["reasons"], ["GROUP_DOWN"])
        # 已承诺的信用仍然保留
        self.assertEqual(self.engine.inspect_group("g1")["classes"]["essential"]["unused_credit"], 2)
        self.engine.group_set_online("g1", True)
        self.assertTrue(self.engine.send_fragment("a", "c1", "p", "f", ["g1"], 1)["ok"])

    def test_fragment_occupies_every_required_group_in_full(self) -> None:
        self.engine.grant_credit("a", "c1", "v", ["g1", "g2"], NORMAL, 5)
        ok = self.engine.send_fragment("a", "c1", "v", "f1", ["g1", "g2"], 5)
        self.assertTrue(ok["ok"])
        self.assertEqual(self.engine.inspect_group("g1")["classes"]["normal"]["occupied"], 5)
        self.assertEqual(self.engine.inspect_group("g2")["classes"]["normal"]["occupied"], 5)

    def test_one_group_short_rejects_whole_fragment_without_side_effect(self) -> None:
        # v 在 g1、g2 各拿 5；先在 g1 受理一个 3 字节片段，使 v 在 g1 只剩 2
        self.engine.grant_credit("a", "c1", "v", ["g1", "g2"], NORMAL, 5)
        self.engine.send_fragment("a", "c1", "v", "f0", ["g1", "g2"], 3)
        rejected = self.engine.send_fragment("a", "c1", "v", "fx", ["g1", "g2"], 5)
        self.assertEqual(rejected["reasons"], ["INSUFFICIENT_CREDIT"])
        # 整段拒绝：g2 也不产生新占用，仍只有 f0 的 3
        self.assertEqual(self.engine.inspect_group("g2")["classes"]["normal"]["occupied"], 3)
        self.assertEqual(self.engine.inspect_group("g1")["classes"]["normal"]["occupied"], 3)
        # 拒绝记录可查，且给出每组可接字节
        self.assertEqual(rejected["detail"]["admissible"]["g1"], 2)
        self.assertEqual(rejected["detail"]["admissible"]["g2"], 2)

    def test_completion_returns_only_that_group(self) -> None:
        self.engine.grant_credit("a", "c1", "v", ["g1", "g2"], NORMAL, 5)
        self.engine.send_fragment("a", "c1", "v", "f1", ["g1", "g2"], 5)
        self.engine.complete("g1", "a", "f1")
        g2 = self.engine.inspect_group("g2")["classes"]["normal"]
        self.assertEqual(g2["occupied"], 5)
        self.assertEqual(g2["unused_credit"], 0)
        g1 = self.engine.inspect_group("g1")["classes"]["normal"]
        self.assertEqual(g1["occupied"], 0)
        self.assertEqual(g1["unused_credit"], 5)

    def test_duplicate_completion_is_idempotent(self) -> None:
        self.engine.grant_credit("a", "c1", "v", ["g1"], NORMAL, 3)
        self.engine.send_fragment("a", "c1", "v", "f", ["g1"], 3)
        first = self.engine.complete("g1", "a", "f")
        second = self.engine.complete("g1", "a", "f")
        self.assertEqual(first["returned"], 3)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["returned"], 0)

    def test_shrink_creates_debt_and_blocks_new_admissions(self) -> None:
        self.engine.grant_credit("a", "c1", "v", ["g2"], NORMAL, 7)
        self.engine.send_fragment("a", "c1", "v", "f", ["g2"], 5)
        result = self.engine.pool("g2", 3, 1)  # 新普通份额 2，占用 5
        self.assertEqual(result["debt"]["normal"], 3)
        blocked = self.engine.grant_credit("a", "c1", "w", ["g2"], NORMAL, 1)
        self.assertEqual(blocked["reasons"], ["GROUP_DEBT"])
        # 必达是独立链：普通欠额不影响必达受理
        essential = self.engine.grant_credit("a", "c1", "p", ["g2"], ESSENTIAL, 1)
        self.assertTrue(essential["ok"])

    def test_old_receipt_cannot_inflate_new_configuration(self) -> None:
        self.engine.grant_credit("a", "c1", "v", ["g2"], NORMAL, 7)
        self.engine.send_fragment("a", "c1", "v", "f", ["g2"], 5)
        self.engine.pool("g2", 3, 1)  # 普通份额降到 2，欠 3
        done = self.engine.complete("g2", "a", "f")
        # 3 字节先还欠额，剩 2 字节受新份额上限约束，没有超额信用
        self.assertEqual(done["debt_cleared"], 3)
        self.assertEqual(done["returned"], 2)
        self.assertEqual(done["expired"], 0)
        view = self.engine.inspect_group("g2")["classes"]["normal"]
        self.assertEqual(view["debt"], 0)
        self.assertLessEqual(view["occupied"] + view["unused_credit"], view["share"])

    def test_shrink_expires_unused_credit_beyond_new_share(self) -> None:
        self.engine.grant_credit("a", "c1", "v", ["g2"], NORMAL, 7)
        result = self.engine.pool("g2", 3, 1)  # 普通份额 2，未用 7
        self.assertEqual(result["expired"]["normal"], 5)
        view = self.engine.inspect_group("g2")["classes"]["normal"]
        self.assertEqual(view["unused_credit"], 2)

    def test_unknown_credit_and_duplicate_fragment_id(self) -> None:
        bad = self.engine.send_fragment("a", "c1", "nope", "f", ["g1"], 1)
        self.assertEqual(bad["reasons"], ["UNKNOWN_CREDIT"])
        self.engine.grant_credit("a", "c1", "v", ["g1"], NORMAL, 2)
        self.assertTrue(self.engine.send_fragment("a", "c1", "v", "f", ["g1"], 2)["ok"])
        dup = self.engine.send_fragment("a", "c1", "v", "f", ["g1"], 2)
        self.assertTrue(dup["duplicate"])


if __name__ == "__main__":
    unittest.main()
