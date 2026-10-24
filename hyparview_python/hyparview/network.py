"""Node 所依赖的最小网络接口。

Node 只通过这几个方法接触外部世界：按地址找到节点、发送消息、探测、
排队本地修复、记录统计。Simulator 是一种实现；单元测试可以传入几行代码的
假网络，直接调用 Node 的方法。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .messages import Message
    from .node import Node


class Network(Protocol):
    def lookup(self, node_id: str) -> Node | None:
        """Resolve a KNOWN address to a live node, or None if unreachable.

        This models a successful / failed connection attempt; it never
        supplies unknown peers, so it is not a global membership oracle.
        """

    def send(self, source: str, target: str, message: Message) -> None:
        """Queue a message for asynchronous delivery."""

    def pingpong_probe(self, source: str, target: str) -> bool:
        """Optional keepalive check; True if target is reachable."""

    def schedule_repair(self, source: str, *, urgent: bool = True) -> None:
        """Queue local active-view repair; not a transmitted message."""

    def record(self, source: str, kind: str) -> None:
        """Count one control-message attempt of this kind sent by source."""

    def record_drop(self, kind: str) -> None:
        """Count one control message that could not be delivered."""
