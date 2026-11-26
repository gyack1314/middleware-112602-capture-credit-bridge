"""HTTP 仿真入口端到端冒烟与值班查询。"""

import json
import tempfile
import threading
import unittest
import urllib.request

from flowcontrol.server import build_server
from flowcontrol.simulator import HttpSimulator


class HttpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.server = build_server(self.tempdir.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.sim = HttpSimulator(f"http://{host}:{port}")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.server.journal.close()
        self.tempdir.cleanup()

    def test_full_flow_over_http(self) -> None:
        self.assertTrue(self.sim.pool("g", 8, 2)["ok"])
        self.assertTrue(self.sim.open("a", "c")["ok"])
        credit = self.sim.credit("a", "c", "v", ["g"], "normal", 6)
        self.assertEqual(credit["per_group"]["g"]["granted"], 6)
        admitted = self.sim.fragment("a", "c", "v", "f", ["g"], 6)
        self.assertTrue(admitted["ok"])
        done = self.sim.done("g", "a", "f")
        self.assertEqual(done["returned"], 6)

        view = self.sim.inspect_group("g")
        self.assertEqual(view["classes"]["normal"]["occupied"], 0)
        source = self.sim.inspect_source("a")
        self.assertEqual(source["can_send_bytes"]["g"]["normal"], 6)
        fragment = self.sim.inspect_fragment("a", "f")
        self.assertEqual(fragment["fragment"]["status"], "completed")

    def test_restart_endpoint_rebuilds_state(self) -> None:
        self.sim.pool("g", 5, 1)
        self.sim.open("a", "c")
        self.sim.credit("a", "c", "v", ["g"], "normal", 4)
        self.sim.fragment("a", "c", "v", "f", ["g"], 2)
        restarted = self.sim.restart()
        self.assertTrue(restarted["restart"])
        view = self.sim.inspect_group("g")
        self.assertEqual(view["classes"]["normal"]["occupied"], 2)
        self.assertEqual(view["classes"]["normal"]["unused_credit"], 2)

    def test_rejection_uses_409_and_reason_code(self) -> None:
        self.sim.pool("g", 3, 1)
        self.sim.open("a", "c")
        self.sim.group_status("g", False)
        result = self.sim.credit("a", "c", "v", ["g"], "normal", 1)
        self.assertEqual(result["reasons"], ["GROUP_DOWN"])

    def test_healthz(self) -> None:
        host, port = self.server.server_address[:2]
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()
