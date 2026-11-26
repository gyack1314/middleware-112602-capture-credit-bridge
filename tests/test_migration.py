"""连接迁移：switch/stopped 两阶段、未用信用转移、晚到重复投送。"""

import unittest

from flowcontrol.engine import (
    CONN_OPEN,
    CONN_STOPPED,
    CONN_SWITCHING,
    NORMAL,
    Engine,
)


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Engine()
        self.engine.pool("g", 9, 2)
        self.engine.open_connection("a", "old")

    def test_switch_keeps_pending_credit_until_outcome_known(self) -> None:
        self.engine.grant_credit("a", "old", "q", ["g"], NORMAL, 6)
        self.engine.send_fragment("a", "old", "q", "f", ["g"], 2)
        result = self.engine.switch("a", "old", "new")
        self.assertEqual(result["prev_state"], CONN_SWITCHING)
        pending = result["pending_credit"][0]
        self.assertEqual(pending["balance"], 4)
        # 结局未定前，旧信用仍保留在旧连接名下
        account = self.engine.accounts[("a", "q", "g")]
        self.assertEqual(account.connection, "old")
        self.assertTrue(account.active)

    def test_new_connection_credit_is_independent_before_stop(self) -> None:
        self.engine.grant_credit("a", "old", "q", ["g"], NORMAL, 6)
        self.engine.send_fragment("a", "old", "q", "f", ["g"], 2)
        self.engine.switch("a", "old", "new")
        # g 普通份额 = cap-reserve = 7；占用 2 + 旧未用 4，真实余量只剩 1
        fresh = self.engine.grant_credit("a", "new", "r", ["g"], NORMAL, 5)
        self.assertEqual(fresh["per_group"]["g"]["granted"], 1)

    def test_stopped_transfers_unused_credit_to_new_connection(self) -> None:
        self.engine.grant_credit("a", "old", "q", ["g"], NORMAL, 6)
        self.engine.send_fragment("a", "old", "q", "f", ["g"], 2)
        self.engine.switch("a", "old", "new")
        result = self.engine.stopped("a", "old", last=["f"])
        self.assertEqual(result["transferred"], [
            {"credit": "q", "group": "g", "class": NORMAL, "bytes": 4, "to": "new"}
        ])
        self.assertEqual(self.engine.accounts[("a", "q", "g")].connection, "new")
        self.assertEqual(self.engine.sources["a"].connections["old"], CONN_STOPPED)

    def test_stopped_can_reclaim_instead_of_transfer(self) -> None:
        self.engine.grant_credit("a", "old", "q", ["g"], NORMAL, 6)
        self.engine.switch("a", "old", "new")
        result = self.engine.stopped("a", "old", last=[], transfer=False)
        self.assertEqual(result["reclaimed"], 6)
        self.assertFalse(self.engine.accounts[("a", "q", "g")].active)
        self.assertEqual(self.engine.accounts[("a", "q", "g")].balance, 0)

    def test_unknown_last_fragment_rejected(self) -> None:
        self.engine.switch("a", "old", "new")
        result = self.engine.stopped("a", "old", last=["ghost"])
        self.assertEqual(result["reasons"], ["UNKNOWN_LAST_FRAGMENT"])
        self.assertNotEqual(self.engine.sources["a"].connections["old"], CONN_STOPPED)

    def test_late_duplicate_returns_original_state_without_new_charge(self) -> None:
        self.engine.grant_credit("a", "old", "q", ["g"], NORMAL, 6)
        self.engine.send_fragment("a", "old", "q", "f", ["g"], 2)
        self.engine.switch("a", "old", "new")
        self.engine.stopped("a", "old", last=["f"])
        balance_before = self.engine.accounts[("a", "q", "g")].balance
        late = self.engine.send_fragment("a", "old", "q", "f", ["g"], 2)
        self.assertTrue(late["duplicate"])
        self.assertEqual(late["fragment"]["status"], "admitted")
        # 新连接信用一分未动
        self.assertEqual(self.engine.accounts[("a", "q", "g")].balance, balance_before)
        self.assertEqual(self.engine.inspect_group("g")["classes"]["normal"]["occupied"], 2)

    def test_old_connection_cannot_send_new_fragment_after_stop(self) -> None:
        self.engine.grant_credit("a", "old", "q", ["g"], NORMAL, 6)
        self.engine.switch("a", "old", "new")
        self.engine.stopped("a", "old", last=[])
        result = self.engine.send_fragment("a", "old", "q", "brand-new", ["g"], 1)
        self.assertEqual(result["reasons"], ["CONNECTION_STOPPED"])

    def test_switching_connection_cannot_request_new_credit(self) -> None:
        self.engine.switch("a", "old", "new")
        result = self.engine.grant_credit("a", "old", "q", ["g"], NORMAL, 1)
        self.assertEqual(result["reasons"], ["CONNECTION_NOT_OPEN"])
        # 新连接正常
        self.assertTrue(
            self.engine.grant_credit("a", "new", "r", ["g"], NORMAL, 1)["ok"]
        )
        self.assertEqual(self.engine.sources["a"].current, "new")


if __name__ == "__main__":
    unittest.main()
