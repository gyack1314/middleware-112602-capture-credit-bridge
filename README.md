# 采集接入的信用额度交接与流控恢复

额度按合成整数 byte 数计算。识别来源内连接代次及同源片段 id。

## 输入消息

每个 JSONL 文件是一段独立输入记录。每行包括整数秒点 at、消息名称 kind 与对象 record。at 代表中间件收到消息的时刻，同一秒内按行序提交。业务时间和对应回执引用写在 record 中。

- **pool**　处理组 group 的 cap 和保留必达 reserve。
- **open**　source 的 connection 建连。
- **credit**　source 申请对 groups 指定 class 的 bytes。
- **fragment**　用 connection 指定 credit 送 id、bytes 和 groups。 每个实际受理片段按完整 bytes 分别占用所列各必需组；迟到的重复投送并非新的占用。
- **done**　指定 group 对 id 报告消费完成。
- **switch**　source 提出换到 next 连接，prev 指老身份。
- **stopped**　来源确认指定旧连接已无在途发送，同时给已实际提交的末尾片段。
- **restart**　服务重启。
- **inspect**　查 group 或 source。

## 示例

- split-confirmation-and-cap-shrink.jsonl
- switch-with-unused-credit.jsonl

## 读取演练资料

运行环境为 Python 3.11，只使用标准库。在根目录运行以下命令可核对文件格式及读取接口。

    python3 scenario_reader.py examples
    python3 -m unittest discover -v

资料读取器会按文件统计各类参与方消息的数量。演练记录只描述各参与方交给系统的输入。

---

# 流控中间件 flowcontrol

`flowcontrol/` 是实现上述口径的采集流控中间件：按处理组**真实可接能力**向各采集端发放
信用额度，处理连接迁移、回执确认、缩容欠额、必达份额与重启恢复。只依赖标准库。

## 记账模型

每个处理组（group）有真实上限 `cap`，其中 `reserve` 字节预留给必达（essential）标记，
其余 `cap - reserve` 为普通（normal）份额。两类份额各自独立记账，对任意一类恒有：

    真实占用 O + 已授未用信用 U <= 该类份额
    组的真实可授余量 = 份额 - O - U

- **信用按真实能力授予**：`credit` 对每个必需组各开一个子账户，授予量不超过该组当类余量；
  多组申请按组分别计算，可部分授予，结果里逐组给出 `granted/requested`。
- **片段多组原子受理**：`fragment` 按完整 bytes 同时占用所列每个必需组；任一组余额不足则
  整段拒绝、不产生任何占用，并在 `detail.admissible` 给出每组当前还能接多少，提示拆片段。
- **回执只返还实际占用组**：`done` 把字节返还到“那次占用”的原信用账户；一个组的完成回执
  不会连带返还另一个组。重复回执幂等（no-op，不再返还）。
- **缩容与历史欠额**：再次 `pool` 即重配（配置代次 epoch 递增）。若真实占用超过新份额，
  记录 `debt` 欠额并在该组该类限制新受理；完成回执先还欠额，回到份额内之后才重新产生
  可发信用。超出新份额的未用信用立即作废（`expired`），旧回执无法灌大新配置。
- **必达独立份额**：essential 只吃 `reserve`，与普通份额互不占用；但组断开（GROUP_DOWN）
  时必达同样不能绕过该组实际容量。同一来源的普通类被欠额限制时，必达类与其它独立处理链
  继续工作。
- **普通来源公平共享**：多个活跃来源时，轮内按站点 `weight` 切分每来源上限，防止某条连线
  独占；`end-round` 结束轮次，超过站点配置 `normal_idle_carry` 的未用普通信用被回收共享，
  轮转起点后移。只有单一活跃来源时可吃满真实余量。

## 连接迁移两阶段

1. `switch`：旧连接进入 `switching`（结局未定），其已授信用与占用原样保留；新连接立即可用，
   但只能申请组内剩余真实容量。
2. `stopped`：来源确认旧连接无在途并上报末尾片段 `last`（未知片段 id 会被拒绝）。此后未用
   信用可转给当前连接（默认 `transfer=true`）或收回（`transfer=false`）。
3. 旧连接晚到的**重复**片段只返回原受理状态、不扣新连接信用；旧连接上的**新**片段一律拒绝
   （CONNECTION_STOPPED）。

## 重启恢复

所有变更命令追加写入状态目录的 `journal.jsonl`，周期性落 `snapshot.json`。`restart` 从
快照加日志尾部重放，完整保持“已授信用与实际接受的差异”、连接代次状态与欠额。

## 运行入口

### 1. 跑演练资料（JSONL）

    python3 -m flowcontrol.cli run-scenario examples/split-confirmation-and-cap-shrink.jsonl \
        --state-dir .state/s1
    python3 -m flowcontrol.cli run-scenario examples/switch-with-unused-credit.jsonl \
        --state-dir .state/s2
    python3 -m flowcontrol.cli run-scenario examples/switch-with-unused-credit.jsonl \
        --state-dir .state/s2 --json   # 逐条结构化结果

### 2. 值班查询（片段 / 来源 / 处理组）

    python3 -m flowcontrol.cli inspect --state-dir .state/s1 --group g2
    python3 -m flowcontrol.cli inspect --state-dir .state/s2 --source a
    python3 -m flowcontrol.cli inspect --state-dir .state/s2 --source a --fragment f

查询回答三件事：现在能送多少（`available` / `can_send_bytes`）、究竟哪些字节已占用
（`occupied_detail` / 每片段 `per_group`）、为什么暂不可继续（`blockers`：GROUP_DOWN、
GROUP_DEBT、连接代次状态等；拒绝类结果带稳定原因码，见下）。

### 3. 单条命令与重启自检

    python3 -m flowcontrol.cli cmd --state-dir .state/demo --kind pool \
        --record '{"group":"g1","cap":12,"reserve":3}'
    python3 -m flowcontrol.cli restart --state-dir .state/demo   # 重建并比对快照

### 4. 本地仿真 HTTP 入口

    python3 -m flowcontrol.cli serve --state-dir .state/serve --port 8080
    # 另一终端：仿真采集端/处理组客户端
    python3 -m flowcontrol.simulator --http http://127.0.0.1:8080

路由（POST JSON，record 与演练契约同形）：`/pool /open /credit /fragment /done /switch
/stopped /group-status /end-round /restart /inspect`，另有 `/healthz`。成功返回 200，
业务拒绝返回 409 并带原因码。

### 5. 进程内综合演练（覆盖全部口径，含断言）

    python3 -m flowcontrol.simulator            # 临时状态目录，打印每一步
    python3 -m flowcontrol.simulator --state-dir .state/demo

## 原因码

| 原因码 | 含义 |
| --- | --- |
| UNKNOWN_GROUP / UNKNOWN_SOURCE / UNKNOWN_CONNECTION | 参与方未登记 |
| CONNECTION_EXISTS / CONNECTION_STOPPED / CONNECTION_NOT_OPEN | 连接代次不允许该操作 |
| UNKNOWN_CREDIT / CREDIT_EXISTS / CREDIT_INACTIVE / CREDIT_CLASS_MISMATCH | 信用账户问题 |
| INSUFFICIENT_CREDIT | 超出当次信用，整段不受理（附每组可接字节） |
| GROUP_DOWN | 处理组断连，必达也不能绕过实际容量 |
| GROUP_DEBT | 缩容历史欠额未清，该组该类限制新受理 |
| UNKNOWN_LAST_FRAGMENT / UNKNOWN_FRAGMENT / NOT_ROUTED_TO_GROUP | 片段引用问题 |
| BAD_POOL_CONFIG / BAD_CREDIT_REQUEST / EMPTY_GROUPS | 输入校验 |

## 代码结构

    flowcontrol/
      engine.py     纯内存流控引擎（信用、占用、欠额、连接代次、公平轮次、不变量自检）
      journal.py    WAL 事件日志 + 快照，restart 重放
      runner.py     JSONL 演练驱动与摘要输出
      server.py     本地仿真 HTTP 入口（ThreadingHTTPServer，标准库）
      simulator.py  进程内/HTTP 仿真端与综合演示 demo()
      cli.py        命令行入口
    tests/          37 个 unittest 用例，覆盖全部口径与两份演练契约

运行全部测试：

    python3 -m unittest discover -s tests -v
