# 基线记录：batch 日志统计任务

固定任务：统计 `sandbox/batch/*.log` 中**大写 ERROR** 的行数，逐文件列表 + 总数，写入 `report.md`。
标准答案 **37 行**（oracle：`grep -c ERROR sandbox/batch/*.log` 求和）。
诱饵：每文件另有 4 行小写 `error`（`WARN retry error count=N`），全量 80 行，用于验证大小写口径。

## 七次 run 对照

| run | 代码版本 | 模型 | 步 | 调用 | tokens | 最终上下文 | artifact | 调用耗时 | 正确率 |
|---|---|---|---|---|---|---|---|---|---|
| 1117-163110-be1d | v2 原版：read_file 全文 | deepseek-v4-flash | 13 | 67 | 159,553 | 69,030 | 24 | 377.6s* | 37/37 |
| 1117-171323-b2e1 | + findstr 计数 | deepseek-v4-flash | 8 | 11 | 29,813 | 16,772 | 3 | 302.4s* | 37/37 |
| 1117-181712-3456 | + show_lines（单文件路径失败） | deepseek-v4-flash | 8 | 22 | 42,388 | 25,640 | 0 | 25.1s | 37/37 |
| 1117-183850-6076 | + 单文件修复 | deepseek-v4-flash | 7 | 26 | 23,605 | 14,493 | 0 | 12.3s | 37/37 |
| 1117-184451-2e3d | 旧码 + 描述引导（错配） | deepseek-v4-flash | 9 | 12 | 35,332 | 17,313 | 1 | 20.0s | 37/37 |
| 1117-185106-02d5 | 描述 + 单文件 + 阈值 1200 | deepseek-v4-flash | 7 | 9 | 18,444 | 9,331 | 1 | 10.6s | 37/37 |
| 1117-205653-db0d | 零值自证 + 无进展守卫 | mimo-v2.6-flash | 6 | 8 | 15,792 | 10,737 | 1 | 213.2s | 37/37 |

\* 含调试器断点阻塞，墙钟不可比。干净可比区间：run3 → run6，tokens −56%、调用 −59%、耗时 −58%。

**跨模型只比「步」和「调用」两列。** run7 换成了小米 MiMo，分词器不同、且是思考模型
（reasoning 3,288 字符占上下文 31%），tokens / 上下文 / 耗时三列与 run1–6 不可比。

## 每次改动的因果

| 改动 | 效果 | 证据 |
|---|---|---|
| 全文搬运 → findstr 计数 | 工具返回 40k→6.6k 字符，tokens −81% | 数据获取从 44 次 read_artifact 降到 2 次聚合 |
| 增加 show_lines 行模式 | 设计正确但实现有 bug：单文件路径 `rglob` 返回空 | 模型 12 次调用全失败，降级 read_file，tokens +42% |
| 单文件路径支持 | 抽样核对一次调用完成 | 20 次单文件调用全成功，tokens −44%（vs run3） |
| description 加优先级引导 | 目录聚合成为首选策略 | 调用 26 → 9，上下文 14.5k → 9.3k |
| 零值自证（全目录 0 命中时给分母+变体） | 0 命中不再需要模型自己去求证 | run7 的 `findstr(Error)` 返回「已扫描 20 文件、540 行；另有 117 行大小写不同」 |
| 零值自证扩展到「部分文件为 0」 | 目录总数 >0 时个别文件的 0 也有分母 | `findstr(batch, ERROR)` 输出 356→535 字符，末尾追加「另 4 个文件零命中…共 108 行已完整扫描」，并给出一条大小写变体样例。**预期消除 run7 那次 `read_file(log_02)`，待 run8 验证** |
| 无进展守卫 | 本次**未触发**（每步都在换参数，无重复指纹） | `no_progress=0`，messages 中无注入记录 |
| artifact 摘要加「不要为确认反复读取」 | 1 个 artifact 产生但**未被读回** | run5 同位置会多 2 步，run7 直接据摘要判断 |
| 换模型 → mimo-v2.6-flash | 步 7→6，调用 9→8 | 一次 assistant 消息返回 2–3 个并行 tool_call（step3=2 个，step4=3 个） |
| 上一版 .env 只补缺 → 改成覆盖 | 项目配置不再被系统遗留变量顶掉 | 伪造 deepseek 环境变量后四常量仍取 .env 值 |

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

- **`--self-test` 会写进真实的 `runs/`**：20:39–20:55 之间留下 15 个 `tokens=0` 的空 run，
  和真实基线混在同一层目录里，按时间排序时干扰判断。自检产物应写到 `runs/_selftest/`
  或临时目录。这也是"注入提示"被误认为发生在 run7 的原因。
- `read_file` 只按行数截断（`lines[:max_lines]`），没有字符预算，传 `max_lines=100` 读 27 行文件仍会超 1200 触发落盘（run6 唯一的 artifact 来源）。修法：加字符上限，与 `ARTIFACT_THRESHOLD` 对齐。
- `list_files` 与 `findstr` 目录模式信息重叠，可省 1 次调用（219 字符）。run7 仍先调了 `list_files`。
- 剩余 token 大头已不是工具返回，而是**每步重发全量历史**：run6 最后 3 步占 63% 的 tokens。
- 思考模型的 `reasoning_content` 被 append 进历史并每步重发（run7 共 3,288 字符，占上下文 31%）。
  MiMo 接受回传，所以能跑；但它同时污染 `ctx_chars` 指标，且每步都要重付一遍思考文本的钱。
  修法：append 前 `d.pop("reasoning_content", None)`，但会多一个变量，需单独开一次 run 验证。

## run7 的行为细节（mimo-v2.6-flash）

- 6 步 / 8 调用（1 list_files + 1 目录计数 + 2 并行核对 + 3 并行变体 + 1 写报告），
  其中 step3、step4 是并行 tool_calls，这是调用数从 9 降到 8、步数从 7 降到 6 的原因。
- 策略与 deepseek 版本一致：`findstr(ERROR)` 拿分布 → `show_lines` 抽样核对 → `findstr(error)`
  量小写诱饵 → `findstr(Error)` 确认第三种写法 → 写报告。小写 80 行一次命中，未误计。
- 单步耗时 58.2 / 71.1 / 23.3 / 22.5 / 20.8 秒，前两步占了 129 秒。思考模型的首包延迟
  是主要成本，与循环逻辑无关；要压时间只能换非思考模型或降思考档位。
- 逐文件 20 项与 `grep -c ERROR` 逐项一致，总数 37 一致。
- 无进展守卫**未触发**（8 个调用指纹互不相同，`no_progress` 始终为 0）。触发守卫的
  是 20:55 那次 `--self-test`：假模型连发三次相同 `list_files`，第 3 步注入提示，符合设计。
  两者不要混为一谈。
