#!/usr/bin/env python3
"""
使用 MLLM (qwen3.6-plus) 对街景图像进行活动适宜性评估。
三种提示策略：zero-shot, few-shot, chain-of-thought
支持断点续传（临时 JSON）和并行调用（限速 30 次/分钟）。
输出: E:/streetview/MLLM_result.xlsx（按年份分 sheet）
"""

import os
import json
import base64
import time
import re
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI
import pandas as pd

# ── 配置 ──────────────────────────────────────────────────────
BASE_DIR = Path(r"D:/streetview")
OUTPUT_DIR = BASE_DIR / "output"
PROMPT_DIR = BASE_DIR / "提示词"
WORK_DIR = BASE_DIR / "work"
CHECKPOINT_PATH = WORK_DIR / "mllm_checkpoint.json"
EXCEL_PATH = BASE_DIR / "MLLM_result.xlsx"

MODEL = "qwen3.6-flash"
ACTIVITIES = ["sitting", "standing", "walking", "jogging", "exercising", "street_vending"]
RPM_LIMIT = 600  # 每分钟最大请求数

# ── API 客户端 ────────────────────────────────────────────────
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
)

# ── 限速器 ────────────────────────────────────────────────────
class RateLimiter:
    """滑动窗口限速：每 60 秒最多 max_calls 次。"""
    def __init__(self, max_calls: int, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        self.timestamps: list[float] = []
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.time()
                # 清理窗口外的时间戳
                self.timestamps = [t for t in self.timestamps if now - t < self.period]
                if len(self.timestamps) < self.max_calls:
                    self.timestamps.append(now)
                    return
                # 需要等待的时间
                wait = self.period - (now - self.timestamps[0]) + 0.05
            time.sleep(wait)

rate_limiter = RateLimiter(RPM_LIMIT)

# ── 断点存储 ──────────────────────────────────────────────────
checkpoint_lock = threading.Lock()

def load_checkpoint() -> dict:
    """加载已完成的任务结果。key = 'id_year_prompt策略'"""
    if CHECKPOINT_PATH.exists():
        with open(CHECKPOINT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_checkpoint(checkpoint: dict):
    """调用者必须持有 checkpoint_lock"""
    with open(CHECKPOINT_PATH, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, ensure_ascii=False, indent=2)

# ── 读取提示词 ────────────────────────────────────────────────
prompts = {}
for name in ["zero-shot", "few-shot", "chain-of-thought"]:
    prompts[name] = (PROMPT_DIR / f"{name}.txt").read_text(encoding="utf-8").strip()


def encode_image_base64(image_path: Path) -> str:
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


def parse_json_response(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def call_model(image_path: Path, prompt_text: str) -> dict:
    content = [
        {"type": "image_url", "image_url": {"url": encode_image_base64(image_path)}},
        {"type": "text", "text": prompt_text},
    ]
    for attempt in range(3):
        try:
            rate_limiter.acquire()
            completion = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": content}],
            )
            raw = completion.choices[0].message.content
            result = parse_json_response(raw)
            if result:
                return result
            print(f"  [警告] JSON 解析失败，原始回复: {raw[:200]}")
        except Exception as e:
            print(f"  [错误] API 调用失败 (尝试 {attempt+1}/3): {e}")
            time.sleep(5)
    return {}


# ── 单个任务执行函数 ──────────────────────────────────────────
def process_task(task: dict, checkpoint: dict) -> dict:
    """
    一个任务 = 一张图片 + 一个 prompt 策略。
    返回 {task_key, id, coord, folder, year, prompt, ...scores/explanations}
    """
    task_key = task["task_key"]

    # 断点跳过：只有之前成功完成（scores 非空）的才跳过，失败的重新执行
    if task_key in checkpoint and _is_valid_result(checkpoint[task_key]):
        return checkpoint[task_key]

    data = call_model(task["image"], prompts[task["prompt"]])

    result = {
        "task_key": task_key,
        "id": task["id"],
        "coord": task["coord"],
        "folder": task["folder"],
        "year": task["year"],
        "prompt": task["prompt"],
    }

    for act in ACTIVITIES:
        act_data = data.get(act, {}) if data else {}
        result[f"{act}_score"] = act_data.get("score", "")
        result[f"{act}_explanation"] = act_data.get("explanation", "")

    status = "完成" if data else "失败"
    print(f"  [{status}] {task['id']}_{task['year']} × {task['prompt']}")

    # 加锁写入断点，避免并发修改字典导致 "dictionary changed size during iteration"
    with checkpoint_lock:
        checkpoint[task_key] = result
        save_checkpoint(checkpoint)

    return result


# ── 收集所有任务 ──────────────────────────────────────────────
prompt_keys = ["zero-shot", "few-shot", "chain-of-thought"]
tasks = []

for folder in sorted(OUTPUT_DIR.iterdir()):
    if not folder.is_dir():
        continue
    images = sorted(folder.glob("*.jpg"))
    if not images:
        continue
    folder_name = folder.name
    sample_id = folder_name.split("_")[0]
    parts = folder_name.split("_", 1)
    coord = parts[1] if len(parts) > 1 else ""

    for img in images:
        year_match = re.search(r"_(\d{4})\.jpg$", img.name)
        year = year_match.group(1) if year_match else "unknown"
        for pkey in prompt_keys:
            task_key = f"{sample_id}_{year}_{pkey}"
            tasks.append({
                "task_key": task_key,
                "id": sample_id,
                "coord": coord,
                "folder": folder_name,
                "year": year,
                "prompt": pkey,
                "image": img,
            })

def _is_valid_result(cached: dict) -> bool:
    """检查已缓存的结果是否有效（至少有一个 score 非空）。"""
    return any(cached.get(f"{act}_score", "") not in ("", None) for act in ACTIVITIES)

checkpoint = load_checkpoint()
done_count = sum(1 for t in tasks if t["task_key"] in checkpoint and _is_valid_result(checkpoint[t["task_key"]]))
print(f"共 {len(tasks)} 个任务（{done_count} 个已完成，{len(tasks) - done_count} 个待执行）")
print(f"并行线程数: {min(RPM_LIMIT, len(tasks))}，限速: {RPM_LIMIT} 次/分钟")

# ── 并行执行 ──────────────────────────────────────────────────
all_results = []
max_workers = min(RPM_LIMIT, len(tasks), 10)  # 线程数上限 10，避免过多连接

with ThreadPoolExecutor(max_workers=max_workers) as executor:
    futures = {
        executor.submit(process_task, task, checkpoint): task
        for task in tasks
    }
    for future in as_completed(futures):
        try:
            result = future.result()
            all_results.append(result)
        except Exception as e:
            task = futures[future]
            print(f"  [异常] {task['task_key']}: {e}")

# ── 汇总结果，按 (id, year) 聚合为一行 ───────────────────────
from collections import defaultdict

rows_map = defaultdict(dict)
for r in all_results:
    row_key = (r["id"], r["year"])
    if "id" not in rows_map[row_key]:
        rows_map[row_key].update({
            "id": r["id"],
            "coord": r["coord"],
            "folder": r["folder"],
            "year": r["year"],
        })
    pkey = r["prompt"]
    for act in ACTIVITIES:
        rows_map[row_key][f"{pkey}_{act}_score"] = r.get(f"{act}_score", "")
        rows_map[row_key][f"{pkey}_{act}_explanation"] = r.get(f"{act}_explanation", "")

# 排序
rows = sorted(rows_map.values(), key=lambda x: (x["year"], x["id"]))

# ── 按年份分 sheet 写入 Excel ─────────────────────────────────
fieldnames = ["id", "coord", "folder", "year"]
for pkey in prompt_keys:
    for act in ACTIVITIES:
        fieldnames.append(f"{pkey}_{act}_score")
        fieldnames.append(f"{pkey}_{act}_explanation")

df = pd.DataFrame(rows, columns=fieldnames)
years = sorted(df["year"].unique())

with pd.ExcelWriter(EXCEL_PATH, engine="openpyxl") as writer:
    for year in years:
        df_year = df[df["year"] == year].reset_index(drop=True)
        df_year.to_excel(writer, sheet_name=str(year), index=False)
        print(f"Sheet '{year}': {len(df_year)} 行")

print(f"\n结果已保存至: {EXCEL_PATH}")
print(f"共 {len(rows)} 条记录，{len(years)} 个 sheet")

# 保留断点文件，便于检查执行情况
# CHECKPOINT_PATH.unlink(missing_ok=True)
print(f"断点文件已保留: {CHECKPOINT_PATH}")
