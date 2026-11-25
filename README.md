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

---

## 快速开始

```bash
# 输入可以是 case 目录、OPPROF 目录，或直接指向 visualize_data.bin
./perfp /path/to/smoke_p03 -o perfp_out

# 只看容器里有哪些记录
./perfp info /path/to/smoke_p03

# 最小产物（不保留 raw timeline / 不提取源码 / 不导出全量事件 CSV）
./perfp /path/to/visualize_data.bin -o perfp_out \
        --no-raw --no-src --no-events-csv

# 已存在输出目录时覆盖
./perfp /path/to/case -o perfp_out --force

# 查询
sqlite3 perfp_out/profile.sqlite
```

### 常用参数

| 参数 | 说明 |
|---|---|
| `-o, --out DIR` | 输出目录，默认 `perfp_out` |
| `--bucket-us F` | 时间直方图桶宽，默认 `0.5` us |
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
  README.md                 # 这份 bundle 的 schema / 用法（英文，方便 agent）
  summary.md                # 给人 / LLM 看的摘要
  summary.json              # 同样的数字，机器可读
  schema.sql                # SQLite DDL
  queries.sql               # 10 条常用查询
  profile.sqlite            # 可查询数据库
  csv/
    cores.csv  pipes.csv  names.csv
    code_stacks.csv  exec_locations.csv  kernel_locations.csv
    instructions.csv  instruction_core.csv  code_lines.csv
    files.csv  file_lines.csv
    timeline_hist.csv  spans.csv  top_events.csv
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

所有 `*_id` 都是小整数，join 到对应字典表即可拿到文本。

便利视图：

- `v_instruction`：指令 + 每核计数器 + trace 累计耗时
- `v_code_line`：按 `file:line` 汇总指令计数
- `v_code_line_core`：同上，按核
- `v_exec_location`：按调用栈**最内层**（实际执行的 CANN 原语）汇总 trace 耗时
- `v_kernel_location`：按调用栈**最外层**（用户 `kernel.asc` 行）汇总 trace 耗时

---

## 指标语义（重要）

- `events.dur` 是模拟器给出的单条指令延迟。跨 pipe / 跨核求和会**重叠**，
  因此可能大于 wall time；它适合做**相对热点**，不是实际耗时。
- `instruction_core.cycles` 是模拟器内部计数，**不是核时钟周期**，不能换算成
  微秒。`perfp` 会在 `summary.md` 里自动检测并给出警告（本样本约 4.9e4
  cycles/us，明显不是真实频率）。
- `spans.dur` 是匹配到的 `E.ts - B.ts`，是真实时长。
- 事件分解：`X` 事件 605,420 + `B/E` span 10,108 = 615,528 = `Instructions
  Executed` 总和。这三者对不上时 `perfp` 会给出警告。

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
