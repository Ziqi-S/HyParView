"""HyParView 理想仿真的公共接口。

节点协议位于 node.py；仿真运行器位于 simulator.py。
"""
from ._version import __version__
from .config import Config
from .messages import Message
from .node import Node
from .simulator import Simulator

__all__ = ["Config", "Message", "Node", "Simulator", "__version__"]
