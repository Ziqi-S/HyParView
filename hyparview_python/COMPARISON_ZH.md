# 上游实现比较与本版取舍

检查日期：2026-10-11。本次读取了两仓库的实际源码，并固定到以下提交；没有运行上游程序，也没有宣称完成跨语言等价验证。

| 参考实现 | 检查版本 | 许可证 |
|---|---|---|
| [HashiCorp / Go](https://github.com/hashicorp/hyparview/tree/78300343c5e317836f18470fa8e6311e3dfbae6b) | `78300343c5e317836f18470fa8e6311e3dfbae6b` | [MPL-2.0](https://github.com/hashicorp/hyparview/blob/78300343c5e317836f18470fa8e6311e3dfbae6b/LICENSE) |
| [sile / Rust](https://github.com/sile/hyparview/tree/05a33a3335e98712d2b7ff340075a64cf5d77b3d) | `05a33a3335e98712d2b7ff340075a64cf5d77b3d`，Cargo 版本 0.1.3 | [MIT](https://github.com/sile/hyparview/blob/05a33a3335e98712d2b7ff340075a64cf5d77b3d/LICENSE) |

本版根据论文独立编写，没有复制上游代码。选择是：借鉴 Rust 的协议状态与外部动作分离、可注入随机源；借鉴 Go 的邻居接受/拒绝反馈、遍历备用候选及 shuffle 优先替换已发送条目。最终协议规则以论文为依据。具体映射和模拟假设见 [PROTOCOL_SPEC_ZH.md](PROTOCOL_SPEC_ZH.md)。

Python 的 `hyparview/node.py` 保存 Node，本地协议与完整连接操作都在这里；`hyparview/simulator.py` 保存 Simulator，为实验提供节点登记、可靠 FIFO 投递、存活查询、事件调度、故障注入与计数。Simulator 是实验环境，不是协议本身的组成要求。`hyparview/simulation/` 保存测量和广播，`hyparview/experiments/` 组织演示、实验与统计。`Config`、`Message`、`Node`、`Simulator` 和 `__version__` 从 `hyparview` 导入；根目录 `demo.py`、`experiments.py` 保留原入口。

## 结构与默认参数

| 项目 | Go | Rust | 本 Python 版 |
|---|---|---|---|
| 协议执行 | 同步 `Send` 接口，直接处理错误 | `VecDeque<Action>`，调用方执行动作 | Node 直接引用 Simulator；控制消息与失败修复使用 FIFO 队列 |
| 连接反馈 | 返回拒绝或 error | NEIGHBOR 消息与 Disconnect 动作 | `Node.connect` 原子更新双方，返回 bool；拒绝与不可达均为 False，不可达地址另从本地清除 |
| 随机源 | 全局 Go 随机函数 | 泛型 RNG，由调用方传入 | 每节点独立 `random.Random`；候选先排序 |
| active / passive 上限 | 5 / 30 | 4 / 24 | 固定 baseline 为 5 / 30 |
| ARWL / PRWL | 6 / 3 | 5 / 2 | 6 / 3 |
| ka / kp | 3 / 4 | 2 / 2 | `k_a=3` / `k_p=4`；CLI 为 `--k-a` / `--k-p` |
| shuffle TTL | 独立字段，默认 6 | 使用 ARWL，默认 5 | 独立字段，默认 6；这是本版选择 |

Go 的 `CreateView(..., n)` 没有根据 n 计算容量。两上游默认参数不同，比较时须统一配置，不能把连接预算差异归因于编程语言。[Go 配置](https://github.com/hashicorp/hyparview/blob/78300343c5e317836f18470fa8e6311e3dfbae6b/hyparview.go#L25-L45)、[Rust 配置](https://github.com/sile/hyparview/blob/05a33a3335e98712d2b7ff340075a64cf5d77b3d/src/node_options.rs)

## 影响协议行为的源码观察

| 机制 | 上游观察 | 本版处理 |
|---|---|---|
| 视图互斥 | Go `AddActive` 未统一删除 passive 副本；Rust 在 active 插入时集中删除 | `connect` 统一检查容量和自连接；`link_up` 更新本地 active 并删除 passive 副本 |
| JOIN 游走 | Go 排除 sender 与新节点；Rust 只排除 sender | 按 Algorithm 1 只排除 sender；中间节点继续有界转发，终点忽略自连接；degree≤1 或无候选时结束 |
| shuffle 发起者 | Go `NewShuffle` 将 `Origin` 赋为 `from`，忽略传入 origin，转发后改变发起者 | origin 在整个游走中保持，回复发给真实发起者 |
| shuffle TTL | Go 在 TTL≥0 时继续转发；Rust 在收到 TTL=0 时执行交换 | 按 §4.4 先减一，再判断是否大于零；无候选时执行交换 |
| shuffle 死路 | 两者某些路径在无转发候选时直接返回 | 接受请求并回复，不丢弃交换 |
| 回包数量 | Go 按接收方配置的总样本数；Rust 按实际请求长度 | `min(实际请求长度, 接收方 passive 大小)` |
| shuffle 淘汰 | Go 优先淘汰发送条目；Rust 满时直接随机淘汰 | 先替换本次发出的 passive 条目，再随机淘汰 |
| 回复关联 | Go 仅保存单个 `LastShuffle`；Rust 无独立导出记录 | request ID 对应各自导出条目；重复或已过期回复忽略 |
| 空 active 判断 | Go `IsEmpty()` 为长度≤1，影响高优先级请求和 shuffle | 仅长度=0 才视为孤立并发送高优先级 NEIGHBOR |
| 失败处理 | Go 包装器可将任意失败消息改投替代节点；Rust 由外部驱动报告失败 | 失败联系删除本地条目并排队修复；应用广播的替补重试单独建模 |

Python 的 `receive` 将四种消息分派给独立处理函数。`_receive_join` 先经 `connect → link_up` 接纳新节点，再以联系节点的 ARWL 初始化 FORWARD_JOIN；接收方只递减消息携带的剩余 TTL，不重置 ARWL。PRWL 按当前 TTL 判断。具体接收序列和例子见 [PROTOCOL_SPEC_ZH.md](PROTOCOL_SPEC_ZH.md)。

上述观察可定位到 [Go 协议核心](https://github.com/hashicorp/hyparview/blob/78300343c5e317836f18470fa8e6311e3dfbae6b/hyparview.go)、[Go NewShuffle](https://github.com/hashicorp/hyparview/blob/78300343c5e317836f18470fa8e6311e3dfbae6b/message.go#L125-L133)、[Go ViewPart](https://github.com/hashicorp/hyparview/blob/78300343c5e317836f18470fa8e6311e3dfbae6b/part.go)、[Go 故障恢复](https://github.com/hashicorp/hyparview/blob/78300343c5e317836f18470fa8e6311e3dfbae6b/failure.go)、[Rust 协议核心](https://github.com/sile/hyparview/blob/05a33a3335e98712d2b7ff340075a64cf5d77b3d/src/node.rs)。这是静态分析，不代表已经复现每个上游问题。

Go [README](https://github.com/hashicorp/hyparview/blob/78300343c5e317836f18470fa8e6311e3dfbae6b/README.org) 记录了模拟中的非对称连接及某些失败恢复条件下的栈溢出。Rust [Node 文档](https://github.com/sile/hyparview/blob/05a33a3335e98712d2b7ff340075a64cf5d77b3d/src/node.rs#L11-L23) 提醒：概率协议可能分区，上层需要在发现分区后重新 join。拥有固定 active 上限并不能保证连通。

## 论文 baseline 与模拟选择

论文 §4 使用可靠 TCP 连接，active 由加入、断连、故障等事件触发变化，passive 周期 shuffle。Algorithm 1 的 Receive(DISCONNECT) 只展示降入 passive；§4.3 正文则将邻居 disconnecting 列为触发修复的情况。本版采用“收到仍存活邻居的 DISCONNECT 后降入 passive，并排队修复”的解释，两上游的接收路径也会尝试补充 active。主动淘汰方不修复其自身临时空位；检测到失败或收到离开通知时排队紧急修复。周期维护默认不主动填满 active；`fill=True` 是可选扩展。

对应上游接收路径是 [Go RecvDisconnect](https://github.com/hashicorp/hyparview/blob/78300343c5e317836f18470fa8e6311e3dfbae6b/hyparview.go#L145-L155) 与 [Rust handle_disconnect](https://github.com/sile/hyparview/blob/05a33a3335e98712d2b7ff340075a64cf5d77b3d/src/node.rs#L245-L252)。此处明确记录对正文与简略伪代码的解释，不把额外调度规则归入 Algorithm 1。

修复通过 FIFO 本地工作执行。每次外部发起的 drain 中，每节点至多执行一次普通修复扫描；已执行的紧急扫描也占用普通扫描预算。再次由存活 DISCONNECT 触发的普通修复延后到下一次外部 drain（JOIN、广播或 membership cycle）。失败联系触发的紧急扫描仍立即有资格执行，各扫描只尝试有限候选。这个调度限额是本版模拟选择，用于避免 active 上限为 1 时高优先级连接持续互相挤出；不是论文规定的限额。

`Node.connect` / `Node.disconnect` 是完整连接操作的唯一实现，包含接受判断、随机淘汰和双端视图更新；Simulator 没有重复的连接规则。`move_up` / `link_down` 只是内部记账辅助。Node 只通过 `Network` 接口接触外部；Simulator 的 `lookup(known_id)` 查询已知地址，不给 Node 提供完整成员表或新联系人。可选的周期 probe 是额外探测策略，不是论文必需的周期消息。

本版不模拟包时延或随机丢包；FIFO 队列保持稳定执行顺序，request ID 关联 shuffle 交换。`dropped` 只记录端点死亡导致的失败尝试。连接作为串行原子操作完成，不能用此模型测量真实网络时延或握手竞争。

失败检测分为两种明确记录的策略。默认 `on-send`（API 名 `on_send`）仅在本地发送、连接尝试或显式 probe 失败时发现失效；未被当前广播到达的节点可能尚未检测到失败邻居。`connection` 将 crash 节点已有 active 物理连接的断裂通知排队，在下一次 drain 向各个相邻节点投递本地传输失败回调；这些节点即使还没有收到广播，也能修复。crash 到回调处理之前仍保留原有失效条目，便于观察 stale state。

两种策略都抽象可靠 TCP，但对关闭事件何时可见作出不同假设；论文没有给出本模拟器的具体回调时序。`connection` 不向 Node 提供全局成员列表、未知地址或额外 bootstrap。它是无误判、无真实时延的传输模型选择，不能为了匹配论文曲线就默认解释为更准确的实现。实验使用 `experiments.py --failure-detector on-send|connection` 指定并保存策略；对比 baseline 与 capacity 时须采用同一策略，必要时分别报告两种模型下的结果。

应用 `broadcast` 在发送时发现失败，立即处理排队修复，并把同一广播载荷发送给替补连接；不必等下一个 membership cycle。论文说明广播测试 active 连接并触发 reactive steps，但没有规定这里采用的逐发送修复/重试顺序；该顺序是显式模拟选择，未证明与 PeerSim 实现形式等价。`broadcast_snapshot` 只测量当前有效图的洪泛，不更新 membership。

异构 capacity 与运行时 active 上限变化属于研究扩展。固定 baseline 和 capacity 实验应采用相同传输、探测、调度和故障策略，并记录各自连接预算。上限不等于实际 degree，也不保证消息负载正比于 capacity。本文档不把小规模演示或单个种子结果解释为论文性能复现。
