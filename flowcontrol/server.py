"""本地仿真 HTTP 入口：值班员与仿真采集端通过 JSON over HTTP 操作中间件。

路由（全部 POST，请求体为 JSON record，与演练契约同形）：
    /pool /open /credit /fragment /done /switch /stopped
    /group-status   处理组断连/恢复 {"group","online"}
    /end-round      结束当前普通份额轮次，余量进入共享
    /restart        进程重启（从日志重建）
    /inspect        {"group"} 或 {"source"} 或 {"source","fragment"}
    /healthz
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .journal import Journal

ROUTES = {
    "/pool": "pool",
    "/open": "open",
    "/credit": "credit",
    "/fragment": "fragment",
    "/done": "done",
    "/switch": "switch",
    "/stopped": "stopped",
    "/group-status": "group-status",
    "/end-round": "end-round",
}


class FlowControlHandler(BaseHTTPRequestHandler):
    journal: Journal
    lock: threading.Lock

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静输出
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send(200, {"ok": True, "seq": self.journal.engine.command_seq})
        else:
            self._send(404, {"ok": False, "reasons": ["NOT_FOUND"]})

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", 0))
            record = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(record, dict):
                raise ValueError("请求体应为 JSON 对象")
        except (ValueError, json.JSONDecodeError) as error:
            self._send(400, {"ok": False, "reasons": ["BAD_REQUEST"], "detail": str(error)})
            return

        if self.path == "/restart":
            with self.lock:
                self.journal.restart()
            self._send(200, {"ok": True, "restart": True})
            return
        if self.path == "/inspect":
            with self.lock:
                result = self._inspect(record)
            self._send(200 if result.get("ok") else 404, result)
            return
        kind = ROUTES.get(self.path)
        if kind is None:
            self._send(404, {"ok": False, "reasons": ["NOT_FOUND"]})
            return
        with self.lock:
            result = self.journal.apply(kind, record)
        self._send(200 if result.get("ok") else 409, result)

    def _inspect(self, record: dict[str, Any]) -> dict[str, Any]:
        engine = self.journal.engine
        if "group" in record:
            return engine.inspect_group(record["group"])
        if "fragment" in record and "source" in record:
            return engine.inspect_fragment(record["source"], record["fragment"])
        if "source" in record:
            return engine.inspect_source(record["source"])
        return {"ok": False, "reasons": ["UNKNOWN_INSPECT_TARGET"]}


def build_server(state_dir: str, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    journal = Journal(state_dir)
    lock = threading.Lock()

    class Handler(FlowControlHandler):
        pass

    Handler.journal = journal
    Handler.lock = lock
    server = ThreadingHTTPServer((host, port), Handler)
    server.journal = journal  # type: ignore[attr-defined]
    server.state_lock = lock  # type: ignore[attr-defined]
    return server


def serve(state_dir: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    server = build_server(state_dir, host, port)
    actual_host, actual_port = server.server_address[:2]
    print(f"流控中间件已启动：http://{actual_host}:{actual_port}  状态目录={state_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.journal.close()  # type: ignore[attr-defined]
        server.server_close()
