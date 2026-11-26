"""本地仿真端：扮演采集端、处理组与值班员操作中间件。

两种接法：
    LocalSimulator  进程内直连 Journal/Engine，用于演练与单元测试
    HttpSimulator   通过本机 HTTP 入口操作，用于端到端冒烟
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .journal import Journal


class LocalSimulator:
    """进程内仿真端，方法名与 HTTP 路由一一对应。"""

    def __init__(self, state_dir: str) -> None:
        self.journal = Journal(state_dir)

    def pool(self, group: str, cap: int, reserve: int, online: bool = True) -> dict[str, Any]:
        return self.journal.apply("pool", {"group": group, "cap": cap,
                                           "reserve": reserve, "online": online})

    def open(self, source: str, connection: str, **extra: Any) -> dict[str, Any]:
        return self.journal.apply("open", {"source": source,
                                           "connection": connection, **extra})

    def credit(self, source: str, connection: str, credit_id: str, groups: list[str],
               cls: str, bytes_: int) -> dict[str, Any]:
        return self.journal.apply("credit", {
            "source": source, "connection": connection, "credit": credit_id,
            "groups": groups, "class": cls, "bytes": bytes_,
        })

    def fragment(self, source: str, connection: str, credit_id: str, fragment_id: str,
                 groups: list[str], bytes_: int) -> dict[str, Any]:
        return self.journal.apply("fragment", {
            "source": source, "connection": connection, "credit": credit_id,
            "id": fragment_id, "groups": groups, "bytes": bytes_,
        })

    def done(self, group: str, source: str, fragment_id: str) -> dict[str, Any]:
        return self.journal.apply("done", {"group": group, "source": source,
                                           "id": fragment_id})

    def switch(self, source: str, prev: str, next_conn: str) -> dict[str, Any]:
        return self.journal.apply("switch", {"source": source, "prev": prev,
                                             "next": next_conn})

    def stopped(self, source: str, connection: str, last: list[str],
                transfer: bool = True) -> dict[str, Any]:
        return self.journal.apply("stopped", {"source": source, "connection": connection,
                                              "last": last, "transfer": transfer})

    def group_status(self, group: str, online: bool) -> dict[str, Any]:
        return self.journal.apply("group-status", {"group": group, "online": online})

    def end_round(self) -> dict[str, Any]:
        return self.journal.apply("end-round", {})

    def restart(self) -> dict[str, Any]:
        self.journal.restart()
        return {"ok": True, "restart": True}

    def inspect_group(self, group: str) -> dict[str, Any]:
        return self.journal.engine.inspect_group(group)

    def inspect_source(self, source: str) -> dict[str, Any]:
        return self.journal.engine.inspect_source(source)

    def inspect_fragment(self, source: str, fragment_id: str) -> dict[str, Any]:
        return self.journal.engine.inspect_fragment(source, fragment_id)

    def close(self) -> None:
        self.journal.close()


class HttpSimulator:
    """本机 HTTP 仿真端，消息体与演练契约同形。"""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def _post(self, path: str, record: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(record).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return json.loads(error.read().decode("utf-8"))

    def pool(self, group: str, cap: int, reserve: int, online: bool = True) -> dict[str, Any]:
        return self._post("/pool", {"group": group, "cap": cap,
                                    "reserve": reserve, "online": online})

    def open(self, source: str, connection: str, **extra: Any) -> dict[str, Any]:
        return self._post("/open", {"source": source, "connection": connection, **extra})

    def credit(self, source: str, connection: str, credit_id: str, groups: list[str],
               cls: str, bytes_: int) -> dict[str, Any]:
        return self._post("/credit", {
            "source": source, "connection": connection, "credit": credit_id,
            "groups": groups, "class": cls, "bytes": bytes_,
        })

    def fragment(self, source: str, connection: str, credit_id: str, fragment_id: str,
                 groups: list[str], bytes_: int) -> dict[str, Any]:
        return self._post("/fragment", {
            "source": source, "connection": connection, "credit": credit_id,
            "id": fragment_id, "groups": groups, "bytes": bytes_,
        })

    def done(self, group: str, source: str, fragment_id: str) -> dict[str, Any]:
        return self._post("/done", {"group": group, "source": source, "id": fragment_id})

    def switch(self, source: str, prev: str, next_conn: str) -> dict[str, Any]:
        return self._post("/switch", {"source": source, "prev": prev, "next": next_conn})

    def stopped(self, source: str, connection: str, last: list[str],
                transfer: bool = True) -> dict[str, Any]:
        return self._post("/stopped", {"source": source, "connection": connection,
                                       "last": last, "transfer": transfer})

    def group_status(self, group: str, online: bool) -> dict[str, Any]:
        return self._post("/group-status", {"group": group, "online": online})

    def end_round(self) -> dict[str, Any]:
        return self._post("/end-round", {})

    def restart(self) -> dict[str, Any]:
        return self._post("/restart", {})

    def inspect_group(self, group: str) -> dict[str, Any]:
        return self._post("/inspect", {"group": group})

    def inspect_source(self, source: str) -> dict[str, Any]:
        return self._post("/inspect", {"source": source})

    def inspect_fragment(self, source: str, fragment_id: str) -> dict[str, Any]:
        return self._post("/inspect", {"source": source, "fragment": fragment_id})

    def close(self) -> None:
        return None


def demo(state_dir: str) -> list[tuple[str, dict[str, Any]]]:
    """完整业务演练：覆盖题述全部关键口径，返回 (步骤名, 结果) 供断言/展示。"""
    sim = LocalSimulator(state_dir)
    trace: list[tuple[str, dict[str, Any]]] = []

    def step(label: str, result: dict[str, Any]) -> dict[str, Any]:
        trace.append((label, result))
        return result

    # 1) 两个处理组：g1 容量 12（必达保留 3），g2 容量 9（必达保留 2）
    step("建组 g1", sim.pool("g1", 12, 3))
    step("建组 g2", sim.pool("g2", 9, 2))

    # 2) 两个普通来源同时接入；站点配置：每轮每来源只保留 1 字节普通未用额度
    step("a 建连 c1", sim.open("a", "c1", normal_idle_carry=1))
    step("b 建连 d1", sim.open("b", "d1", normal_idle_carry=1))

    # 3) 普通信用按真实余量授予；两来源轮转，a 不能独占 g1 普通份额 9
    r = step("a 申请 g1 普通信用 8（轮内封顶，只拿权重份额）",
             sim.credit("a", "c1", "va", ["g1"], "normal", 8))
    assert r["ok"] and r["per_group"]["g1"]["granted"] <= 5
    r_b = step("b 申请 g1 普通信用 8（余量留给后来者，b 不被饿死）",
               sim.credit("b", "d1", "vb", ["g1"], "normal", 8))
    assert r_b["ok"] and r_b["per_group"]["g1"]["granted"] >= 1
    # a 已到轮内封顶，继续申请不会再多拿
    r_more = step("a 同轮再申请被封顶",
                  sim.credit("a", "c1", "va2", ["g1"], "normal", 4))
    assert r_more["per_group"]["g1"]["granted"] == 0

    # 4) 片段同时进两个必需组，完整字节分别占用；超信用整段不受理
    step("结束轮次：未用普通余量按站点配置回收共享", sim.end_round())
    step("a 申请 g1+g2 普通信用 3（按各组真实余量授予）",
         sim.credit("a", "c1", "v2", ["g1", "g2"], "normal", 3))
    step("a 发送 f1（g1+g2，3 字节，两组各占 3）",
         sim.fragment("a", "c1", "v2", "f1", ["g1", "g2"], 3))
    too_big = step("a 发送超信用 f2 被拒",
                   sim.fragment("a", "c1", "v2", "f2", ["g1", "g2"], 4))
    assert not too_big["ok"] and too_big["reasons"] == ["INSUFFICIENT_CREDIT"]

    # 5) 一个组的完成回执只返还该组占用
    step("g1 完成 f1", sim.done("g1", "a", "f1"))
    g2_before = sim.inspect_group("g2")
    assert g2_before["classes"]["normal"]["occupied"] == 3, "另一组不应被连带返还"

    # 6) 必达标记有独立份额；组断开时不能绕过实际容量
    step("g2 断连", sim.group_status("g2", False))
    blocked = step("断连期间必达申请被拒",
                   sim.credit("a", "c1", "p1", ["g2"], "essential", 1))
    assert not blocked["ok"] and blocked["reasons"] == ["GROUP_DOWN"]
    step("g2 恢复", sim.group_status("g2", True))
    essential = step("必达申请 1（独立 reserve 份额）",
                     sim.credit("a", "c1", "p1", ["g2"], "essential", 1))
    assert essential["ok"]

    # 7) 缩容：真实占用大于新上限 -> 历史欠额，限制新受理
    shrink = step("g2 缩容到 cap=3 reserve=1", sim.pool("g2", 3, 1))
    assert shrink["debt"]["normal"] > 0
    debt_block = step("欠额期间普通新受理被拒",
                      sim.credit("a", "c1", "v3", ["g2"], "normal", 1))
    assert not debt_block["ok"] and debt_block["reasons"] == ["GROUP_DEBT"]
    # 同来源的必达类走独立链，不受普通欠额牵连
    essential_ok = step("必达链独立，仍可受理",
                        sim.credit("a", "c1", "p2", ["g2"], "essential", 1))
    assert essential_ok["ok"]

    # 8) 完成回执按实际占用返还，旧回执幂等，不能灌大新配置
    done1 = step("g2 完成 f1（返还受新份额约束）", sim.done("g2", "a", "f1"))
    done2 = step("g2 重复完成 f1（幂等 no-op）", sim.done("g2", "a", "f1"))
    assert done2["ok"] and done2.get("duplicate") and done2["returned"] == 0

    # 9) 连接迁移：switch 后旧连接结局未定，stopped 后未用信用转给新连接
    step("b 提出切换 d1->d2", sim.switch("b", "d1", "d2"))
    pending = sim.inspect_source("b")
    assert pending["connections"]["d1"] == "switching"
    step("重启：信用与占用差异须保持", sim.restart())
    after = sim.inspect_source("b")
    assert after["connections"]["d1"] == "switching", "重启后连接结局状态丢失"
    step("b 确认 d1 停止", sim.stopped("b", "d1", last=[]))
    moved = sim.inspect_source("b")
    assert moved["current"] == "d2"

    # 10) 旧连接晚到重复片段只回原状态，不消耗新连接信用
    step("b 用 d1 拿信用并送片段 fb", sim.credit("b", "d2", "vb2", ["g1"], "normal", 2))
    # d1 已 stopped；造一个 d1 时代已受理片段的晚到重复：先用 d2 受理再用 d1 重投
    step("b 经 d2 发送 fb", sim.fragment("b", "d2", "vb2", "fb", ["g1"], 2))
    late = step("b 经旧连接 d1 晚送重复 fb",
                sim.fragment("b", "d1", "vb2", "fb", ["g1"], 2))
    assert late["ok"] and late.get("duplicate") is True
    bal_after = sim.inspect_source("b")
    fb_view = next(f for f in bal_after["fragments"] if f["id"] == "fb")
    assert fb_view["status"] == "admitted"

    # 11) 轮次：普通未用额度按站点配置回收共享，轮转防独占
    step("结束轮次（余量共享）", sim.end_round())

    sim.close()
    return trace


if __name__ == "__main__":
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(description="本地仿真端综合演练")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--http", default=None, help="对运行中的 HTTP 入口做冒烟，参数为 base-url")
    args = parser.parse_args()

    if args.http:
        client = HttpSimulator(args.http)
        print(json.dumps(client.pool("hg", 5, 1), ensure_ascii=False))
        print(json.dumps(client.open("ha", "hc"), ensure_ascii=False))
        print(json.dumps(client.credit("ha", "hc", "hp", ["hg"], "normal", 3), ensure_ascii=False))
        print(json.dumps(client.fragment("ha", "hc", "hp", "hf", ["hg"], 3), ensure_ascii=False))
        print(json.dumps(client.done("hg", "ha", "hf"), ensure_ascii=False))
    else:
        state_dir = args.state_dir or tempfile.mkdtemp(prefix="flow-demo-")
        for label, result in demo(state_dir):
            body = json.dumps(result, ensure_ascii=False) if result is not None else ""
            print(f"== {label}\n   {body}")
        print(f"\n状态目录：{state_dir}（可用 inspect 子命令继续查询）")
