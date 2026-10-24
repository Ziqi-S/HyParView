"""HyParView 节点：本地视图、加入、连接、修复与 shuffle。"""
from __future__ import annotations

from dataclasses import replace
import random
from typing import TYPE_CHECKING

from .config import Config
from .messages import Message

if TYPE_CHECKING:
    from .network import Network


class Node:
    """Local protocol state and complete ideal connection operations.

    Peers are chosen only from local views or an explicitly known contact.
    All outside contact goes through self.network (see network.py): lookup
    resolves a known address; it never supplies peers.
    All choices sort IDs first so seeded runs survive Python hash randomization.
    """

    def __init__(self, node_id: str, network: Network,
                 config: Config | None = None, seed: int = 0):
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("Node ID must be a nonempty string")
        self.id = node_id
        self.network = network
        self.config = config or Config()
        self.active: set[str] = set()
        self.passive: set[str] = set()
        self.rng = random.Random(seed)
        self.events: list[tuple[str, str]] = [] #本地邻居新增/删除记录
        self._sequence = 0 #本节点shuffle次数计数器
        self._shuffles: dict[int, tuple[str, ...]] = {} #记录“某次 shuffle 发出去了哪些节点”

    def sample(self, peer_ids: set[str], count: int) -> list[str]:
        """从 peers 中随机选择 count 个节点"""
        neighbors = sorted(peer_ids) #先排序，保证可复现性
        return self.rng.sample(neighbors, min(count, len(neighbors)))

    def add_passive(self, peer_id: str, exported: list[str] | None = None):
        """Add a peer to the passive view, evicting an existing peer if necessary."""
        if (peer_id == self.id or peer_id in self.active or peer_id in self.passive
                or self.config.passive_size == 0):
            return
        if len(self.passive) == self.config.passive_size: # passive view is full
            # Quotation: A node will first attempt to remove identifiers sent to the peer. 意味着不需要disconnect()
            # If no such identifiers remain in the passive view, it will remove identifiers at random.
            candidates = [p for p in (exported or []) if p in self.passive] #TODO: exported改为从self._shuffles观察？
            victim = candidates[0] if candidates else self.sample(self.passive, 1)[0]
            self.passive.remove(victim)

        self.passive.add(peer_id)

    def merge_passive(self, peers_id, received=()):
        """Merge a list of peers into the passive view."""
        for peer_id in peers_id:
            self.add_passive(peer_id, list(received))

    def accepts(self, peer_id: str, high: bool) -> bool:
        """Check if a peer can be accepted into the active view."""
        if peer_id == self.id:
            return False
        if peer_id in self.active:
            return True  #already linked

        # High priority is always accepted (call make_room if full);
        # low priority only if there is a free slot.
        return high or len(self.active) < self.config.active_size

    def make_room(self):
        """Remove a random active neighbor to make room for a new one."""
        if len(self.active) == self.config.active_size:
            self.disconnect(self.sample(self.active, 1)[0])

    def move_up(self, peer_id: str):
        """Add a peer from the passive view to the active view.
        建立 active 连接是双向的，要双方都同意才能成立。容量检查和淘汰旧邻居都要等双方都表态之后才能做，所以不适合放进单边的 move_up()。
        """
        self.passive.discard(peer_id)
        if peer_id not in self.active:
            self.active.add(peer_id) # 容量检查统一在 connect 中完成
            self.events.append(("up", peer_id))

    def handle_peer_disconnection(self, peer_id: str, alive: bool, *, recover: bool = True): #*表示recover必须通过关键字传递
        """ Move a peer from the active view to the passive view, or remove it entirely.
        只更新本节点；alive 表示该邻居是否仍可作为备用地址。
        收到断连通知或发现邻居失效时使用；完整双向断连调用 disconnect。
        """
        is_active = peer_id in self.active

        if is_active:
            self.active.remove(peer_id)
            self.events.append(("down", peer_id))

        self.passive.discard(peer_id)

        if alive:
            self.add_passive(peer_id)

        if is_active and recover:
            # Section 4.3 treats incoming disconnections as recovery triggers.
            # A local eviction to make room suppresses its own recovery.
            self.network.schedule_repair(self.id, urgent=not alive)

    def connect(self, peer_id: str, high: bool = False) -> bool:
        """ add each other to their active view, possibly evicting an existing peer.
        peer_id从self.passive里来
        完整建立一条双向边：检查双方容量、必要时淘汰、更新双方视图。
        这是理想仿真中的一次原子操作。成功返回 True；存活但拒绝或
        节点已退出时返回 False，后者会从本地备用视图删除。
        """
        network = self.network
        neighbor = network.lookup(peer_id)  # 在所有存活peers中查找peer_id对应的节点对象

        if peer_id == self.id or network.lookup(self.id) is None:
            return False

        network.record(self.id, "NEIGHBOR_HIGH" if high else "NEIGHBOR_LOW")#TODO：有必要记录吗？如果neighbor不存在，记录的意义是？

        if neighbor is None: #可能存在但是已经退出了，也有可能不存在
            network.record_drop("NEIGHBOR") #TODO：有必要记录吗？
            self.handle_peer_disconnection(peer_id, alive=False)#不需要recover，因为neighbor已经退出了
            return False

        # 双方都遵守各自的连接上限；high 允许随机淘汰旧邻居。
        accepted = neighbor.accepts(self.id, high) and self.accepts(peer_id, high)
        if not accepted:
            network.record(peer_id, "NEIGHBOR_REJECT")
            return False
        else:
            network.record(peer_id, "NEIGHBOR_ACK")

        # 如果neighbor已经在active中，为什么还需要connect()？可能是因为neighbor在active中，但是本节点不在neighbor的active中，所以需要connect()来建立双向连接。
        if peer_id not in self.active:
            self.make_room()
        if self.id not in neighbor.active:
            neighbor.make_room()
        self.move_up(peer_id)
        neighbor.move_up(self.id)

        return True

    def disconnect(self, peer_id: str, *, leaving: bool = False):
        """完整断开一条双向边；leaving=True 表示本节点主动离开。
        本地为腾出空位而断开时不立即修复；接收断连通知的邻居排队修复。
        """
        network = self.network
        network.record(self.id, "DISCONNECT")
        neighbor = network.lookup(peer_id)

        if neighbor is not None:
            self.handle_peer_disconnection(peer_id, alive=True, recover=False)
            neighbor.handle_peer_disconnection(self.id, alive=not leaving)
        else:
            network.record_drop("DISCONNECT")

    #TODO: neighbor request?
    def join(self, contact: str):
        """Join the network via a known contact node.
        # 这里的 JOIN 消息是单向的，contact 节点收到后会把新节点加入自己的 active，然后再发 FORWARD_JOIN 给自己的其他 active 节点。
        # 新节点自己不会直接加入 active，而是等待 contact 节点的 FORWARD_JOIN 消息。
        """
        if contact != self.id:
            self.network.send(self.id, contact, Message("JOIN"))

    #TODO: 需要return值吗？
    def shuffle(self) -> int | None:
        """发起一次 shuffle，返回请求 ID 或 None（没有活跃邻居）。"""
        if not self.active: # No active peers to shuffle with
            return None

        target = self.sample(self.active, 1)[0] #随机选择这一跳的活跃邻居作为 shuffle 的目标节点
        exported = tuple(self.sample(self.passive, self.config.k_p)) #随机选择 k_p 个被动邻居作为 shuffle 的导出节点
        sample = (self.id, *self.sample(self.active, self.config.k_a), *exported) #*是展开
        self._sequence += 1 #每次 shuffle 都会增加计数器
        self._shuffles[self._sequence] = exported
        self.network.send(self.id, target, Message(
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
            self.network.send(self.id, peer, Message(
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
        self.network.send(self.id, next_hop,
                            replace(message, ttl=remaining_ttl - 1))

    def _receive_shuffle(self, sender: str, message: Message):
        choices = self.active - {sender}
        remaining_ttl = message.ttl - 1  # section 4.4：与 FORWARD_JOIN 不同，先减一
        if remaining_ttl > 0 and len(self.active) > 1 and choices:
            self.network.send(self.id, self.sample(choices, 1)[0],
                                replace(message, ttl=remaining_ttl))
        else:
            # 无下一跳时也执行交换；先选回复，再合并收到的地址。
            reply = tuple(self.sample(self.passive, len(message.sample_ids)))
            self.network.send(self.id, message.origin, Message(
                "SHUFFLE_REPLY", sample_ids=reply, request_id=message.request_id))
            self.merge_passive(message.sample_ids, reply)

    def _receive_shuffle_reply(self, message: Message):
        exported = self._shuffles.pop(message.request_id, None) # 弹出已发送的 shuffle 请求
        if exported is not None:
            self.merge_passive(message.sample_ids, exported)

    # 按随机顺序尝试备用节点，直到补满 active 或候选耗尽。
    def repair(self):
        """ Fill the active view from the passive view, if possible."""
        for peer in self.sample(self.passive, len(self.passive)):
            if len(self.active) == self.config.active_size:
                break
            self.connect(peer, high=not self.active)  # 如果active为空，则high=True，保证至少有一个active neighbor

    def maintenance(self, *, probe_active_view: bool = True, fill: bool = False):
        """
            1. 检查活跃邻居是否在线。
            2. 清除未完成的旧 shuffle 请求记录。
            3. 发起新的 shuffle。

            probe_active_view=True, 会通过 PING/PONG 检查所有活跃邻居。这个定时探测是代码加入的模拟策略，不应该直接当作论文中的消息机制。
            fill=True 时，节点会在维护过程中尝试填充其活跃邻居列表，以确保其活跃邻居数量达到配置的上限。
        """

        if probe_active_view:
            for peer in sorted(self.active):
                if not self.network.pingpong_probe(self.id, peer):
                    self.handle_peer_disconnection(peer, alive=False)
        # The simulator drains each round: unanswered requests expire next tick.
        self._shuffles.clear() #TODO:检查必要性
        if fill:
            self.network.schedule_repair(self.id)
        self.shuffle()

    def set_active_size(self, size: int):
        """shrink now, queue refill on enlargement."""
        previous = self.config.active_size
        self.config = replace(self.config, active_size=size)
        while len(self.active) > size:
            self.disconnect(self.sample(self.active, 1)[0])
        if size > previous:
            self.network.schedule_repair(self.id)

    def check_local(self):
        assert self.id not in self.active | self.passive
        assert not self.active & self.passive
        assert len(self.active) <= self.config.active_size
        assert len(self.passive) <= self.config.passive_size

