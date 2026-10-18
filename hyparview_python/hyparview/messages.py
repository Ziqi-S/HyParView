"""节点之间交换的成员管理消息。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    kind: str
    origin: str = ""
    #sender: str = "" #TODO: 这个字段需要吗? --> 通过receive(sender, message)传入
    ttl: int = 0
    sample: tuple[str, ...] = () # 本次消息携带的node_id
    request_id: int = 0

