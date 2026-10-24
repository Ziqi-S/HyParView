"""HyParView 理想仿真的公共接口。

节点协议位于 node.py；Node 只依赖 network.py 中的 Network 接口；
仿真运行器 simulator.py 是该接口的一种实现。
"""
from ._version import __version__
from .config import Config
from .messages import Message
from .network import Network
from .node import Node
from .simulator import Simulator

__all__ = ["Config", "Message", "Network", "Node", "Simulator", "__version__"]
