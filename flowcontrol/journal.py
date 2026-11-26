"""事件日志与快照：进程重启后保持已授信用与实际接受的差异。

状态目录布局：
    journal.jsonl   追加写的命令事件（含结果），重启时按序重放
    snapshot.json   周期快照，重放时从快照继续追后续日志
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .engine import Engine

SNAPSHOT_EVERY = 50  # 每 50 条命令落一次快照


class Journal:
    def __init__(self, directory: str | Path, snapshot_every: int = SNAPSHOT_EVERY) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.directory / "journal.jsonl"
        self.snapshot_path = self.directory / "snapshot.json"
        self.snapshot_every = snapshot_every
        self.engine = self._load()
        self._handle = self.journal_path.open("a", encoding="utf-8")

    # ------------------------------------------------------------------ #
    def _load(self) -> Engine:
        snapshot: dict[str, Any] | None = None
        seq_after_snapshot = 0
        if self.snapshot_path.exists():
            snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            seq_after_snapshot = snapshot["command_seq"]
        engine = Engine.from_snapshot(snapshot) if snapshot else Engine()
        if self.journal_path.exists():
            with self.journal_path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    if event["seq"] <= seq_after_snapshot:
                        continue
                    self._replay(engine, event)
        return engine

    @staticmethod
    def _replay(engine: Engine, event: dict[str, Any]) -> None:
        kind = event["kind"]
        rec = event["record"]
        handler = getattr(engine, COMMAND_METHODS[kind])
        if kind == "pool":
            handler(rec["group"], rec["cap"], rec["reserve"],
                    online=rec.get("online", True))
        elif kind == "open":
            handler(rec["source"], rec["connection"],
                    weight=rec.get("weight", 1),
                    normal_idle_carry=rec.get("normal_idle_carry"))
        elif kind == "credit":
            handler(rec["source"], rec["connection"], rec["credit"],
                    rec["groups"], rec["class"], rec["bytes"])
        elif kind == "fragment":
            handler(rec["source"], rec["connection"], rec["credit"],
                    rec["id"], rec["groups"], rec["bytes"])
        elif kind == "done":
            handler(rec["group"], rec["source"], rec["id"])
        elif kind == "switch":
            handler(rec["source"], rec["prev"], rec["next"])
        elif kind == "stopped":
            handler(rec["source"], rec["connection"],
                    last=rec.get("last", []), transfer=rec.get("transfer", True))
        elif kind == "group-status":
            handler(rec["group"], rec["online"])
        elif kind == "end-round":
            handler()

    # ------------------------------------------------------------------ #
    def apply(self, kind: str, record: dict[str, Any]) -> dict[str, Any]:
        """执行命令并追加日志；restart 命令只做日志标注，不改变状态。"""
        if kind == "restart":
            event = {"seq": self.engine.command_seq, "kind": kind,
                     "record": record, "result": {"ok": True, "restart": True}}
            self._handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._handle.flush()
            return event["result"]
        if kind == "inspect":
            return self._inspect(record)
        result = self._replay_one(kind, record)
        event = {"seq": self.engine.command_seq, "kind": kind,
                 "record": record, "result": result}
        self._handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._handle.flush()
        if self.engine.command_seq % self.snapshot_every == 0:
            self.snapshot()
        return result

    def _replay_one(self, kind: str, record: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self.engine, COMMAND_METHODS[kind])
        if kind == "pool":
            return handler(record["group"], record["cap"], record["reserve"],
                           online=record.get("online", True))
        if kind == "open":
            return handler(record["source"], record["connection"],
                           weight=record.get("weight", 1),
                           normal_idle_carry=record.get("normal_idle_carry"))
        if kind == "credit":
            return handler(record["source"], record["connection"], record["credit"],
                           record["groups"], record["class"], record["bytes"])
        if kind == "fragment":
            return handler(record["source"], record["connection"], record["credit"],
                           record["id"], record["groups"], record["bytes"])
        if kind == "done":
            return handler(record["group"], record["source"], record["id"])
        if kind == "switch":
            return handler(record["source"], record["prev"], record["next"])
        if kind == "stopped":
            return handler(record["source"], record["connection"],
                           last=record.get("last", []),
                           transfer=record.get("transfer", True))
        if kind == "group-status":
            return handler(record["group"], record["online"])
        if kind == "end-round":
            return handler()
        raise ValueError(f"未登记的命令: {kind}")

    def _inspect(self, record: dict[str, Any]) -> dict[str, Any]:
        if "group" in record:
            return self.engine.inspect_group(record["group"])
        if "fragment" in record:
            return self.engine.inspect_source(record["source"]) if "source" in record else \
                {"ok": False, "reasons": ["INSPECT_NEEDS_SOURCE"]}
        if "source" in record:
            return self.engine.inspect_source(record["source"])
        return {"ok": False, "reasons": ["UNKNOWN_INSPECT_TARGET"]}

    def inspect_fragment(self, source: str, fragment_id: str) -> dict[str, Any]:
        return self.engine.inspect_fragment(source, fragment_id)

    def snapshot(self) -> None:
        data = self.engine.to_snapshot()
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(tmp, self.snapshot_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def restart(self) -> Engine:
        """模拟进程重启：关闭句柄、从日志与快照重建引擎。"""
        self.close()
        self.engine = self._load()
        self._handle = self.journal_path.open("a", encoding="utf-8")
        return self.engine

    def close(self) -> None:
        self.snapshot()
        self._handle.close()


COMMAND_METHODS = {
    "pool": "pool",
    "open": "open_connection",
    "credit": "grant_credit",
    "fragment": "send_fragment",
    "done": "complete",
    "switch": "switch",
    "stopped": "stopped",
    "group-status": "group_set_online",
    "end-round": "end_round",
}
