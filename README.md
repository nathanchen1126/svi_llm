# SVI-LLM：街景图像大模型评估工具

利用大语言模型（MLLM）对上海街景图像进行城市活动适宜性评估，包含图像预处理、多提示策略并行评估、结果汇总分析等完整流程。

## 项目结构

```
├── process_v4.py              # 街景采样点四向拼合图生成脚本
├── mllm_evaluate_v0513.py     # MLLM 活动适宜性评估脚本（多线程并行）
└── README.md
```

## 环境要求

- Python 3.9+
- 依赖包：`openai`, `pandas`, `openpyxl`, `Pillow`

```bash
pip install openai pandas openpyxl Pillow
```

## 模块说明

### 1. process\_v4.py — 图像预处理

将原始街景图片按采样点拼接为四方向（0°/90°/180°/270°）全景图。

**主要功能：**
- 从图片文件名自动提取经纬度坐标
- 与 CSV 采样点数据按经纬度匹配合并
- 支持相邻年份替补（如 2014 年缺失时用 2013/2015 年代替）
- 将四个方向的图像横向拼接为单张 JPEG
- 生成处理日志和汇总报告

**配置：** 修改脚本开头的 `BASE_DIR`、`CSV_PATH`、`TARGET_YEARS` 等变量。

### 2. mllm\_evaluate\_v0513.py — MLLM 评估

使用通义千问（qwen3.6-flash）大模型对拼接后的街景图像进行活动适宜性评估。

**主要功能：**
- **三种提示策略**：zero-shot、few-shot、chain-of-thought
- **六类活动评估**：坐憩(sitting)、站立(standing)、步行(walking)、慢跑(jogging)、锻炼(exercising)、街贩(street\_vending)
- **并行调用**：多线程并发请求，滑动窗口限速（默认 600 次/分钟）
- **断点续传**：自动跳过已完成的有效任务，失败任务会重新执行
- **结果输出**：按年份分 Sheet 写入 `MLLM_result.xlsx`

**注意事项：**
- 需要设置环境变量 `DASHSCOPE_API_KEY`（阿里云 DashScope API 密钥）
- 需要在 `BASE_DIR/提示词/` 目录下准备三个提示词文件：`zero-shot.txt`、`few-shot.txt`、`chain-of-thought.txt`
- 拼接好的图像应放在 `BASE_DIR/output/` 目录下

## 使用流程

1. **准备数据**：将原始街景图片和采样点 CSV 放在项目目录
2. **运行预处理**：`python process_v4.py` 生成拼接图像
3. **准备提示词**：在 `提示词/` 目录下编写三个提示词文件
4. **运行评估**：`python mllm_evaluate_v0513.py` 调用 MLLM 进行评估
5. **查看结果**：在 `MLLM_result.xlsx` 中查看各年份的评估得分

## 断点续传

评估脚本会在 `work/mllm_checkpoint.json` 中保存中间结果。若程序中断，重新运行时会：
- 跳过已成功完成的任务（所有活动 score 非空）
- **重新执行**之前失败的任务（score 为空的记录）

运行结束后 会保留checkpoint 文件。
