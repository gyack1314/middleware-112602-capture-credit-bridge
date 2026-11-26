"""命令行运行入口。

用法示例：
    python3 -m flowcontrol.cli run-scenario examples/switch-with-unused-credit.jsonl \\
        --state-dir .state/switch
    python3 -m flowcontrol.cli inspect --state-dir .state/switch --source a
    python3 -m flowcontrol.cli inspect --state-dir .state/switch --group g
    python3 -m flowcontrol.cli inspect --state-dir .state/switch --source a --fragment f
    python3 -m flowcontrol.cli cmd --state-dir .state/demo --kind pool \\
        --record '{"group":"g1","cap":12,"reserve":3}'
    python3 -m flowcontrol.cli serve --state-dir .state/serve --port 8080
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .journal import Journal
from .runner import format_trace, run_file
from .server import serve


def _load_supported() -> set[str]:
    path = Path(__file__).resolve().parent.parent / "examples" / "message_types.json"
    if path.exists():
        return set(json.loads(path.read_text(encoding="utf-8")))
    return set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flowcontrol", description="采集流控中间件")
    sub = parser.add_subparsers(dest="action", required=True)

    p_run = sub.add_parser("run-scenario", help="按 JSONL 演练资料驱动中间件")
    p_run.add_argument("scenario", type=Path)
    p_run.add_argument("--state-dir", required=True)
    p_run.add_argument("--json", action="store_true", help="输出逐条 JSON 而非摘要")

    p_ins = sub.add_parser("inspect", help="值班查询")
    p_ins.add_argument("--state-dir", required=True)
    p_ins.add_argument("--group")
    p_ins.add_argument("--source")
    p_ins.add_argument("--fragment")

    p_cmd = sub.add_parser("cmd", help="执行单条命令（record 与演练契约同形）")
    p_cmd.add_argument("--state-dir", required=True)
    p_cmd.add_argument("--kind", required=True)
    p_cmd.add_argument("--record", required=True, help="JSON 字符串，或 @文件路径")

    p_restart = sub.add_parser("restart", help="模拟进程重启并自检状态保持")
    p_restart.add_argument("--state-dir", required=True)

    p_serve = sub.add_parser("serve", help="启动本地仿真 HTTP 入口")
    p_serve.add_argument("--state-dir", required=True)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)

    args = parser.parse_args(argv)

    if args.action == "serve":
        serve(args.state_dir, args.host, args.port)
        return 0

    if args.action == "run-scenario":
        journal = Journal(args.state_dir)
        try:
            events = run_file(args.scenario, journal, _load_supported())
        finally:
            journal.close()
        if args.json:
            for event in events:
                print(json.dumps(event, ensure_ascii=False))
        else:
            print(format_trace(events))
        return 0

    journal = Journal(args.state_dir)
    try:
        if args.action == "restart":
            before = journal.engine.to_snapshot()
            journal.restart()
            after = journal.engine.to_snapshot()
            same = json.dumps(before, sort_keys=True, ensure_ascii=False) == \
                json.dumps(after, sort_keys=True, ensure_ascii=False)
            print(json.dumps({"ok": same, "seq": journal.engine.command_seq,
                              "round": journal.engine.round_no}, ensure_ascii=False))
            return 0 if same else 2

        if args.action == "inspect":
            engine = journal.engine
            if args.group:
                result = engine.inspect_group(args.group)
            elif args.source and args.fragment:
                result = engine.inspect_fragment(args.source, args.fragment)
            elif args.source:
                result = engine.inspect_source(args.source)
            else:
                parser.error("inspect 需要 --group 或 --source [--fragment]")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 3

        if args.action == "cmd":
            raw = args.record
            if raw.startswith("@"):
                raw = Path(raw[1:]).read_text(encoding="utf-8")
            record = json.loads(raw)
            result = journal.apply(args.kind, record)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 4
    finally:
        journal.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
