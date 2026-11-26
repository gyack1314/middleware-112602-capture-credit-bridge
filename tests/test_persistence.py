"""持久化：WAL + 快照，restart 后保持已授信用与实际接受的差异。"""

import json
import tempfile
import unittest
from pathlib import Path

from flowcontrol.journal import Journal


class PersistenceTests(unittest.TestCase):
    def test_restart_preserves_credit_and_occupancy_difference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            journal.apply("pool", {"group": "g", "cap": 9, "reserve": 2})
            journal.apply("open", {"source": "a", "connection": "old"})
            journal.apply("credit", {"source": "a", "connection": "old",
                                     "credit": "q", "groups": ["g"],
                                     "class": "normal", "bytes": 6})
            journal.apply("fragment", {"source": "a", "connection": "old",
                                       "credit": "q", "id": "f",
                                       "groups": ["g"], "bytes": 2})
            journal.apply("switch", {"source": "a", "prev": "old", "next": "new"})
            before = journal.engine.to_snapshot()
            journal.restart()
            after = journal.engine.to_snapshot()
            self.assertEqual(
                json.dumps(before, sort_keys=True, ensure_ascii=False),
                json.dumps(after, sort_keys=True, ensure_ascii=False),
            )
            # 重启后继续受理：g 普通份额 7，占用 2、旧未用 4，真实余量只剩 1
            fresh = journal.apply("credit", {"source": "a", "connection": "new",
                                             "credit": "r", "groups": ["g"],
                                             "class": "normal", "bytes": 5})
            self.assertEqual(fresh["per_group"]["g"]["granted"], 1)
            journal.close()

            # 全新进程视角：仅靠目录里的日志与快照恢复
            reborn = Journal(directory)
            view = reborn.engine.inspect_source("a")
            self.assertEqual(view["connections"]["old"], "switching")
            # 旧未用 4（q）+ 新授予 1（r）= 5
            self.assertEqual(view["can_send_bytes"]["g"]["normal"], 5)
            reborn.close()

    def test_snapshot_then_journal_tail_replays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory, snapshot_every=3)
            for i in range(7):
                journal.apply("pool", {"group": f"g{i}", "cap": 4, "reserve": 1})
            self.assertTrue((Path(directory) / "snapshot.json").exists())
            journal.close()
            reborn = Journal(directory)
            self.assertEqual(len(reborn.engine.groups), 7)
            reborn.close()

    def test_scenario_messages_are_journaled_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            journal.apply("pool", {"group": "g", "cap": 5, "reserve": 1})
            journal.close()
            lines = (Path(directory) / "journal.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            event = json.loads(lines[0])
            self.assertEqual(event["kind"], "pool")
            self.assertTrue(event["result"]["ok"])


if __name__ == "__main__":
    unittest.main()
