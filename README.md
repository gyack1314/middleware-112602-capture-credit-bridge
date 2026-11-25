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
