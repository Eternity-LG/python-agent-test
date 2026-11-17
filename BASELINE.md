# 基线记录：batch 日志统计任务

固定任务：统计 `sandbox/batch/*.log` 中**大写 ERROR** 的行数，逐文件列表 + 总数，写入 `report.md`。
标准答案 **37 行**（oracle：`grep -c ERROR sandbox/batch/*.log` 求和）。
诱饵：每文件另有 4 行小写 `error`（`WARN retry error count=N`），全量 80 行，用于验证大小写口径。

## 六次 run 对照

| run | 代码版本 | 步 | 调用 | tokens | 最终上下文 | artifact | 调用耗时 | 正确率 |
|---|---|---|---|---|---|---|---|---|
| 1117-163110-be1d | v2 原版：read_file 全文 | 13 | 67 | 159,553 | 69,030 | 24 | 377.6s* | 37/37 |
| 1117-171323-b2e1 | + findstr 计数 | 8 | 11 | 29,813 | 16,772 | 3 | 302.4s* | 37/37 |
| 1117-181712-3456 | + show_lines（单文件路径失败） | 8 | 22 | 42,388 | 25,640 | 0 | 25.1s | 37/37 |
| 1117-183850-6076 | + 单文件修复 | 7 | 26 | 23,605 | 14,493 | 0 | 12.3s | 37/37 |
| 1117-184451-2e3d | 旧码 + 描述引导（错配） | 9 | 12 | 35,332 | 17,313 | 1 | 20.0s | 37/37 |
| 1117-185106-02d5 | 描述 + 单文件 + 阈值 1200 | 7 | 9 | 18,444 | 9,331 | 1 | 10.6s | 37/37 |

\* 含调试器断点阻塞，墙钟不可比。干净可比区间：run3 → run6，tokens −56%、调用 −59%、耗时 −58%。

## 每次改动的因果

| 改动 | 效果 | 证据 |
|---|---|---|
| 全文搬运 → findstr 计数 | 工具返回 40k→6.6k 字符，tokens −81% | 数据获取从 44 次 read_artifact 降到 2 次聚合 |
| 增加 show_lines 行模式 | 设计正确但实现有 bug：单文件路径 `rglob` 返回空 | 模型 12 次调用全失败，降级 read_file，tokens +42% |
| 单文件路径支持 | 抽样核对一次调用完成 | 20 次单文件调用全成功，tokens −44%（vs run3） |
| description 加优先级引导 | 目录聚合成为首选策略 | 调用 26 → 9，上下文 14.5k → 9.3k |

## 复现方法

```bash
cd D:/workspace/PythonProject/test01
python -m pip install -r requirements.txt
export OPENAI_API_KEY=sk-xxx
python agent2.py            # 产物在 runs/<时间戳-id>/
```

指标从 `runs/<id>/trace.jsonl`（每步 tokens/chars/ms）与 `state.json`（`calls` 计数）读取。

## 两条测量纪律

1. **观察与测量分开跑**。debug 断点会污染墙钟（run2 单步 139s），要观察就开断点，要测数据就裸跑。
2. **一次只改一个变量**。run5 同时改了阈值(2000)+描述，性能回退无法归因；run3 同理。

## 已知残余

- `read_file` 只按行数截断（`lines[:max_lines]`），没有字符预算，传 `max_lines=100` 读 27 行文件仍会超 1200 触发落盘（run6 唯一的 artifact 来源）。修法：加字符上限，与 `ARTIFACT_THRESHOLD` 对齐。
- `list_files` 与 `findstr` 目录模式信息重叠，可省 1 次调用（219 字符）。
- 剩余 token 大头已不是工具返回，而是**每步重发全量历史**：run6 最后 3 步占 63% 的 tokens。
