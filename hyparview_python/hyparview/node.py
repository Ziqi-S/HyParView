"""HyParView 节点：本地视图、加入、连接、修复与 shuffle。"""
from __future__ import annotations

from dataclasses import replace
import random
from typing import TYPE_CHECKING

from .config import Config
from .messages import Message

if TYPE_CHECKING:
    from .simulator import Simulator


class Node:
    """Local protocol state and complete ideal connection operations.

    Peers are chosen only from local views or an explicitly known contact.
    Simulator.get_live_node resolves a known address; it never supplies peers.
    All choices sort IDs first so seeded runs survive Python hash randomization.
    """

    def __init__(self, node_id: str, simulator: Simulator,
                 config: Config | None = None, seed: int = 0):
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("Node ID must be a nonempty string")
        self.id = node_id
        self.simulator = simulator
        self.config = config or Config()
        self.active: set[str] = set()
        self.passive: set[str] = set()
        self.rng = random.Random(seed)
        self.events: list[tuple[str, str]] = [] #本地邻居新增/删除记录
        self._sequence = 0 #本节点shuffle次数计数器
        self._shuffles: dict[int, tuple[str, ...]] = {} #记录“某次 shuffle 发出去了哪些节点”

    def sample(self, peers, count: int) -> list[str]:
        population = sorted(peers) #先排序，保证可复现性
        return self.rng.sample(population, min(count, len(population)))

    #add single node
    def add_passive(self, peer: str, preferred: list[str] | None = None):
        if (peer == self.id or peer in self.active or peer in self.passive
                or self.config.passive_size == 0):
            return
        if len(self.passive) >= self.config.passive_size:
            # First replace peers exported in THIS shuffle; then random peers.
            candidates = [p for p in (preferred or []) if p in self.passive]
            victim = candidates[0] if candidates else self.sample(self.passive, 1)[0]
            self.passive.remove(victim)
        self.passive.add(peer)

    #merge a list of nodes
    def merge_passive(self, peers, exported=()):
        preferred = list(exported)
        for peer in peers:
            self.add_passive(peer, preferred)

    def accepts(self, peer: str, high: bool) -> bool:
        return peer != self.id and (peer in self.active or high
                                    or len(self.active) < self.config.active_size)

    def make_room(self):
        if len(self.active) >= self.config.active_size:
            self.disconnect(self.sample(self.active, 1)[0])

    def link_up(self, peer: str):
        """只更新本节点；connect 已检查容量并在必要时调用 make_room。"""
        self.passive.discard(peer)
        if peer not in self.active:
            self.active.add(peer) # 容量检查统一在 connect 中完成
            self.events.append(("up", peer))

    def link_down(self, peer: str, alive: bool, *, recover: bool = True): #*表示recover必须通过关键字传递
        """只更新本节点；alive 表示该邻居是否仍可作为备用地址。

        收到断连通知或发现邻居失效时使用；完整双向断连调用 disconnect。
        """
        was_active = peer in self.active
        if peer in self.active:
            self.active.remove(peer)
            self.events.append(("down", peer))
        self.passive.discard(peer) #discard()不会报错，即使peer不在passive中
        if alive:
            self.add_passive(peer)
        if was_active and recover:
            # Section 4.3 treats incoming disconnections as recovery triggers.
            # A local eviction to make room suppresses its own recovery.
            self.simulator.schedule_repair(self.id, urgent=not alive)

    def connect(self, peer: str, high: bool = False) -> bool:
        """完整建立一条双向边：检查双方容量、必要时淘汰、更新双方视图。

        这是理想仿真中的一次原子操作。成功返回 True；存活但拒绝或
        节点已退出时返回 False，后者会从本地备用视图删除。
        """
        simulator = self.simulator
        if peer == self.id or simulator.get_live_node(self.id) is None:
            return False
        simulator._count(self.id, "NEIGHBOR_HIGH" if high else "NEIGHBOR_LOW")
        neighbor = simulator.get_live_node(peer)
        if neighbor is None:
            simulator.dropped["NEIGHBOR"] += 1
            self.link_down(peer, alive=False)
            return False

        # 双方都遵守各自的连接上限；high 允许随机淘汰旧邻居。
        accepted = neighbor.accepts(self.id, high) and self.accepts(peer, high)
        simulator._count(peer, "NEIGHBOR_ACK" if accepted else "NEIGHBOR_REJECT")
        if not accepted:
            return False
        if peer not in self.active:
            self.make_room()
        if self.id not in neighbor.active:
            neighbor.make_room()
        self.link_up(peer)
        neighbor.link_up(self.id)
        return True

    def disconnect(self, peer: str, *, leaving: bool = False):
        """完整断开一条双向边；leaving=True 表示本节点主动离开。

        本地为腾出空位而断开时不立即修复；接收断连通知的邻居排队修复。
        """
        simulator = self.simulator
        simulator._count(self.id, "DISCONNECT")
        neighbor = simulator.get_live_node(peer)
        self.link_down(peer, alive=neighbor is not None, recover=False)
        if neighbor is not None:
            neighbor.link_down(self.id, alive=not leaving)
        else:
            simulator.dropped["DISCONNECT"] += 1

    def join(self, contact: str):
        if contact != self.id:
            self.simulator.send(self.id, contact, Message("JOIN"))

    # 按随机顺序尝试备用节点，直到补满 active 或候选耗尽。
    def repair(self):
        """Bounded candidate sweep, retaining peers that reject low priority.

        A failed active connection schedules this operation immediately in the
        event queue. Visit each candidate at most once so refusals cannot loop.
        试图填补active views的空缺。每个候选节点最多访问一次，以避免拒绝循环。
        """
        for peer in self.sample(self.passive, len(self.passive)):
            if len(self.active) >= self.config.active_size:
                break
            self.connect(peer, high=not self.active)

    def maintenance(self, *, probe_active: bool = True, fill: bool = False):
        """
            1. 检查活跃邻居是否在线。
            2. 清除未完成的旧 shuffle 请求记录。
            3. 发起新的 shuffle。

            probe_active=True, 会通过 PING/PONG 检查所有活跃邻居。这个定时探测是代码加入的模拟策略，不应该直接当作论文中的消息机制。
            fill=True 时，节点会在维护过程中尝试填充其活跃邻居列表，以确保其活跃邻居数量达到配置的上限。
        """
        # Optional keepalives are a simulation policy, not a paper message.
        if probe_active:
            for peer in sorted(self.active):
                if not self.simulator.probe(self.id, peer):
                    self.link_down(peer, alive=False)
        # Simulator drains each round: unanswered requests expire next tick.
        self._shuffles.clear()
        if fill:
            self.simulator.schedule_repair(self.id)
        self.shuffle()

    #TODO: 需要return值吗？
    def shuffle(self) -> int | None:
        if not self.active: # No active peers to shuffle with
            return None
        target = self.sample(self.active, 1)[0] #随机选择这一跳的活跃邻居作为 shuffle 的目标节点
        exported = tuple(self.sample(self.passive, self.config.k_p)) #随机选择 k_p 个被动邻居作为 shuffle 的导出节点
        sample = (self.id, *self.sample(self.active, self.config.k_a), *exported) #*是展开
        self._sequence += 1 #每次 shuffle 都会增加计数器
        self._shuffles[self._sequence] = exported
        self.simulator.send(self.id, target, Message(
            "SHUFFLE", self.id, self.config.shuffle_ttl, sample, self._sequence))
        return self._sequence

    def receive(self, sender: str, message: Message):
        """分派排队的控制消息；每类消息的步骤在对应处理方法中展开。

        在本理想仿真中，NEIGHBOR 接纳和 DISCONNECT 通知由 connect /
        disconnect 原子完成，不再投递同一操作，避免重复更新视图。
        """
        if message.kind == "JOIN":
            self._receive_join(new_node=sender)
        elif message.kind == "FORWARD_JOIN":
            self._receive_forward_join(sender, message)
        elif message.kind == "SHUFFLE":
            self._receive_shuffle(sender, message)
        elif message.kind == "SHUFFLE_REPLY":
            self._receive_shuffle_reply(message)
        else:
            raise ValueError(f"Unknown message kind: {message.kind}")

    def _receive_join(self, new_node: str):
        """Algorithm 1：contact 先接纳新节点，再传播其加入信息。"""
        # 1. 将新节点加入 contact 的 active；满时随机淘汰旧邻居。
        # connect 同时更新双方视图，因此成功后这里已经包含 new_node。
        if not self.connect(new_node, high=True):
            return

        # 2. 给接纳后 active 中除新节点外的所有节点发送 FORWARD_JOIN。
        for peer in sorted(self.active - {new_node}):
            self.simulator.send(self.id, peer, Message(
                kind="FORWARD_JOIN", origin=new_node, ttl=self.config.arwl))

    def _receive_forward_join(self, sender: str, message: Message):
        """按消息携带的剩余 TTL 游走；不在每个节点重置 ARWL。"""
        new_node = message.origin
        remaining_ttl = message.ttl  # 初始值由 contact 设为 ARWL
        choices = self.active - {sender}  # 只排除上一跳，避免立即走回去

        # 1. 剩余 TTL 为零或走到死路：将新节点加入当前节点的 active。
        if remaining_ttl == 0 or len(self.active) == 1 or not choices:
            self.connect(new_node, high=True)
            return

        # 2. PRWL 是剩余 TTL 阈值；到达阈值时将新节点保存到 passive。
        if remaining_ttl == self.config.prwl:
            self.add_passive(new_node)

        # 3. 继续游走时减一。ARWL 的约束已经由剩余 TTL 传递下来了。
        next_hop = self.sample(choices, 1)[0]
        self.simulator.send(self.id, next_hop,
                            replace(message, ttl=remaining_ttl - 1))

    def _receive_shuffle(self, sender: str, message: Message):
        choices = self.active - {sender}
        remaining_ttl = message.ttl - 1  # section 4.4：与 FORWARD_JOIN 不同，先减一
        if remaining_ttl > 0 and len(self.active) > 1 and choices:
            self.simulator.send(self.id, self.sample(choices, 1)[0],
                                replace(message, ttl=remaining_ttl))
        else:
            # 无下一跳时也执行交换；先选回复，再合并收到的地址。
            reply = tuple(self.sample(self.passive, len(message.sample)))
            self.simulator.send(self.id, message.origin, Message(
                "SHUFFLE_REPLY", sample=reply, request_id=message.request_id))
            self.merge_passive(message.sample, reply)

    def _receive_shuffle_reply(self, message: Message):
        exported = self._shuffles.pop(message.request_id, None) # 弹出已发送的 shuffle 请求
        if exported is not None:
            self.merge_passive(message.sample, exported)

    def set_active_size(self, size: int):
        """Experimental bound change: shrink now, queue refill on enlargement."""
        previous = self.config.active_size
        self.config = replace(self.config, active_size=size)
        while len(self.active) > size:
            self.disconnect(self.sample(self.active, 1)[0])
        if size > previous:
            self.simulator.schedule_repair(self.id)

    def check_local(self):
        assert self.id not in self.active | self.passive
        assert not self.active & self.passive
        assert len(self.active) <= self.config.active_size
        assert len(self.passive) <= self.config.passive_size

