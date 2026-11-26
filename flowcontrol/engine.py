"""采集流控中间件核心引擎。

核心口径（所有额度均为合成整数 byte）：

* 每个处理组 group 有真实可接上限 cap，其中 reserve 字节预留给必达（essential）
  份额，其余 cap - reserve 为普通（normal）份额。两个份额各自独立记账，
  组断开时一律不受理、不发新信用。
* 一个片段同时进入若干必需处理组，按完整 bytes 分别占用每个组；任一组不满足
  就整段不受理（不产生任何占用、不扣任何信用）。
* 信用是“按真实可接能力”授予的：组内 O（真实占用）+ U（已授未用）<= 份额。
  回执完成（done）后才把字节返还到那次实际占用的信用账户，旧回执幂等，
  无法灌大已经缩容后的新配置。
* 缩容产生历史欠额 debt，欠额存在期间该组该份额限制新受理。
* 连接切换走 switch/stopped 两阶段：未确认结局的旧连接保留其信用与占用；
  旧连接明确停止后，未用信用可收回或转给新连接；旧连接晚到的重复投送只回
  原状态，不消耗新连接信用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

NORMAL = "normal"
ESSENTIAL = "essential"
CLASSES = (NORMAL, ESSENTIAL)

# 连接生命周期
CONN_OPEN = "open"          # 正常在用
CONN_SWITCHING = "switching"  # 已提出切换，结局未定
CONN_STOPPED = "stopped"    # 来源确认无在途，可安全回收/转移

# 片段受理状态
FRAG_ADMITTED = "admitted"      # 已受理，占用各组
FRAG_COMPLETED = "completed"    # 所有必需组均已完成
FRAG_REJECTED = "rejected"      # 未受理（未产生占用）


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, "reasons": [], **payload}


def _fail(reason: str, detail: dict[str, Any] | None = None, **payload: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "reasons": [reason], **payload}
    if detail:
        result["detail"] = detail
    return result


@dataclass
class GroupState:
    name: str
    cap: int
    reserve: int
    epoch: int = 0
    online: bool = True
    # 真实占用：class -> source -> bytes
    occupied: dict[str, dict[str, int]] = field(
        default_factory=lambda: {NORMAL: {}, ESSENTIAL: {}}
    )
    # 历史欠额：class -> bytes（缩容后真实占用超过新份额的部分）
    debt: dict[str, int] = field(default_factory=lambda: {NORMAL: 0, ESSENTIAL: 0})
    # 上一轮实际授给各来源的字节（公平轮转用）：class -> source -> bytes
    last_granted: dict[str, dict[str, int]] = field(
        default_factory=lambda: {NORMAL: {}, ESSENTIAL: {}}
    )

    def share(self, cls: str) -> int:
        return self.reserve if cls == ESSENTIAL else self.cap - self.reserve

    def used(self, cls: str) -> int:
        return sum(self.occupied[cls].values())

    def unused(self, engine: "Engine", cls: str) -> int:
        total = 0
        for account in engine.accounts.values():
            if account.group == self.name and account.cls == cls and account.active:
                total += account.balance
        return total

    def available(self, engine: "Engine", cls: str) -> int:
        """当前真实可接额度（含已承诺未用的信用）。"""
        return max(0, self.share(cls) - self.used(cls) - self.unused(engine, cls))


@dataclass
class CreditAccount:
    credit_id: str
    source: str
    group: str
    cls: str
    connection: str
    balance: int = 0                 # 已授未用
    granted_total: int = 0           # 累计授予（同 id 可分次申请）
    returned_total: int = 0          # 完成回执累计返还
    expired_total: int = 0           # 缩容/停连作废的部分
    active: bool = True              # False 表示随旧连接停止而停用
    epoch: int = 0                   # 授予时所在的组配置代次

    def to_dict(self) -> dict[str, Any]:
        return {
            "credit_id": self.credit_id,
            "source": self.source,
            "group": self.group,
            "class": self.cls,
            "connection": self.connection,
            "balance": self.balance,
            "granted_total": self.granted_total,
            "returned_total": self.returned_total,
            "expired_total": self.expired_total,
            "active": self.active,
            "epoch": self.epoch,
        }


@dataclass
class Occupancy:
    credit_id: str
    connection: str
    cls: str
    bytes: int
    epoch: int
    done: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "credit_id": self.credit_id,
            "connection": self.connection,
            "class": self.cls,
            "bytes": self.bytes,
            "epoch": self.epoch,
            "done": self.done,
        }


@dataclass
class FragmentRecord:
    fragment_id: str
    source: str
    connection: str
    bytes: int
    groups: list[str]
    cls: str
    status: str
    reason: str | None = None
    detail: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.fragment_id,
            "source": self.source,
            "connection": self.connection,
            "bytes": self.bytes,
            "groups": list(self.groups),
            "class": self.cls,
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass
class SourceState:
    name: str
    # connection -> 状态
    connections: dict[str, str] = field(default_factory=dict)
    current: str | None = None
    # 已受理片段 id -> 记录
    fragments: dict[str, FragmentRecord] = field(default_factory=dict)
    # (group, fragment_id) -> 占用
    occupancies: dict[tuple[str, str], Occupancy] = field(default_factory=dict)
    # 停止时上报的末尾片段清单（连接 -> ids）
    last_reported: dict[str, list[str]] = field(default_factory=dict)
    weight: int = 1
    normal_idle_carry: int | None = None  # 站点配置：每轮可保留的普通未用额度
    round_index: int = 0                 # 该来源已参与的轮次


class Engine:
    """同步、单线程语义的流控引擎；所有命令返回结构化结果字典。"""

    def __init__(self, share_leftovers: bool = True, strict: bool = True) -> None:
        self.groups: dict[str, GroupState] = {}
        self.sources: dict[str, SourceState] = {}
        # 键：(source, credit_id, group) —— 一次信用申请对每个必需组各开一个子账户
        self.accounts: dict[tuple[str, str, str], CreditAccount] = {}
        self.round_no: int = 0
        # 普通份额每轮公平轮转的起点
        self._rotation: dict[str, int] = {}
        self.share_leftovers = share_leftovers
        self.strict = strict  # 开发期不变量自检
        self.command_seq: int = 0

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _check_invariants(self) -> None:
        if not self.strict:
            return
        for group in self.groups.values():
            for cls in CLASSES:
                used = group.used(cls)
                unused = group.unused(self, cls)
                debt = group.debt[cls]
                # 欠额即 used - share 的超额；O+U 不得超过份额（欠额时无未用信用）
                assert used + unused <= group.share(cls) + max(0, used - group.share(cls)), (
                    f"组 {group.name}/{cls} 超出真实容量: used={used} unused={unused} "
                    f"share={group.share(cls)}"
                )
                if debt > 0:
                    assert used > group.share(cls), (
                        f"组 {group.name}/{cls} 欠额未清但占用已回到份额内"
                    )
                    assert unused == 0, f"组 {group.name}/{cls} 欠额期间不得保留未用信用"

    def _group(self, name: str) -> GroupState | None:
        return self.groups.get(name)

    def _source(self, name: str) -> SourceState | None:
        return self.sources.get(name)

    def _connection_state(self, source: str, connection: str) -> str | None:
        state = self._source(source)
        if state is None:
            return None
        return state.connections.get(connection)

    # ------------------------------------------------------------------ #
    # pool：建立/调整处理组
    # ------------------------------------------------------------------ #
    def pool(self, group: str, cap: int, reserve: int, online: bool = True) -> dict[str, Any]:
        if cap < 0 or reserve < 0 or reserve > cap:
            return _fail("BAD_POOL_CONFIG", {"group": group, "cap": cap, "reserve": reserve})
        existing = self.groups.get(group)
        if existing is None:
            state = GroupState(name=group, cap=cap, reserve=reserve, online=online)
            self.groups[group] = state
            self.command_seq += 1
            self._check_invariants()
            return _ok(group=group, cap=cap, reserve=reserve, epoch=0, action="created")
        old_caps = {cls: existing.share(cls) for cls in CLASSES}
        existing.cap = cap
        existing.reserve = reserve
        existing.epoch += 1
        existing.online = online
        expired = {NORMAL: 0, ESSENTIAL: 0}
        debts = {NORMAL: 0, ESSENTIAL: 0}
        for cls in CLASSES:
            share = existing.share(cls)
            used = existing.used(cls)
            # 1) 历史欠额：真实占用超过新份额
            debt = max(0, used - share)
            existing.debt[cls] = debt
            debts[cls] = debt
            # 2) 未用信用超出新份额余量则作废（旧配置不能灌大新配置）
            headroom = max(0, share - used)
            for account in self.accounts.values():
                if account.group != group or account.cls != cls or not account.active:
                    continue
                if account.balance > headroom:
                    cut = account.balance - headroom
                    account.balance = headroom
                    account.expired_total += cut
                    expired[cls] += cut
            existing.last_granted[cls].clear()
        self.command_seq += 1
        self._check_invariants()
        return _ok(
            group=group,
            cap=cap,
            reserve=reserve,
            epoch=existing.epoch,
            action="reconfigured",
            old_shares=old_caps,
            expired=expired,
            debt=debts,
        )

    def group_set_online(self, group: str, online: bool) -> dict[str, Any]:
        state = self._group(group)
        if state is None:
            return _fail("UNKNOWN_GROUP", {"group": group})
        state.online = online
        self.command_seq += 1
        return _ok(group=group, online=online, promised_credit_kept=True)

    # ------------------------------------------------------------------ #
    # open / switch / stopped：连接生命周期
    # ------------------------------------------------------------------ #
    def open_connection(self, source: str, connection: str,
                        weight: int = 1, normal_idle_carry: int | None = None) -> dict[str, Any]:
        if weight <= 0:
            return _fail("BAD_SOURCE_CONFIG", {"weight": weight})
        state = self.sources.get(source)
        if state is None:
            state = SourceState(name=source)
            self.sources[source] = state
        if connection in state.connections:
            return _fail("CONNECTION_EXISTS", {"source": source, "connection": connection})
        state.connections[connection] = CONN_OPEN
        state.weight = weight
        if normal_idle_carry is not None:
            state.normal_idle_carry = normal_idle_carry
        if state.current is None:
            state.current = connection
        self.command_seq += 1
        return _ok(source=source, connection=connection, current=state.current)

    def switch(self, source: str, prev: str, next_conn: str) -> dict[str, Any]:
        state = self._source(source)
        if state is None:
            return _fail("UNKNOWN_SOURCE", {"source": source})
        if prev not in state.connections:
            return _fail("UNKNOWN_CONNECTION", {"source": source, "connection": prev})
        if next_conn in state.connections:
            return _fail("CONNECTION_EXISTS", {"source": source, "connection": next_conn})
        if state.connections[prev] == CONN_STOPPED:
            return _fail("CONNECTION_STOPPED", {"source": source, "connection": prev})
        state.connections[prev] = CONN_SWITCHING
        state.connections[next_conn] = CONN_OPEN
        state.current = next_conn
        # 旧连接上未确认结局的信用保持 active，等待 stopped
        self.command_seq += 1
        return _ok(
            source=source,
            prev=prev,
            prev_state=CONN_SWITCHING,
            next=next_conn,
            pending_credit=self._credit_on(source, prev),
        )

    def stopped(self, source: str, connection: str, last: list[str] | None = None,
                transfer: bool = True) -> dict[str, Any]:
        state = self._source(source)
        if state is None:
            return _fail("UNKNOWN_SOURCE", {"source": source})
        if connection not in state.connections:
            return _fail("UNKNOWN_CONNECTION", {"source": source, "connection": connection})
        if state.connections[connection] == CONN_STOPPED:
            return _fail("CONNECTION_STOPPED", {"source": source, "connection": connection})
        last = list(last or [])
        unknown_last = [fid for fid in last if fid not in state.fragments]
        if unknown_last:
            return _fail("UNKNOWN_LAST_FRAGMENT", {"unknown": unknown_last})
        state.last_reported[connection] = last
        state.connections[connection] = CONN_STOPPED

        moved: list[dict[str, Any]] = []
        reclaimed = 0
        for account in list(self.accounts.values()):
            if account.source != source or account.connection != connection or not account.active:
                continue
            if transfer and state.current is not None and state.current != connection:
                account.connection = state.current
                moved.append({
                    "credit": account.credit_id,
                    "group": account.group,
                    "class": account.cls,
                    "bytes": account.balance,
                    "to": state.current,
                })
            else:
                reclaimed += account.balance
                account.balance = 0
                account.active = False
        self.command_seq += 1
        self._check_invariants()
        return _ok(
            source=source,
            connection=connection,
            transferred=moved,
            reclaimed=reclaimed,
            current=state.current,
        )

    def _credit_on(self, source: str, connection: str) -> list[dict[str, Any]]:
        return [
            {
                "credit": account.credit_id,
                "group": account.group,
                "class": account.cls,
                "balance": account.balance,
                "active": account.active,
            }
            for account in self.accounts.values()
            if account.source == source and account.connection == connection
        ]

    # ------------------------------------------------------------------ #
    # credit：按真实可接能力发信用
    # ------------------------------------------------------------------ #
    def grant_credit(self, source: str, connection: str, credit_id: str,
                     groups: list[str], cls: str, bytes_: int) -> dict[str, Any]:
        if cls not in CLASSES or bytes_ < 0:
            return _fail("BAD_CREDIT_REQUEST", {"class": cls, "bytes": bytes_})
        state = self._source(source)
        if state is None:
            return _fail("UNKNOWN_SOURCE", {"source": source})
        conn_state = state.connections.get(connection)
        if conn_state is None:
            return _fail("UNKNOWN_CONNECTION", {"source": source, "connection": connection})
        if conn_state != CONN_OPEN:
            return _fail("CONNECTION_NOT_OPEN",
                         {"source": source, "connection": connection, "state": conn_state})
        if not groups:
            return _fail("EMPTY_GROUPS", {})
        for group_name in groups:
            group = self._group(group_name)
            if group is None:
                return _fail("UNKNOWN_GROUP", {"group": group_name})
            if not group.online:
                return _fail("GROUP_DOWN", {"group": group_name})
            if group.debt[cls] > 0:
                return _fail("GROUP_DEBT",
                             {"group": group_name, "class": cls, "debt": group.debt[cls]})
        if any(key[0] == source and key[1] == credit_id for key in self.accounts):
            return _fail("CREDIT_EXISTS", {"source": source, "credit": credit_id})

        per_group: dict[str, dict[str, Any]] = {}
        if cls == NORMAL:
            grant_plan = self._normal_grant_plan(source, groups, bytes_)
        else:
            grant_plan = self._essential_grant_plan(groups, bytes_)

        for group_name in groups:
            group = self.groups[group_name]
            granted = grant_plan[group_name]
            account = CreditAccount(
                credit_id=credit_id,
                source=source,
                group=group_name,
                cls=cls,
                connection=connection,
                balance=granted,
                granted_total=granted,
                epoch=group.epoch,
            )
            self.accounts[(source, credit_id, group_name)] = account
            group.last_granted[cls][source] = group.last_granted[cls].get(source, 0) + granted
            per_group[group_name] = {
                "granted": granted,
                "requested": bytes_,
                "available_before": group.available(self, cls) + granted,
            }
        total = sum(item["granted"] for item in per_group.values())
        self.command_seq += 1
        self._check_invariants()
        return _ok(
            source=source,
            connection=connection,
            credit=credit_id,
            class_=cls,
            requested=bytes_,
            granted=total,
            per_group=per_group,
            partial=total < bytes_ * len(groups),
        )

    def _normal_grant_plan(self, source: str, groups: list[str],
                           requested: int) -> dict[str, int]:
        """普通份额：多来源时轮内按站点权重封顶防独占；单来源可吃满真实余量。

        * 多来源：首轮授予不超过该来源本轮权重份额，余量留给其它来源，轮末共享；
        * 单来源（无其它活跃连接）：没有独占对象，按真实可接余量全额授予。
        """
        plan: dict[str, int] = {}
        other_active = any(
            name != source and any(conn == CONN_OPEN for conn in state.connections.values())
            for name, state in self.sources.items()
        )
        for group_name in groups:
            group = self.groups[group_name]
            available = group.available(self, NORMAL)
            if available <= 0 or requested == 0:
                plan[group_name] = 0
                continue
            if not other_active:
                plan[group_name] = min(requested, available)
                continue
            cap = self._source_round_cap(source, group)
            already = group.last_granted[NORMAL].get(source, 0)
            plan[group_name] = max(0, min(requested, available, cap - already))
        return plan

    def _source_round_cap(self, source: str, group: GroupState) -> int:
        """单个来源在一轮内可取得的普通份额上限（按站点权重切分）。"""
        active_sources = [
            name for name, state in self.sources.items()
            if any(state.connections[c] == CONN_OPEN for c in state.connections)
        ]
        if source not in active_sources:
            active_sources.append(source)
        total_weight = sum(self.sources[name].weight for name in active_sources)
        share = group.share(NORMAL) * self.sources[source].weight // max(1, total_weight)
        return max(1, share) if share > 0 else 0

    def _essential_grant_plan(self, groups: list[str], requested: int) -> dict[str, int]:
        """必达份额：独立保留额度，FIFO 申请、按真实余量授予，断组不绕过容量。"""
        return {
            name: max(0, min(requested, self.groups[name].available(self, ESSENTIAL)))
            for name in groups
        }

    # ------------------------------------------------------------------ #
    # fragment：发送片段
    # ------------------------------------------------------------------ #
    def send_fragment(self, source: str, connection: str, credit_id: str,
                      fragment_id: str, groups: list[str], bytes_: int) -> dict[str, Any]:
        state = self._source(source)
        if state is None:
            return _fail("UNKNOWN_SOURCE", {"source": source})
        # 迟到重复投送：只回原状态，不产生新占用、不消耗新连接信用
        if fragment_id in state.fragments:
            record = state.fragments[fragment_id]
            return _ok(
                duplicate=True,
                fragment=self._fragment_view(record),
                note="LATE_DUPLICATE_RETURNS_ORIGINAL_STATE",
            )
        conn_state = state.connections.get(connection)
        if conn_state is None:
            return _fail("UNKNOWN_CONNECTION", {"source": source, "connection": connection})
        if conn_state == CONN_STOPPED:
            return _fail("CONNECTION_STOPPED", {"source": source, "connection": connection})
        if not groups:
            return _fail("EMPTY_GROUPS", {})

        cls: str | None = None
        accounts: dict[str, CreditAccount] = {}
        for group_name in groups:
            key = (source, credit_id, group_name)
            account = self.accounts.get(key)
            if account is None:
                return _fail("UNKNOWN_CREDIT",
                             {"source": source, "credit": credit_id, "group": group_name})
            if not account.active:
                return _fail("CREDIT_INACTIVE",
                             {"credit": credit_id, "group": group_name})
            if cls is None:
                cls = account.cls
            elif account.cls != cls:
                return _fail("CREDIT_CLASS_MISMATCH", {"credit": credit_id})
            group = self.groups[group_name]
            if not group.online:
                return _fail("GROUP_DOWN", {"group": group_name})
            if group.debt[cls] > 0:
                return _fail("GROUP_DEBT",
                             {"group": group_name, "class": cls, "debt": group.debt[cls]})
            accounts[group_name] = account

        assert cls is not None
        # 超出当次信用的部分不受理：整段拒绝，并给出每组还能接多少
        admissible = {name: account.balance for name, account in accounts.items()}
        if any(account.balance < bytes_ for account in accounts.values()):
            detail = {
                "fragment": fragment_id,
                "bytes": bytes_,
                "admissible": admissible,
                "hint": "拆小片段或等待新信用",
            }
            record = FragmentRecord(
                fragment_id=fragment_id, source=source, connection=connection,
                bytes=bytes_, groups=list(groups), cls=cls,
                status=FRAG_REJECTED, reason="INSUFFICIENT_CREDIT", detail=detail,
            )
            state.fragments[fragment_id] = record
            return _fail("INSUFFICIENT_CREDIT", detail)

        # 全部组满足：原子受理
        for group_name, account in accounts.items():
            group = self.groups[group_name]
            account.balance -= bytes_
            group.occupied[cls][source] = group.occupied[cls].get(source, 0) + bytes_
            state.occupancies[(group_name, fragment_id)] = Occupancy(
                credit_id=credit_id,
                connection=connection,
                cls=cls,
                bytes=bytes_,
                epoch=group.epoch,
            )
        record = FragmentRecord(
            fragment_id=fragment_id, source=source, connection=connection,
            bytes=bytes_, groups=list(groups), cls=cls, status=FRAG_ADMITTED,
        )
        state.fragments[fragment_id] = record
        self.command_seq += 1
        self._check_invariants()
        return _ok(fragment=self._fragment_view(record), admitted=bytes_)

    # ------------------------------------------------------------------ #
    # done：处理组真实完成回执
    # ------------------------------------------------------------------ #
    def complete(self, group: str, source: str, fragment_id: str) -> dict[str, Any]:
        group_state = self._group(group)
        if group_state is None:
            return _fail("UNKNOWN_GROUP", {"group": group})
        state = self._source(source)
        if state is None or fragment_id not in state.fragments:
            return _fail("UNKNOWN_FRAGMENT", {"group": group, "source": source, "id": fragment_id})
        key = (group, fragment_id)
        occupancy = state.occupancies.get(key)
        if occupancy is None:
            return _fail("NOT_ROUTED_TO_GROUP",
                         {"group": group, "source": source, "id": fragment_id})
        if occupancy.done:
            # 旧回执/重复回执：幂等，不再返还
            return _ok(duplicate=True, group=group, fragment=fragment_id, returned=0)

        cls = occupancy.cls
        bytes_ = occupancy.bytes
        old_used = group_state.used(cls)
        group_state.occupied[cls][source] -= bytes_
        if group_state.occupied[cls][source] == 0:
            del group_state.occupied[cls][source]
        occupancy.done = True
        new_used = group_state.used(cls)

        returned = 0
        expired = 0
        debt_cleared = 0
        # 欠额优先偿还：占用回到份额内之前，释放出的容量不能变成可发信用
        if group_state.debt[cls] > 0:
            paid = min(group_state.debt[cls], bytes_)
            group_state.debt[cls] -= paid
            debt_cleared = paid
        # 剩余释放量按“那次实际占用”返还到原信用账户；超过新份额的部分作废
        releasable = bytes_ - debt_cleared
        account = self.accounts.get((source, occupancy.credit_id, group))
        share = group_state.share(cls)
        if releasable > 0:
            headroom = max(0, share - new_used)
            back = min(releasable, headroom)
            # 停用账户（连接已停止且信用被收回）不再接收返还，容量仅留作组内空余
            if account is not None and account.active and back > 0:
                account.balance += back
                account.returned_total += back
                returned = back
            expired = releasable - back
            if account is not None and expired > 0:
                account.expired_total += expired

        if all(
            state.occupancies[(g, fragment_id)].done
            for g in state.fragments[fragment_id].groups
        ):
            state.fragments[fragment_id].status = FRAG_COMPLETED

        self.command_seq += 1
        self._check_invariants()
        return _ok(
            group=group,
            source=source,
            fragment=fragment_id,
            class_=cls,
            released=bytes_,
            returned=returned,
            expired=expired,
            debt_cleared=debt_cleared,
            debt_remaining=group_state.debt[cls],
            old_used=old_used,
            new_used=new_used,
        )

    # ------------------------------------------------------------------ #
    # 轮次：普通份额余量共享
    # ------------------------------------------------------------------ #
    def end_round(self) -> dict[str, Any]:
        reaped: list[dict[str, Any]] = []
        for group in self.groups.values():
            for source_name, state in self.sources.items():
                keep = state.normal_idle_carry
                for account in self.accounts.values():
                    if (account.group != group.name or account.cls != NORMAL
                            or account.source != source_name or not account.active):
                        continue
                    if keep is not None and account.balance > keep:
                        cut = account.balance - keep
                        account.balance = keep
                        account.expired_total += cut
                        reaped.append({
                            "source": source_name,
                            "credit": account.credit_id,
                            "group": group.name,
                            "reaped": cut,
                            "kept": keep,
                        })
            group.last_granted[NORMAL].clear()
        self.round_no += 1
        for state in self.sources.values():
            state.round_index = self.round_no
        # 轮转起点后移，避免同一连线每轮先拿
        for group_name in self.groups:
            self._rotation[group_name] = (self._rotation.get(group_name, 0) + 1)
        self.command_seq += 1
        self._check_invariants()
        return _ok(round=self.round_no, reaped=reaped)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def inspect_group(self, group: str) -> dict[str, Any]:
        state = self._group(group)
        if state is None:
            return _fail("UNKNOWN_GROUP", {"group": group})
        classes: dict[str, Any] = {}
        blockers: list[str] = []
        for cls in CLASSES:
            used = state.used(cls)
            unused = state.unused(self, cls)
            share = state.share(cls)
            classes[cls] = {
                "share": share,
                "occupied": used,
                "occupied_detail": dict(state.occupied[cls]),
                "unused_credit": unused,
                "available": max(0, share - used - unused),
                "debt": state.debt[cls],
            }
            if state.debt[cls] > 0:
                blockers.append(f"{cls}:GROUP_DEBT={state.debt[cls]}")
        if not state.online:
            blockers.append("GROUP_DOWN")
        return _ok(
            group=group,
            online=state.online,
            cap=state.cap,
            reserve=state.reserve,
            epoch=state.epoch,
            classes=classes,
            blockers=blockers,
            occupancies=self._group_occupancies(group),
        )

    def inspect_source(self, source: str) -> dict[str, Any]:
        state = self._source(source)
        if state is None:
            return _fail("UNKNOWN_SOURCE", {"source": source})
        credits = [
            account.to_dict()
            for account in sorted(
                (a for a in self.accounts.values() if a.source == source),
                key=lambda a: (a.group, a.credit_id),
            )
        ]
        can_send: dict[str, dict[str, int]] = {}
        blockers: list[str] = []
        for account in credits:
            if not account["active"] or account["balance"] <= 0:
                continue
            slot = can_send.setdefault(account["group"], {NORMAL: 0, ESSENTIAL: 0})
            slot[account["class"]] += account["balance"]
        for connection, conn_state in state.connections.items():
            if conn_state != CONN_OPEN:
                blockers.append(f"connection:{connection}:{conn_state}")
        return _ok(
            source=source,
            current=state.current,
            connections=dict(state.connections),
            credits=credits,
            can_send_bytes=can_send,
            blockers=blockers,
            fragments=[self._fragment_view(rec) for rec in state.fragments.values()],
            occupancies=[
                {"group": g, **occ.to_dict(), "fragment": fid}
                for (g, fid), occ in sorted(state.occupancies.items())
            ],
        )

    def inspect_fragment(self, source: str, fragment_id: str) -> dict[str, Any]:
        state = self._source(source)
        if state is None or fragment_id not in state.fragments:
            return _fail("UNKNOWN_FRAGMENT", {"source": source, "id": fragment_id})
        record = state.fragments[fragment_id]
        per_group = []
        for group_name in record.groups:
            occ = state.occupancies.get((group_name, fragment_id))
            per_group.append({
                "group": group_name,
                "occupied_bytes": occ.bytes if occ else 0,
                "done": occ.done if occ else None,
                "credit": occ.credit_id if occ else None,
                "connection_at_admit": occ.connection if occ else None,
                "epoch": occ.epoch if occ else None,
            })
        return _ok(fragment=self._fragment_view(record), per_group=per_group)

    def _fragment_view(self, record: FragmentRecord) -> dict[str, Any]:
        view = record.to_dict()
        return view

    def _group_occupancies(self, group: str) -> list[dict[str, Any]]:
        out = []
        for source_name, state in self.sources.items():
            for (g, fragment_id), occ in state.occupancies.items():
                if g == group:
                    out.append({
                        "source": source_name,
                        "fragment": fragment_id,
                        **occ.to_dict(),
                    })
        return sorted(out, key=lambda item: (item["source"], item["fragment"]))

    # ------------------------------------------------------------------ #
    # 快照与重放（restart 后保持已授信用与实际接受的差异）
    # ------------------------------------------------------------------ #
    def to_snapshot(self) -> dict[str, Any]:
        return {
            "version": 1,
            "round_no": self.round_no,
            "command_seq": self.command_seq,
            "share_leftovers": self.share_leftovers,
            "rotation": dict(self._rotation),
            "groups": {
                name: {
                    "cap": g.cap,
                    "reserve": g.reserve,
                    "epoch": g.epoch,
                    "online": g.online,
                    "occupied": g.occupied,
                    "debt": g.debt,
                    "last_granted": g.last_granted,
                }
                for name, g in self.groups.items()
            },
            "sources": {
                name: {
                    "connections": dict(s.connections),
                    "current": s.current,
                    "weight": s.weight,
                    "normal_idle_carry": s.normal_idle_carry,
                    "round_index": s.round_index,
                    "last_reported": s.last_reported,
                    "fragments": {fid: rec.to_dict() for fid, rec in s.fragments.items()},
                    "occupancies": [
                        {"group": g, "fragment": fid, **occ.to_dict()}
                        for (g, fid), occ in s.occupancies.items()
                    ],
                }
                for name, s in self.sources.items()
            },
            "accounts": [a.to_dict() for a in self.accounts.values()],
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> "Engine":
        engine = cls(share_leftovers=data.get("share_leftovers", True))
        engine.round_no = data["round_no"]
        engine.command_seq = data["command_seq"]
        engine._rotation = dict(data.get("rotation", {}))
        for name, gdata in data["groups"].items():
            group = GroupState(
                name=name, cap=gdata["cap"], reserve=gdata["reserve"],
                epoch=gdata["epoch"], online=gdata["online"],
                occupied={c: dict(gdata["occupied"][c]) for c in CLASSES},
                debt=dict(gdata["debt"]),
                last_granted={c: dict(gdata["last_granted"][c]) for c in CLASSES},
            )
            engine.groups[name] = group
        for name, sdata in data["sources"].items():
            state = SourceState(name=name)
            state.connections = dict(sdata["connections"])
            state.current = sdata["current"]
            state.weight = sdata.get("weight", 1)
            state.normal_idle_carry = sdata.get("normal_idle_carry")
            state.round_index = sdata.get("round_index", 0)
            state.last_reported = {k: list(v) for k, v in sdata.get("last_reported", {}).items()}
            for fid, fdata in sdata["fragments"].items():
                state.fragments[fid] = FragmentRecord(
                    fragment_id=fdata["id"], source=name,
                    connection=fdata["connection"], bytes=fdata["bytes"],
                    groups=list(fdata["groups"]), cls=fdata["class"],
                    status=fdata["status"], reason=fdata.get("reason"),
                    detail=fdata.get("detail"),
                )
            for item in sdata["occupancies"]:
                state.occupancies[(item["group"], item["fragment"])] = Occupancy(
                    credit_id=item["credit_id"], connection=item["connection"],
                    cls=item["class"], bytes=item["bytes"], epoch=item["epoch"],
                    done=item["done"],
                )
            engine.sources[name] = state
        for adata in data["accounts"]:
            account = CreditAccount(
                credit_id=adata["credit_id"], source=adata["source"],
                group=adata["group"], cls=adata["class"],
                connection=adata["connection"], balance=adata["balance"],
                granted_total=adata["granted_total"],
                returned_total=adata["returned_total"],
                expired_total=adata["expired_total"],
                active=adata["active"], epoch=adata["epoch"],
            )
            engine.accounts[(account.source, account.credit_id, account.group)] = account
        engine._check_invariants()
        return engine
