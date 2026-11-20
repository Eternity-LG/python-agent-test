import json,sys,os,time
from pathlib import Path
_runs_env = os.getenv("RUNS")           # .env 里的 RUNS 优先，否则用项目根/runs
if _runs_env:
    _p = Path('../runs').expanduser()
    # 相对路径按项目根解析，不跟着 cwd 跑——否则从别的目录启动会把 runs 建到那里
    RUNS = _p if _p.is_absolute() else (Path(__file__).parent / _p).resolve()
else:
    RUNS = Path(__file__).parent
RUN_DIR: Path = None    # 当前运行的目录，被 read_artifact / write_report 共享
#根据传参路径读取trace.json文件
def read_trace(path):
    with open(path, "r", encoding="utf-8") as f:
        data = [json.loads(line) for line in f if line.strip()]
    return data
def getTracePath(dest):
    trace_path = os.path.join(RUNS, dest, "trace.jsonl")
    if trace_path is None:
        raise FileNotFoundError(f"文件夹中不存在trace.json文件: {dest}")
    return trace_path
if __name__ == "__main__":
    #通过启动参数读取文件夹路径
    run_path ="runs" if len(sys.argv) == 1 else sys.argv[2]
    path = None
    if not os.path.exists(run_path):
        raise FileNotFoundError(f"文件夹路径不存在: {run_path}")
    if "runs"==run_path:
        dests = [
        d for d in os.listdir(run_path)
        if os.path.isdir(os.path.join(run_path, d)) and d.split("-")[0].isdigit()
        ]
        if not dests:
            raise FileNotFoundError("runs 下没有数字开头的文件夹")
        dest = sorted(dests, reverse=True)[0]   # 目录名按时间前缀，降序取最新
        path = getTracePath(dest)
        #取首位文件夹名称
    else:
        path = getTracePath(run_path)
    #获取路径后，读取trace.json文件内容
    data = read_trace(path)
    f'{print(data)}'
    #从trace.json文件中提取需要的数据  
    """
    json 内容模板
    {"event":"step","step":5,"ms":3495,"chars":8675,"tokens":14624,"prompt":4036,"completion":569,"reasoning":129,"cache_hit":3840,"cache_miss":196} 
    """
    cost_breakdown = [i for i in data if i["event"]=="step"]
    result = []
    #for 不能使用i 作为list下标吗？不能，因为i 是一个字典，不能直接用i 来访问list的元素
    for i in range(len(cost_breakdown)):
        if i==0:
            continue
        else:
            result.append({
                "step":cost_breakdown[i]["step"],
                "ms":cost_breakdown[i]["ms"],
                "Δtokens":cost_breakdown[i]["tokens"] - cost_breakdown[i-1]["tokens"],
                "Δprompt":  cost_breakdown[i]["prompt"] - cost_breakdown[i-1]["prompt"],
                "Δcompletion":cost_breakdown[i]["completion"] - cost_breakdown[i-1]["completion"],
                "Δreasoning":cost_breakdown[i]["reasoning"] - cost_breakdown[i-1]["reasoning"],
                "Δcache_hit":cost_breakdown[i]["cache_hit"] - cost_breakdown[i-1]["cache_hit"],
                "Δcache_miss":cost_breakdown[i]["cache_miss"] - cost_breakdown[i-1]["cache_miss"]
            })
    print(cost_breakdown)
    print(result)

    """
    计算后最终的输出结果
        step   ms    Δtokens  prompt  hit/miss    重发   新增   completion(reasoning)   相对成本
        1      1548  1724     1653    1280/373    0      1653   71(0)                   525
        ...
合计      —     25428    —       —                 15,7k  4,7k   2,0k(0.5k)              ...
占比                 —        —                    62%    18%    20%                     —
    """
    #新建一个对象返回最后结果，结果有多条






    