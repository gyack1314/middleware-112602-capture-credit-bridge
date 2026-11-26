"""演练驱动器：按 JSONL 输入契约顺序驱动引擎，输出每条消息的处置结果。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from scenario_reader import iter_messages

from .journal import Journal

SCENARIO_KINDS = (
    "pool", "open", "credit", "fragment", "done",
    "switch", "stopped", "restart", "inspect",
    # 中间件扩展命令（不在演练契约登记表中，仅 CLI/HTTP 使用）
    "group-status", "end-round",
)


def run_scenario(path: str | Path, journal: Journal,
                 supported: set[str] | None = None) -> Iterator[dict[str, Any]]:
    """逐条演练；遇到 restart 消息时从日志重建引擎，验证差异保持。"""
    known = set(supported or ()) | set(SCENARIO_KINDS)
    for message in iter_messages(Path(path), known):
        kind = message["kind"]
        record = message["record"]
        if kind == "restart":
            journal.restart()
            result = {"ok": True, "restart": True}
        elif kind == "inspect":
            result = _inspect(journal, record)
        else:
            result = journal.apply(kind, record)
        yield {
            "at": message["at"],
            "kind": kind,
            "record": record,
            "result": result,
        }


def _inspect(journal: Journal, record: dict[str, Any]) -> dict[str, Any]:
    if "group" in record:
        return journal.engine.inspect_group(record["group"])
    if "source" in record:
        return journal.engine.inspect_source(record["source"])
    if "fragment" in record and "source" in record:
        return journal.engine.inspect_fragment(record["source"], record["fragment"])
    return {"ok": False, "reasons": ["UNKNOWN_INSPECT_TARGET"]}


def run_file(path: str | Path, journal: Journal,
             supported: set[str] | None = None) -> list[dict[str, Any]]:
    return list(run_scenario(path, journal, supported))


def format_trace(events: list[dict[str, Any]]) -> str:
    lines = []
    for event in events:
        result = event["result"]
        mark = "OK " if result.get("ok") else "ERR"
        summary = _summarize(event["kind"], result)
        lines.append(f"at={event['at']:>3} {mark} {event['kind']:<9} {summary}")
    return "\n".join(lines)


def _summarize(kind: str, result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return ",".join(result.get("reasons", [])) + (
            f" {json.dumps(result.get('detail', {}), ensure_ascii=False)}"
            if result.get("detail") else ""
        )
    if kind == "pool":
        debt = result.get("debt")
        if debt and any(debt.values()):
            return f"{result['group']} cap={result['cap']} debt={debt} expired={result.get('expired')}"
        return f"{result['group']} cap={result['cap']} reserve={result['reserve']}"
    if kind == "credit":
        return (f"{result['source']}/{result['credit']} [{result['class_']}] "
                f"requested={result['requested']} granted={result['granted']}"
                + (" PARTIAL" if result.get("partial") else ""))
    if kind == "fragment":
        if result.get("duplicate"):
            return f"{result['fragment']['id']} LATE-DUPLICATE status={result['fragment']['status']}"
        return f"{result['fragment']['id']} admitted={result['admitted']}"
    if kind == "done":
        if result.get("duplicate"):
            return f"{result['group']}/{result['fragment']} DUPLICATE-DONE"
        return (f"{result['group']}/{result['fragment']} released={result['released']} "
                f"returned={result['returned']} expired={result['expired']} "
                f"debt_cleared={result['debt_cleared']}")
    if kind == "switch":
        return f"{result['source']} {result['prev']}->{result['next']} pending={len(result['pending_credit'])}"
    if kind == "stopped":
        moved = ",".join(f"{m['credit']}:{m['bytes']}->{m['to']}" for m in result["transferred"])
        return f"{result['source']}/{result['connection']} moved=[{moved}] reclaimed={result['reclaimed']}"
    if kind == "inspect":
        return "inspect"
    if kind == "restart":
        return "engine rebuilt from journal"
    return json.dumps(result, ensure_ascii=False)
