# HyParView Python 仿真重实现

按导师要求提供仿真实现，Python 3.10+，运行时只用标准库。协议行为依据 Leitão 等人（2007）的论文 §4 和 Algorithm 1，参考 HashiCorp 的 Go 实现及 sile 的 Rust 实现，独立编写 Python 代码。上游固定版本、源码差异与取舍见 [COMPARISON_ZH.md](COMPARISON_ZH.md)，逐项论文映射见 [PROTOCOL_SPEC_ZH.md](PROTOCOL_SPEC_ZH.md)。

实现包括有界、对称的 active view，备用 passive view，JOIN / FORWARD_JOIN，带优先级的 NEIGHBOR 接受与拒绝，DISCONNECT，周期 shuffle，以及发送失败触发的即时修复。节点只知道本地地址，模拟器的全局表用于投递与测量。

## 运行

进入项目目录后，无需安装依赖：

```bash
python3 -m unittest discover -s tests -v
python3 demo.py --nodes 100 --seed 42 --mode fixed --output results/demo.json
python3 experiments.py --nodes 100,1000 --seeds 0,1,2 --failure-fractions 0.2,0.5,0.8 --warmup-rounds 50 --broadcasts 100 --output results/experiments.json
```

`demo.py` 演示成员图在周期故障探测后如何恢复；`experiments.py` 在注入故障后连续广播，期间**不运行新的 membership cycle**，用于观察论文 §5.2 强调的 reactive recovery。完整 JSON 保存参数、随机种子、加入联系人、故障名单、每次广播可靠性及分组统计。不同参数或观察窗口分别聚合，未恢复的运行单独计数。

论文规模的单次设置可以这样运行：

```bash
python3 experiments.py --nodes 10000 --seeds 42 --failure-fractions 0.8 --warmup-rounds 50 --broadcasts 1000 --path-samples 32 --output results/large.json
```

这只是对应规模与流程，不代表复现了论文结果。正式比较应使用多个独立种子。`--path-samples exact` 执行完整最短路径计算，10,000 节点时开销较大；默认抽样 32 个源，输出会标记路径估计及直径下界。

故障检测可显式选择 `--failure-detector on-send`（默认）或 `--failure-detector connection`。前者在发送 / keepalive 失败时检测；后者另将现有 active 连接的 TCP 断开作为本地事件，在下一次 drain 通知端点，未收到广播的节点也能开始修复。两个模型都没有误判和实际检测延迟，应按导师的实验假设选择。连接事件不会通知仅在 passive 中保存该地址的节点，不提供全局死亡名单或新联系人。

## 最小例子

```python
from hyparview import Config, Simulator

network = Simulator(seed=42)
a = network.add("A", Config())
b = network.add("B", contact="A")
c = network.add("C")
network.drain()             # 处理 JOIN / FORWARD_JOIN
a.add_passive("C")
network.crash("B")         # 不通知邻居；A 暂时保留失效地址
result = network.broadcast("A")
print(result["fraction"])  # 1.0：检测 B 失败，提升 C 并转发
print(a.active)            # {'C'}
```

`broadcast()` 会驱动失效检测与修复，应用发送数和控制发送数分别计数。`broadcast_snapshot()` 只测量当前有效图的可达范围，不改变协议状态。

## 项目结构与阅读顺序

节点协议与仿真环境分别放在 `node.py`、`simulator.py`，测量和实验工具单独放置：

```text
hyparview/
├── __init__.py          # 对外公开接口
├── _version.py          # 版本号
├── config.py            # Config：协议参数及校验
├── messages.py          # Message：控制消息数据
├── node.py              # Node：本地协议与双向连接操作
├── simulator.py         # Simulator：节点登记、队列、故障与调度
├── simulation/
│   ├── __init__.py
│   ├── metrics.py       # 图性质与视图准确性测量
│   └── broadcast.py     # 应用广播与快照洪泛
└── experiments/
    ├── __init__.py
    ├── demo.py          # 固定上限 / 异构容量演示
    ├── runner.py        # 多规模、多种子、多故障比例实验
    └── statistics.py    # 分组统计与近似置信区间
demo.py                  # 保留的演示脚本入口
experiments.py           # 保留的实验脚本入口
pyproject.toml           # 包发现、版本及安装后的命令入口
tests/                   # 协议、仿真及实验工具测试
```

建议按以下顺序阅读：

1. `hyparview/config.py`、`hyparview/messages.py`：先看参数和消息字段。
2. `hyparview/node.py`：`receive` 只分发消息，具体流程分别在 `_receive_join`、`_receive_forward_join`、`_receive_shuffle`、`_receive_shuffle_reply`；再看 `repair`、`connect` / `disconnect`。
3. `hyparview/simulator.py`：看 FIFO 投递、修复调度、存活查询、故障注入和计数。
4. `hyparview/simulation/metrics.py`、`hyparview/simulation/broadcast.py`：看图测量与应用广播。
5. `hyparview/experiments/runner.py`：最后看实验如何组织；统计方法在同目录 `statistics.py`，单次演示在 `demo.py`。

常用导入为 `from hyparview import Config, Message, Node, Simulator, __version__`。Node 直接引用具体 Simulator；Simulator 负责环境操作，连接规则集中在 Node。`get_live_node(known_id)` 只查询已知地址，不能给节点发现未知成员。根目录 `demo.py` 和 `experiments.py` 保留原来的导入接口及命令行入口，上面的运行命令继续适用。

Simulator 是实验环境：登记节点、投递排队消息、调度恢复并记录计数，方便在一个进程里运行许多 Node。它不是 HyParView 协议要求节点必须拥有的组件，也不代表真实网络。

几个容易混淆的方法可以这样区分：

| 方法 | 负责什么 |
|---|---|
| `Node.connect(peer)` | 完整建立双向连接：判断双方是否接受、必要时淘汰、更新双方视图 |
| `Node.disconnect(peer)` | 完整断开双向连接；`leaving=True` 表示本节点要退出 |
| `Node.link_up` / `Node.link_down` | 只改本节点的视图并记录事件，供上述完整操作或失效通知复用 |
| `Simulator.send` / `Simulator.drain` | 消息入队 / 按顺序交给节点处理，不再实现连接规则 |

建立连接时，由 `connect` 统一安排容量检查和必要的淘汰；`link_up` 只做本地更新。FIFO 只是确定执行顺序，避免断连、修复之间递归调用，不模拟时间流逝。

[PROTOCOL_SPEC_ZH.md](PROTOCOL_SPEC_ZH.md) 记录论文映射与调度约定，[COMPARISON_ZH.md](COMPARISON_ZH.md) 记录上游比较，[VALIDATION.md](VALIDATION.md) 保存实际验证结果。`example_*.json` 是三份 100 节点演示输出。

## 参数

固定 baseline 默认 `active_size=5`、`passive_size=30`、`arwl=6`、`prwl=3`、`k_a=3`、`k_p=4`，来自论文 §5.1。`shuffle_ttl=6` 是本版明确选择，论文未明确给出这一 HyParView 参数。命令行可逐项覆盖，如 `--active-size 5 --passive-size 30 --shuffle-ttl 6 --k-a 3 --k-p 4`。

JOIN 的接收方先调用 `connect(new_node, high=True)`；成功后，双方已通过 `link_up` 加入彼此的 active view。随后才向其它当前 active 邻居发送 FORWARD_JOIN，并在这里用 `arwl` 初始化 TTL。接收 FORWARD_JOIN 的节点使用消息中剩余的 TTL，在转发时减一，不重新设置为自己的 ARWL；TTL=0、degree≤1 或没有下一跳时结束，否则在当前 TTL 等于 PRWL 时记录到 passive，再继续转发。

例如 ARWL=3、PRWL=1，且中途都有下一跳时：

```text
contact → p0(TTL=3) → p1(TTL=2) → p2(TTL=1，记录 passive) → p3(TTL=0，接纳 active)
```

这里按 Algorithm 1 先检查收到的 TTL，再转发 TTL−1；contact 的首次发送与后续转发分开计数，上例从 contact 到终点有 4 条边。SHUFFLE 则在接收时先减一，再判断是否继续。shuffle 回复按 request ID 匹配本次导出的 passive 地址，优先替换这些地址，再随机淘汰；重复、未知或过期回复忽略。

## 仿真约定

- 控制消息与去重的本地修复工作进入 FIFO 队列，保持稳定的执行顺序。`Node.connect` / `Node.disconnect` 完成接受判断、随机淘汰与双端视图更新；连接操作为串行原子操作，ACK/REJECT 计数。`connect` 只返回 bool：接受为 True；存活但拒绝或不可达均为 False，不可达地址另从本地视图清除。
- active 因加入、断连、失败或显式容量修改变化。收到存活 DISCONNECT 后降入 passive 并触发修复，依据论文 §4.3 正文及两份参考实现；主动淘汰方不修复其临时空位。每次 drain 中每节点最多执行一次普通断连修复，重复触发延至下一次外部 drain；失效联系的修复仍立即执行。每次扫描随机遍历 passive，每个候选最多尝试一次；拒绝保留，死亡删除，无可达地址则保持孤立。
- `tick(rounds, probe_active=True, fill=False)` 每轮运行额外的理想 keepalive 探测和 shuffle。可关闭 probe；默认不无条件填充 active。`fill=True` 是显式扩展策略。
- 广播时每个首次接收者向除发送者外的 active 邻居转发。已排队的可靠发送不因随后淘汰连接而取消；接收端崩溃仍会导致失败。发送失败后处理修复，再把当前消息尝试发给替补。这个具体重试顺序是模拟约定，论文没有给出完整应用层调度伪代码。
- 不模拟时延或随机丢包；队列用于安排事件，request ID 用于关联 shuffle。`dropped` 只统计端点死亡导致的失败尝试。也不模拟误判、真实 TCP 流控或身份重启；节点 ID 不复用。crash 后先保留失效条目，按选定检测模型处理本地事件。协议可能保持分区，调用方需要已知 contact 才能重新加入。

因此，本项目可作为论文协议规则的研究仿真基线；测试验证的是这里明确记录的模型，不能证明形式正确性、真实网络性能或上游逐消息等价。

## 输出解释

| 字段 | 含义 |
|---|---|
| `components` / `largest_component_fraction` | 存活、双向 active 图的分量数 / 最大分量占存活节点比例 |
| `degrees` / `active_occupancy` | 有效图的 degree / 本地 active 条目数；后者可能含尚未检测的失效地址 |
| `active_accuracy` / `passive_accuracy` | 各本地视图中存活地址占比，空视图约定为 1；同时提供平均值与失效条目数 |
| `average_path_length_lcc` / `diameter_lcc` | 最大分量的路径均值 / 直径；抽样时分别为估计 / 下界 |
| `paths_exact` / `path_sources` | 路径是否完整计算 / BFS 源数；跳过路径时路径均值和直径为 null |
| `control_attempts_*` | 含失败尝试、ACK/REJECT、额外 PING/PONG 的控制发送次数 |
| `dropped` | 因端点死亡而失败的消息 / 操作尝试；不是随机丢包 |
| `data_attempts_total` | reactive broadcast 的应用发送次数，含失败尝试与重复投递 |
| `first_connected_recovery_round` | 连续广播结束后，图首次重新连通的维护轮次；未恢复为 null |

消息数不是字节数，轮次不是秒。连通恢复不同于论文 §5.3“平均广播可靠性回到故障前水平”的 healing 定义。多种子统计的 95% 区间采用 `mean ± 1.96 × stdev / sqrt(n)`，是探索性正态近似；种子少时估计较弱，单次运行不输出区间。

`average_broadcast_reliability` 先在每个种子内对整个故障后广播序列取均值，再跨种子统计，便于对应论文 §5.2 的平均可靠性测量。最后一次随机源广播的结果单独保存，不应将它当作整个序列的平均值。

## 容量扩展

保留 `demo.py --mode capacity`，外部容量 1/2/4 对应 active 上限 3/4/8；`--failure-mode high-capacity` 优先移除高容量节点。这些映射是示例研究策略，与标准固定上限 baseline 分开。

`node.set_active_size(size)` 缩容时立即随机断开多余连接，扩容时排队尝试填补；调用 `network.drain()` 执行。上限不等于实际 degree，也不保证负载正比于容量。比较时需要记录总连接预算，控制相同故障、联系人和随机源，并使用多种子。

可选 `python3 -m pip install -e .` 安装本地命令 `hyparview-demo` 与 `hyparview-experiments`；直接运行脚本不需要安装。
