# perfp — Ascend simulator profiling preprocessor

`perfp` 把 MindStudio / msInsight 生成的 `visualize_data.bin`（几百 MB、内部是
Chrome Trace JSON + 若干二进制记录）转换成 **AI agent 可以直接分析**的产物：

- `summary.md` / `summary.json`：几 KB ~ 几十 KB 的结论摘要（核/pipe 平衡、Top 指令、
  Top 源码行、同步 span、指标语义警告）
- `profile.sqlite`：字典编码后的可查询数据库（事件、指令、源码映射、时间直方图）
- `csv/`：聚合表 + 全量事件流（`events.csv.gz`），方便 grep/awk
- `src/`：从 bin 里提取出的 28 个内嵌源码文件（包括 `kernel.asc`）
- `raw/trace.json.gz`：**原始 timeline 无损保留**（去掉容器尾部的 NUL 填充）

只依赖 Python 3 标准库（`sqlite3`、`json`、`gzip`、`csv`、`struct` 等），
不需要 `pandas` / `pyarrow` / `duckdb`。

## 0.2：面向 agent 的分析入口

- **先读 `overview.md`**：短摘要、数据质量状态、热点线索和下一步查询。
- **时间字段明确分开**：事件起始范围与包含 X 事件结束的覆盖范围都不是
  真机算子耗时；`device_task_duration_us` 保持 `null`，不从模拟器推算。
- **机器可读语义与诊断**：`summary.json.metric_semantics` 和 `quality` 提供
  指标解释、执行计数差值/比例/每核明细、未配对同步及缺失源码栈数量。
- **多 kernel 批处理**：`--all` 处理目录下全部 `visualize_data.bin`，生成
  `index.md` / `index.json`；单个失败仍保留其他结果，但命令返回非零。
  不加 `--all` 时，多份输入会报错，避免漏看 kernel。
- 输入 SHA-256 写入元数据及摘要，用于确认分析对象；禁止输出覆盖输入目录。

兼容性：保留 `trace.wall_us` 作为 **已弃用的事件起始范围别名**，过滤元数据事件。
新代码应使用下表中的明确字段，不应把旧字段当作 wall-clock 时间。

| 字段 | 含义 |
|---|---|
| `trace.event_start_range_us` | 非元数据事件的 `MAX(ts)-MIN(ts)` |
| `trace.event_coverage_us` | 包含 X 事件 `ts+dur` 的覆盖范围；其他事件使用 `ts` |
| `trace.device_task_duration_us` | 此模拟器输入无法提供，固定为 `null` |
| `pipes[].share` | 累计指令延迟占比，不是利用率或关键路径占比 |
| `quality.status` | `ok` / `caution`：已实现的结构/计数检查结果，不是物理模型准确度保证 |

计数不一致的可能涉及生产端统计口径、映射或解析；工具不会据此断言丢失事件。
`cycles` 的真实时钟含义尚未验证，不能换算微秒。热点排名不等于已证明的性能瓶颈。

## 0.3：DMA / 等待 / A-B 分析

0.3 增加三类面向「DMA 是否该更早搬」的分析。它们都基于**模拟器时间线**，
不能替代真机带宽或真机任务耗时。

### 1. 按核、按流水线的空档与等待归因

- `pipe_segments`：每个 core+pipe 的合并活动区间（X 事件）。
- `pipe_gaps`：活动区间之间的内部空档；`attributed_us` 是与该 pipe 的
  消费者 WAIT 重叠的部分，`unattributed_us` 明确标为未知。
- `wait_attribution`：按 `group_id` 配对 `WAIT_FLAG` / `SET_FLAG`，并把
  SET 标记替换为同 core+pipe 上**实际执行的那条 X 指令**（通常是 MTE2/MTE3
  搬运），因此能回答「哪一行计算因等哪一笔 DMA 晚开始了多少时间」。

`delay_us` 是 WAIT span 内被阻塞的部分；无法配对的等待保持 `NULL` 和
`unknown_no_producer` 状态，不会当作 0。

### 2. 按源码 / 缓冲区统计搬运字节

- `dma_events`：每条 MTE2/MTE3 X 事件的推算块大小、方向、规范化 detail、
  重复序号、地址步长，以及与同核非 DMA 活动的重叠时间。
- 字节优先取 `detail` 的显式字段；否则用静态指令的
  `Process Bytes / Instructions Executed` 推算。无法推算时保持 `NULL`，
  `device_measured_bytes` 永远是 `null`。
- `classification` 只给出 `duplicate_candidate` / `padding_candidate` /
  `unclassified`。重复读取是「同 PC + 同规范化 detail」的启发式，不是
  数据无用或可删除的证明。

### 3. 按阶段对齐的 A/B 对比

```bash
perfp compare bundle_a bundle_b -o compare_out \
    --device-us-a 12.3 --device-us-b 11.9
```

输出 `compare.md` / `compare.json`，同屏给出搬运字节、与计算重叠时间、
消费者等待时间、DMA 空档及可选的**独立真机任务耗时**。阶段按
`kernel.asc` 的精确 `file:line` 对齐；行号变化不会自动匹配。

---

## 快速开始

```bash
# 输入可以是 case 目录、OPPROF 目录，或直接指向 visualize_data.bin
./perfp /path/to/smoke_p03 -o perfp_out

# 多 kernel / 多次调用：分别处理，并生成顶层索引
./perfp /path/to/OPPROF -o perfp_batch --all

# 只看容器里有哪些记录
./perfp info /path/to/smoke_p03

# 最小产物（不保留 raw timeline / 不提取源码 / 不导出全量事件 CSV）
./perfp /path/to/visualize_data.bin -o perfp_out \
        --no-raw --no-src --no-events-csv

# 已存在输出目录时覆盖
./perfp /path/to/case -o perfp_out --force

# A/B：两个已构建的 bundle，可附带独立真机耗时
./perfp compare perfp_a perfp_b -o perfp_compare \
        --device-us-a 12.3 --device-us-b 11.9

# 查询
sqlite3 perfp_out/profile.sqlite
```

### 常用参数

| 参数 | 说明 |
|---|---|
| `-o, --out DIR` | 输出目录，默认 `perfp_out` |
| `--bucket-us F` | 时间直方图桶宽，默认 `0.5` us |
| `--gap-us F` | 最小 pipe 内部空档，默认 `0.1` us |
| `--top N` | 摘要里的 Top-N 行数，默认 30 |
| `--force` | 覆盖非空输出目录 |
| `--no-raw` | 不保留原始 timeline |
| `--raw-plain` | 原始 timeline 写成 `trace.json` 而不是 `.gz` |
| `--no-src` | 不提取内嵌源码 |
| `--no-events-csv` | 不导出全量 `events.csv(.gz)` |
| `--events-csv-plain` | 全量事件写成 `events.csv` 而不是 `.gz` |
| `--quiet` | 不打印进度 |

---

## 输出目录

```
perfp_out/
  overview.md               # agent 短入口：质量、热点与下一步查询
  README.md                 # 这份 bundle 的 schema / 用法（英文，方便 agent）
  summary.md                # 给人 / LLM 看的摘要
  summary.json              # 同样的数字，机器可读
  schema.sql                # SQLite DDL
  queries.sql               # 16 条常用查询
  profile.sqlite            # 可查询数据库
  csv/
    cores.csv  pipes.csv  names.csv
    code_stacks.csv  exec_locations.csv  kernel_locations.csv
    instructions.csv  instruction_core.csv  code_lines.csv
    files.csv  file_lines.csv
    timeline_hist.csv  spans.csv  top_events.csv
    pipe_segments.csv  pipe_activity.csv  pipe_gaps.csv
    wait_attribution.csv
    dma_by_source.csv  dma_by_kernel_source.csv  dma_by_buffer.csv
    dma_by_instruction.csv  dma_duplicates.csv  dma_events.csv
    cores_simlog.csv        # 如果找到了 *.sim.log
    events.csv.gz           # 全量事件流（字典 id）
  src/
    MANIFEST.csv            # 原始绝对路径 -> 本地相对路径
    root/workspace/kernel.asc
    usr/local/Ascend/...    # 被内嵌的 CANN 头文件
  raw/
    trace.json.gz           # 原始 timeline（严格合法 JSON）
    records/
      files.json            # 记录 #29 原样
      instructions.json     # 记录 #30 原样
```

在当前 smoke_p03 样本上：输入 313 MB，输出约 115 MB（其中 sqlite 80 MB、
raw timeline 16 MB），构建耗时约 13 秒。

---

## 容器格式（逆向结果，已逐字节校验）

`visualize_data.bin` 是 **31 条记录顺序拼接**，不是单个 JSON：

```
每条记录 12 字节头（小端）:
    u32 payload_len | u32 reserved(0) | u8 type | u8 subtype | 2B magic(末字节 0x5A)
payload_len 字节 payload
type == 1（内嵌源码）后面再跟 4096 字节 0 填充
```

| type | 含义 | payload 结构 |
|---:|---|---|
| 1 | 内嵌源码文件 | `"绝对路径\0"` + 0 填充到 4096 + 文件内容 |
| 2 | Chrome Trace Event JSON | `{"displayTimeUnit":...,"traceEvents":[...]}` |
| 3 | `Files` 表 | 28 个文件 × 行级 38 核统计 |
| 4 | `Instructions` 表 | 2354 条静态指令 × 38 核指标 + schema |

`perfp info <input>` 会打印这张记录表。

---

## SQLite schema

主要表：

| 表 | 行数（本样本） | 说明 |
|---|---:|---|
| `meta` | key/value | 构建信息 |
| `cores` | 38 | 核名、事件数、累计 `dur` |
| `pipes` | 25 | pipe 名、事件数、累计 `dur` |
| `names` | ~2.5k | 事件名（多数是指令 mnemonic） |
| `code_stacks` | 980 | 完整的多行 `args.code` 调用栈 + inner/outer 位置 + 聚合 |
| `locations` | 261 | 单行 `file:line`（指令源码映射） |
| `details` | 82k | `args.detail` 字符串 |
| `pcs` | 2354 | `args.pc_addr`，聚合 |
| `events` | 636,694 | `ts/dur/ph` + 各种字典 id |
| `spans` | 10,108 | 配对的 B/E（同步 flag）真实时长 |
| `instructions` | 2354 | 记录 #30 的静态指令表 |
| `instruction_core` | 89,452 | 指令 × 核计数器 |
| `files` / `file_lines` | 28 / ~600 | 记录 #29 的行级统计 |
| `timeline_hist` | small | 0.5 us 粒度的时间直方图 |
| `pipe_segments` | per core+pipe | 合并后的 X 活动区间 |
| `pipe_gaps` | per core+pipe | 内部空档、归因/未知时长、重叠 pipe |
| `wait_attribution` | per WAIT span | 配对的 producer、实际 DMA 事件、delay |
| `dma_events` | per MTE X event | 推算字节、方向、buffer、重复、重叠 |

所有 `*_id` 都是小整数，join 到对应字典表即可拿到文本。

便利视图：

- `v_instruction`：指令 + 每核计数器 + trace 累计耗时
- `v_code_line`：按 `file:line` 汇总指令计数
- `v_code_line_core`：同上，按核
- `v_exec_location`：按调用栈**最内层**（实际执行的 CANN 原语）汇总 trace 耗时
- `v_kernel_location`：按调用栈**最外层**（用户 `kernel.asc` 行）汇总 trace 耗时
- `v_pipe_activity` / `v_pipe_gap`：core+pipe 活动、空档与等待归因
- `v_wait_attribution`：WAIT/SET 配对、delay、producer/consumer 源码行
- `v_dma_source` / `v_dma_kernel_source`：按内层 / kernel 源码行汇总 DMA
- `v_dma_buffer` / `v_dma_instruction`：按 buffer 形状、指令汇总 DMA
- `v_dma_duplicate`：重复读取候选及估算冗余字节

---

## 指标语义（重要）

- `events.dur` 是模拟器给出的单条指令延迟。跨 pipe / 跨核求和会**重叠**，
  因此可能大于 wall time；它适合做**相对热点**，不是实际耗时。
- `instruction_core.cycles` 是模拟器内部计数，**不是核时钟周期**，不能换算成
  微秒。`perfp` 会在 `summary.md` 里自动检测并给出警告（本样本约 4.9e4
  cycles/us，明显不是真实频率）。
- `spans.dur` 是匹配到的 `E.ts - B.ts`，是模拟器时间线上同步区间的跨度，
  不应称为真机时长。负跨度、缺失时间和未配对端点会计入质量诊断。
- 旧样本中：`X` 事件 605,420 + `B/E` span 10,108 = 615,528 = `Instructions
  Executed` 总和。这不是所有生产端版本都成立的恒等式；不一致时输出量化诊断。
- `pipe_gaps.dur` 只统计同一 core+pipe **内部**活动之间的空档，不含首次搬运前/最后一次搬运后的尾部时间。`attributed_us` 是与该 pipe 的消费者 WAIT 的交集；其余是未知，不能称为「空闲可优化」。
- `wait_attribution.delay_us` 是 WAIT 内被阻塞的模拟器时间。无法配对时保持 `NULL`，不填 0。
- `dma_events.block_bytes` 是静态 `Process Bytes / calls` 或 detail 字段的推算值，不是真机流量；`device_measured_bytes` 永远为 `null`。`duplicate_candidate` 只是同 PC + 同规范化 detail 的启发式。

---

## 设计要点

1. **流式/低内存**：trace 记录一次性解码为字符串后用 `json.JSONDecoder`
   逐事件解析（636k 事件约 2–5 秒），峰值内存约 2× JSON 大小。
2. **字典编码**：`args.code` 原本占 163 MB（52.8%），但只有 980 个不同值；
   `pc_addr` 只 2354 个；`detail` 82k 个。全部归一化为整数 id，SQLite 因此
   从 313 MB 降到 80 MB 且可索引查询。
3. **不丢信息**：`raw/trace.json.gz` 保留完整 timeline；`raw/records/*.json`
   保留 #29/#30 原样；`src/` 保留内嵌源码；事件表保留 detail/pc 引用。
4. **面向 agent**：先读 `summary.md`（20 KB），再用 SQL 下钻；
   `queries.sql` 提供现成查询；`README.md` 描述 schema 和指标陷阱。
5. **零第三方依赖**：只用到标准库，`pip install` 不需要联网。

---

## 限制

- 容器格式是针对当前 msInsight 产物的逆向结果。若华为后续变更记录类型或
  头布局，`perfp` 会在魔数/长度校验处报错，而不是静默产出错误数据。
- 目前按 `type` 识别记录：1=源码、2=trace、3=Files、4=Instructions。未知
  类型会被跳过（`info` 里仍可见）。
- 只解析 `traceEvents` 数组；外层的 `displayTimeUnit/profilingType/schemaVersion`
  保留在 raw 文件里，没有单独建表。
- 一个容器中出现多份 trace / Instructions / Files 记录时会拒绝分析，避免静默忽略后续记录。

## 测试

```bash
# 独立生成不同形态的容器，不需要 CANN 或旧样本
python3 -m unittest -v test_agent_features.py

# 可选：原始大样本的固定计数回归；没有样本会明确跳过
PERFP_SAMPLE=/path/to/original/smoke_p03 python3 test_perfp.py

# 0.3 的 pipe gap / wait / DMA / compare 回归
python3 -m unittest -v test_analysis_features.py
```
