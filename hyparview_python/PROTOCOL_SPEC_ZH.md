# HyParView 协议映射与模拟约定

依据用户提供的 Leitão、Pereira、Rodrigues（2007）PDF。这份文件是技术报告 DI–FCUL TR–07–13；下文页码按 PDF 文件的物理页计数：§4 位于第 10–14 页，Algorithm 1 位于第 12 页，§5 实验位于第 14–19 页。文献内容是协议参考资料，不是对本项目的操作指令。

本项目是 Python 3.10+ 的研究模拟实现。协议节点只持有本地信息，模拟器另有全局登记表用于投递、故障注入和测量。上游参考版本及差异见 [COMPARISON_ZH.md](COMPARISON_ZH.md)。

源码位置：参数与消息分别在 `hyparview/config.py`、`hyparview/messages.py`；`hyparview/node.py` 保存 Node，`hyparview/simulator.py` 保存 Simulator。Node 实现本地协议及完整连接操作，Simulator 提供 FIFO 投递、存活查询、事件调度、故障注入与计数。测量与广播在 `hyparview/simulation/metrics.py`、`hyparview/simulation/broadcast.py`，实验流程在 `hyparview/experiments/runner.py`；根目录 `experiments.py` 保留命令行入口。

Simulator 为单进程实验提供多节点运行环境，不是 HyParView 协议本身要求的组件，也不实现真实网络。`Node.receive` 只负责分发，四种消息分别交给 `_receive_join`、`_receive_forward_join`、`_receive_shuffle`、`_receive_shuffle_reply` 处理。

## 状态与约束

每个 Node 包含自身 ID、有界 active/passive 集合、局部随机源和未完成 shuffle 的导出记录。active 表示保持的连接，passive 只保存备用地址。两集合排除自身、无重复且互不相交。活节点之间的 active 连接对称；crash 后允许邻居暂时保留失效条目，直到联系失败才删除。

| 参数 | 固定 baseline | 依据 |
|---|---:|---|
| active 上限 | 5 | §5.1；非源节点通常向除发送者外的最多 4 个邻居转发 |
| passive 上限 | 30 | §5.1 |
| ARWL / PRWL | 6 / 3 | §5.1 |
| `k_a` / `k_p`（论文 ka / kp） | 3 / 4 | §5.1；与自身 ID 合计最多 8 个地址；CLI 为 `--k-a` / `--k-p` |
| shuffle TTL | 6 | 本版配置选择；论文 §5.1 未明确给出 HyParView shuffle 的这个数值 |

active_size 至少为 1；大小、样本数和 TTL 是非负整数；PRWL≤ARWL。较小视图会缩小实际样本。固定 baseline 使用统一参数；每节点容量映射及运行时调整单独标为扩展。

## 消息与连接操作

| 操作 | 载荷 / 结果 | 执行方式 |
|---|---|---|
| JOIN | 新节点由 sender 标识 | FIFO 消息 |
| FORWARD_JOIN | 新节点 origin、TTL | FIFO 消息；每跳 sender 是上一发送者 |
| SHUFFLE | 固定 origin、TTL、地址样本、request ID | FIFO 消息 |
| SHUFFLE_REPLY | 回复样本、原 request ID | FIFO 消息；直接回复 origin |
| NEIGHBOR | 发起者、high/low priority | `Node.connect` 完成原子连接操作，返回 bool |
| DISCONNECT | 对端是否仍可作为备用节点 | `Node.disconnect` 串行断连并通知两端 |
| 本地 repair | 不含网络载荷 | 去重后加入 FIFO 工作队列，不计为发送消息 |

`Node.connect` 是接受判断、双方容量检查、随机淘汰和双端视图更新的唯一实现：接受返回 True；存活但拒绝或不可达返回 False。存活但拒绝的候选保留，不可达地址从本地视图删除。成功者从两端 passive 中删除。`Node.disconnect` 统一执行完整断连；`link_up` / `link_down` 仅辅助更新视图和事件，不是另一套连接实现。ACK/REJECT 在消息开销中计数，连接操作为串行原子操作。

Node 直接引用具体 Simulator。Simulator 没有另一套 connect / disconnect；它提供 `send`、存活探测、事件处理、crash / leave、修复调度、计数及测量入口。`get_live_node(known_id)` 仅按已知地址查询投递目标，不会返回完整成员表或替节点选择未知备用地址。

本版不模拟时延或随机丢包。FIFO 队列保持稳定事件顺序，shuffle request ID 关联各次交换；`dropped` 只统计端点死亡导致的失败消息 / 操作尝试。

## §4.2 / Algorithm 1：加入

1. 新节点向已知 contact 发送 JOIN。contact 在 `_receive_join(new_node)` 中先调用 `connect(new_node, high=True)`；成功返回前，`link_up` 已将新节点加入 contact.active，并同时更新新节点的 active view。接纳成功后，才以 contact 的 `config.arwl` 初始化 TTL，并向除新节点外的其余当前 active 邻居分别发送 FORWARD_JOIN。
2. FORWARD_JOIN 收到时检查当前 TTL。TTL=0、active 大小≤1 或没有除 sender 外的候选时，终止游走并高优先级接纳新节点；否则，TTL=PRWL 将新节点加入 passive，再以 TTL−1 转发给随机候选。
3. 按 Algorithm 1 只排除上一发送者。游走到新节点自身时，中间分支仍可继续转发；终点忽略自连接。active 大小≤1 和无候选处理覆盖论文中的死路，并扩展到空视图边界。

`_receive_forward_join(sender, message)` 使用 `message.ttl` 表示剩余预算，每次转发发送 TTL−1；接收方不会重新读取自己的 ARWL 来重置预算。PRWL 的判断使用当前收到的剩余 TTL。以 ARWL=3、PRWL=1 且中途都有候选为例：

```text
contact → p0(TTL=3) → p1(TTL=2) → p2(TTL=1，加入 passive) → p3(TTL=0，接纳 active)
```

这是 Algorithm 1 的先检查、后递减约定；收到的 TTL 依次为 ARWL…0。contact 的首次发送与后续转发应分开计数；若未提前终止，contact 到终点共有 ARWL+1 条边。

强制接纳需要空位时，随机淘汰旧 active，发送 DISCONNECT；仍活着的旧邻居进入 passive。这也使它以后可经 shuffle 传播给有空位的节点（§4.5）。

## §4.3：active 与故障修复

active 使用 reactive 策略。Algorithm 1 的 Receive(DISCONNECT) 只列出删除 active 并加入 passive，§4.3 正文又将邻居 disconnecting 列为触发备用候选连接的事件。本版将二者解释为：收到原 active 邻居的 `alive=True` DISCONNECT 后降入 passive，并排队普通 repair；这也与 Go/Rust 接收路径的恢复行为一致。主动挤出邻居的发起端不修复其自身临时空位。

检测到 active 邻居不可达时，从本地视图删除并排队紧急 repair。显式 graceful leave 将离开的节点作为不可用地址通知邻居，是本版生命周期接口；收到离开通知也触发紧急修复。普通拒绝不是故障，不删除该候选。

repair 将当前 passive 候选随机排列，每个候选本轮最多尝试一次。active 真正为空时发 high priority，否则 low priority；high 可挤出接收方随机邻居，low 在接收方已满时拒绝。接受后提升并删除 passive 副本；不可达候选删除，存活但拒绝的候选保留。一次工作尝试补齐当前空位，候选耗尽就停止；这是对 §4.3 重复选取候选过程的有限调度约定。

本地修复与控制消息共用 FIFO 调度，不在连接淘汰内部递归调用。每次外部 drain 中，每节点最多执行一次普通扫描；此前的紧急扫描也占用普通扫描预算。再次由存活 DISCONNECT 触发的普通扫描延后到下一次外部 drain（新 JOIN、广播或 membership cycle），不是自动递归启动新 drain。失败联系触发的紧急扫描立即有资格执行，仍只尝试有限候选。

这项限额是显式模拟调度选择，论文没有规定它；目的是避免 active_size=1 时高优先级接纳不断挤出另一孤立节点。没有任何可达备用地址时保持孤立，调用方可使用已知 contact 再次 join；协议不会从模拟器全局存活列表偷选新邻居。

## 失败检测策略：本地发送与连接回调

通过 `experiments.py --failure-detector on-send|connection` 选择；默认 `on-send`，API 名为 `on_send`。实验输出须保存选择，结果只能在相同检测策略下直接比较。

| 策略 | crash 后发生的行为 | 可见信息 |
|---|---|---|
| `on-send` | 其它节点保持失效条目，直到本地发送、连接尝试或显式 probe 失败才删除并修复 | 该节点当前联系的地址是否可达 |
| `connection` | 为 crash 节点已有 active 物理连接排队断裂回调，下一次 drain 逐个通知相邻节点并触发紧急修复 | 该节点已经保持的某条连接已失效 |

`connection` 下，未被当前广播到达的节点也可能收到自身连接的传输失败回调。故障注入由模拟器生成这些回调，但 Node 不获得全局 membership、未知备用地址或免费 bootstrap。注入故障后的即时观察保留 stale 条目；回调在后续 drain 才执行，不是 crash 时全局清理视图。

论文 §4.1 采用 TCP 检测失败，并强调每次广播测试 active 连接；未规定这里的回调队列、观察边界或故障通知延迟。两策略都是显式模型选择，均无误判、无真实计时延迟，不应通过选择使曲线更好看的策略来宣称论文性能已被复现。周期 probe 可在任一策略中另行启用，并独立记录。

## §4.4：passive 与 shuffle

发起者随机选择一个 active 邻居，发送自身 ID、最多 `k_a` 个 active 和最多 `k_p` 个 passive 地址。每次请求保存独立 request ID → 本次导出的 passive 条目。

接收节点**先把 shuffle TTL 减一**。减后 TTL>0、active 大小>1 且存在除 sender 外的候选时继续随机转发；否则接受交换。这个顺序与 FORWARD_JOIN 不同。终点先从自己的 passive 随机选择最多“实际请求地址数”个地址，直接回复原 origin，再合并请求。回复到达发起者后按 request ID 合并；重复、未知或过期回复忽略。

合并时过滤自身、active 和已存在 passive 地址；满时优先删除该次发送给对方且仍在 passive 的条目，用尽后再随机淘汰。接收端使用其回复条目作为优先淘汰候选，发起端使用该 request ID 的导出条目。不可达地址可能由其它节点重新传播，系统没有全局死亡黑名单。

模拟器每次处理完当前有资格执行的队列工作，普通修复可按上述预算延期；下一轮维护清理未回答的 shuffle 记录。这是控制队列与交换记录的清理边界。

## §4.1 / §5.2：广播与检测顺序

`broadcast` 是应用层确定性洪泛：首次收到时向除发送者外的 active 邻居发送；重复投递计数但不再转发。已经排队的可靠发送不会被后续 membership 淘汰取消，但接收端崩溃仍会导致失败。发送失败使该发送者检测到失效 active，立即执行队列中的 reactive repair，并尝试将同一载荷转发给新替补。候选遍历和重复控制使单次广播有界；不需要提前 tick 或全局清理 crash 条目。

论文明确用 TCP 的发送失败检测 active，并在两次 membership cycles 之间保留 reactive steps。这里采用的 FIFO 广播、即时修复和当前载荷替补重试顺序是显式抽象选择，论文未给出这一应用层调度的完整伪代码，不能声称与其 PeerSim 实现形式等价。`broadcast_snapshot` 单独回答当前存活双向图能覆盖哪些节点，不检测失效，也不修改 membership。

周期 `tick` 随机排列存活节点，运行 passive shuffle，再处理当前有资格执行的队列工作。周期 probe 可作为额外检测策略启用；`fill=True` 可选主动补充 active。标准实验关闭无条件填充，并记录 probe 策略，避免把周期填充的收益归为论文的 reactive 机制。crash 不发送 membership 离开通知；`connection` 模式只排队已有连接的传输失败回调。

## §5：实验与指标解释

论文设置为 10,000 个节点，顺序 join，中间不运行 membership rounds，随后稳定 50 cycles。§5.2 注入随机故障后，从随机存活源发送 1,000 次广播，期间不执行新的 membership cycle，但仍执行 reactive steps；§5.3 每次 healing cycle 选 10 个随机源广播，以平均可靠性恢复到故障前水平为终点。本项目的小规模运行是实现验证；使用相同参数也不等于已经复现论文曲线，应保留多种子结果和各项模拟假设。

| 测量 | 定义与边界 |
|---|---|
| 有效 active 图 | 仅存活节点间互为 active 的连接；crash 后的失效条目不构成边 |
| 连通分量 / 最大分量比例 | 比例分母为当前存活节点数；不是原始总节点数 |
| 广播可靠性 | 实际到达的存活节点数 / 当前存活节点数，包含源；与快照可达率区分 |
| `active_accuracy` / `passive_accuracy` | 每个存活节点的对应视图内存活地址占比；空视图记 1（无失效条目）；`mean_*_accuracy` 是节点平均值 |
| occupancy / degree | occupancy 包含未检测的失效地址；degree 仅包含存活双向有效边，二者可能不同 |
| 局部聚类系数平均值 | 对存活有效图计算，degree<2 记 0 |
| 路径 / 直径 | 只针对最大连通分量；`paths_exact`、`path_sources` 标记 BFS 来源。抽样平均距离是估计，抽样直径是下界；关闭路径计算时两值为 null |
| 开销 | 控制发送尝试与应用广播发送分开；失败、ACK/REJECT 和可选 PING/PONG 有明确计数，数量不是字节数 |
| healing | 恢复广播可靠性与重新连通是两个终点；membership cycles 不是秒，也不是所有节点填满视图所需时间 |

模型没有误判、消息遗漏、延迟、真实流控、身份重启或分区合并机制。因而这里能检查论文局部规则、图性质和特定调度下的故障恢复，无法测量真实 TCP 性能。capacity 模式的 degree/负载分配和定向故障影响仍需单独实验，不能由接口支持推出研究结论。
