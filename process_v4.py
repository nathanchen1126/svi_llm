"""
街景采样点四向拼合图生成脚本 v3
匹配逻辑：从图片文件名提取 lat，与 CSV lat 四舍五入匹配，确保唯一对应 id
相邻年份替补 → 拼接输出 → 清理冗余
"""

import csv
import os
import re
import shutil
from decimal import Decimal, ROUND_HALF_UP
from PIL import Image
from collections import defaultdict, Counter

# ╔════════════════════════════════════════════════════════════╗
# ║                    可配置参数（按需修改）                  ║
# ╚════════════════════════════════════════════════════════════╝

# --- 路径 ---
BASE_DIR    = r"D:\streetview"                                  # 项目根目录
CSV_PATH    = os.path.join(BASE_DIR, "shanghai_sample_points.csv")  # 采样点 CSV
WORK_DIR    = os.path.join(BASE_DIR, "work")                    # 中间工作目录
OUTPUT_DIR  = os.path.join(BASE_DIR, "output")                  # 全年份齐全输出目录
OUTPUT_MISS = os.path.join(BASE_DIR, "output_miss")              # 缺年份输出目录
LOG_PATH    = os.path.join(WORK_DIR, "process_log.csv")         # 处理日志
REPORT_PATH = os.path.join(WORK_DIR, "summary_report.txt")      # 汇总报告

# --- 原始图像所在目录 ---
# 年份文件夹直接位于此目录下，如 IMAGE_ROOT/2014/120.129/...
IMAGE_ROOT  = BASE_DIR

# --- 目标年份 及 替补顺序 ---
# key = 目标年份, value = 按优先级排列的查找顺序（自身在最前）
TARGET_YEARS = {
    "2014": ["2014", "2013", "2015"],
    "2022": ["2022", "2023", "2021"],
}

# --- 拼接参数 ---
ANGLES       = ["0", "90", "180", "270"]   # 拼接角度顺序
JPEG_QUALITY = 95                          # 输出 JPEG 质量

# --- 需要保护的目录/文件（清理时不删除） ---
PROTECTED_NAMES = {
    ".claude", "code", "output", "output_miss", "work",
    "shanghai_sample_points.csv",
    "提示词",
}

# --- 是否在完成后删除 work 下的中间文件（年份子目录） ---
CLEAN_WORK_INTERMEDIATES = True

# --- 是否在完成后删除根目录下残留的 {id}_ 结果文件夹 ---
CLEAN_ROOT_RESIDUALS = True


# ╔════════════════════════════════════════════════════════════╗
# ║                       工具函数                             ║
# ╚════════════════════════════════════════════════════════════╝

def round_lat(csv_lat_str, decimal_places):
    """将 CSV lat 字符串四舍五入到指定小数位数，返回字符串（去尾零）"""
    quant = Decimal(10) ** (-decimal_places)
    rounded = Decimal(csv_lat_str).quantize(quant, rounding=ROUND_HALF_UP)
    result = str(rounded)
    if '.' in result:
        result = result.rstrip('0').rstrip('.')
    return result


def extract_lat_from_files(folder_path):
    """
    从文件夹中的图片文件名提取 lat。
    文件名格式: {lon}_{lon}_{lat}_{angle}.ext
    """
    for f in os.listdir(folder_path):
        m = re.match(r'^.+_.+_(.+)_\d+\.(png|jpg)$', f, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def find_complete_angles(folder_path):
    """
    检查文件夹中是否有完整四角度图片。
    返回 {angle_str: filepath} 或 None。
    """
    files = os.listdir(folder_path)
    angle_files = {}
    for angle in ANGLES:
        found = None
        for f in files:
            if re.search(rf'_{angle}\.(png|jpg)$', f, re.IGNORECASE):
                found = os.path.join(folder_path, f)
                break
        if found is None:
            return None
        angle_files[angle] = found
    return angle_files


def stitch_images(angle_files, output_path):
    """按 0°→90°→180°→270° 横向拼接，输出 JPEG"""
    images = []
    for angle in ANGLES:
        images.append(Image.open(angle_files[angle]))

    total_w = sum(img.width for img in images)
    max_h = max(img.height for img in images)
    stitched = Image.new('RGB', (total_w, max_h))

    x = 0
    for img in images:
        stitched.paste(img, (x, 0))
        x += img.width

    stitched.save(output_path, 'JPEG', quality=JPEG_QUALITY)
    for img in images:
        img.close()


def collect_all_year_dirs(image_root):
    """
    扫描 image_root 下所有 4 位数字年份目录，返回排序列表。
    这些是磁盘上实际存在的年份。
    """
    years = []
    if not os.path.isdir(image_root):
        return years
    for entry in os.listdir(image_root):
        if os.path.isdir(os.path.join(image_root, entry)) and re.match(r'^\d{4}$', entry):
            years.append(entry)
    return sorted(years)


# ╔════════════════════════════════════════════════════════════╗
# ║                        主流程                              ║
# ╚════════════════════════════════════════════════════════════╝

def main():
    print("=" * 60)
    print("街景采样点四向拼合图处理 v3（lat 匹配 + 相邻年份替补）")
    print("=" * 60)

    # ----------------------------------------------------------
    # 步骤 0：扫描可用年份和文件夹
    # ----------------------------------------------------------
    available_years = collect_all_year_dirs(IMAGE_ROOT)
    print(f"\n磁盘上可用年份: {available_years}")

    # 列出替补链中哪些年份实际存在
    for target, chain in TARGET_YEARS.items():
        present = [y for y in chain if y in available_years]
        missing = [y for y in chain if y not in available_years]
        print(f"  {target} 替补链 {chain} → 可用 {present}, 缺失 {missing}")

    # 收集所有 (year, lon_folder, img_lat, folder_path)
    folder_info = []
    img_lat_set = set()

    for year in available_years:
        year_path = os.path.join(IMAGE_ROOT, year)
        for lon_folder in os.listdir(year_path):
            fp = os.path.join(year_path, lon_folder)
            if not os.path.isdir(fp):
                continue
            img_lat = extract_lat_from_files(fp)
            if img_lat is None:
                print(f"  [警告] 无法从 {fp} 提取 lat，跳过")
                continue
            folder_info.append((year, lon_folder, img_lat, fp))
            img_lat_set.add(img_lat)
            print(f"  发现: {year}/{lon_folder} → lat={img_lat}")

    unique_img_lats = sorted(img_lat_set)
    print(f"\n唯一图片采样点（lat）数: {len(unique_img_lats)}")

    # ----------------------------------------------------------
    # 步骤 1：读取 CSV 并按 lat 匹配
    # ----------------------------------------------------------
    print("\n[步骤1] 读取 CSV 并按 lat 匹配...")

    csv_records = []
    with open(CSV_PATH, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        print(f"CSV列名: {reader.fieldnames}")
        for row in reader:
            csv_records.append((row['id'].strip(), row['lon'].strip(), row['lat'].strip()))
    print(f"  CSV 记录数: {len(csv_records)}")

    img_lat_to_id   = {}   # img_lat -> id
    img_lat_to_info = {}   # img_lat -> (id, lon_str, lat_str)
    match_details   = {}   # id -> "完全匹配" / "标准化匹配"

    for img_lat in unique_img_lats:
        decimal_places = len(img_lat.split('.')[1]) if '.' in img_lat else 0
        print(f"\n  图片 lat: {img_lat} ({decimal_places} 位小数)")

        # 第一优先级：完全字符串匹配
        exact = [(i, lo, la) for i, lo, la in csv_records if la == img_lat]
        if len(exact) == 1:
            i, lo, la = exact[0]
            img_lat_to_id[img_lat] = i
            img_lat_to_info[img_lat] = (i, lo, la)
            match_details[i] = "完全匹配"
            print(f"    → 完全匹配: id={i}")
            continue
        elif len(exact) > 1:
            print(f"    [警告] 完全匹配到多个点: {[m[0] for m in exact]}，跳过此图片")
            continue

        # 第二优先级：四舍五入匹配
        rounded = [(i, lo, la) for i, lo, la in csv_records
                   if round_lat(la, decimal_places) == img_lat]
        if len(rounded) == 1:
            i, lo, la = rounded[0]
            img_lat_to_id[img_lat] = i
            img_lat_to_info[img_lat] = (i, lo, la)
            match_details[i] = "标准化匹配"
            print(f"    → 标准化匹配: id={i}, lon={lo}, lat={la}")
        elif len(rounded) > 1:
            print(f"    [错误] 标准化匹配到多个点: {[m[0] for m in rounded]}，终止！")
            return
        else:
            print(f"    [警告] 无匹配")

    matched_img_lats = [lat for lat in unique_img_lats if lat in img_lat_to_id]
    print(f"\n  成功匹配: {len(matched_img_lats)} / {len(unique_img_lats)}")

    # img_lat -> {year: folder_path}
    lat_year_folders = defaultdict(dict)
    for year, lon_folder, img_lat, fp in folder_info:
        lat_year_folders[img_lat][year] = fp

    # ----------------------------------------------------------
    # 步骤 2：复制与替补
    # ----------------------------------------------------------
    print("\n[步骤2] 复制图像（含相邻年份替补）...")

    for year in TARGET_YEARS:
        os.makedirs(os.path.join(WORK_DIR, year), exist_ok=True)

    id_year_source = {}  # (id, target_year) -> 来源描述

    for img_lat in matched_img_lats:
        id_s = img_lat_to_id[img_lat]
        year_folders = lat_year_folders[img_lat]

        for target_year, fallback_chain in TARGET_YEARS.items():
            found = False
            for try_year in fallback_chain:
                # 检查该年份是否在磁盘上存在
                if try_year not in available_years:
                    continue
                # 检查该采样点是否在该年份下有文件夹
                if try_year not in year_folders:
                    continue
                fp = year_folders[try_year]
                angle_files = find_complete_angles(fp)
                if angle_files is None:
                    continue

                # 四图齐全，复制
                work_id_dir = os.path.join(WORK_DIR, target_year, id_s)
                os.makedirs(work_id_dir, exist_ok=True)
                for angle in ANGLES:
                    src = angle_files[angle]
                    ext = os.path.splitext(src)[1]
                    shutil.copy2(src, os.path.join(work_id_dir, f"{angle}{ext}"))

                if try_year == target_year:
                    id_year_source[(id_s, target_year)] = f"{target_year}自身"
                else:
                    id_year_source[(id_s, target_year)] = f"用{try_year}替代"
                found = True
                print(f"  id={id_s} {target_year}年 ← {try_year}")
                break

            if not found:
                id_year_source[(id_s, target_year)] = "无图"
                print(f"  id={id_s} {target_year}年 ← 无图（替补链均无完整四图）")

    # 统计
    print("\n  --- 图像来源统计 ---")
    for ty in sorted(TARGET_YEARS):
        srcs = [id_year_source.get((img_lat_to_id[lat], ty), "无图")
                for lat in matched_img_lats]
        print(f"  {ty}年: {dict(Counter(srcs))}")

    # ----------------------------------------------------------
    # 步骤 3：四向拼接
    # ----------------------------------------------------------
    print("\n[步骤3] 四向拼接...")

    for d in [OUTPUT_DIR, OUTPUT_MISS]:
        if os.path.exists(d):
            shutil.rmtree(d)
        os.makedirs(d, exist_ok=True)

    stitch_count = 0
    stitch_complete = 0
    stitch_miss = 0
    for img_lat in matched_img_lats:
        id_s, lon_s, lat_s = img_lat_to_info[img_lat]
        label = f"{id_s}_{lon_s}_{lat_s}"

        # 判断是否所有目标年份都有图
        all_years_ok = all(
            id_year_source.get((id_s, ty)) != "无图"
            for ty in TARGET_YEARS
        )
        dest_dir = OUTPUT_DIR if all_years_ok else OUTPUT_MISS
        out_folder = os.path.join(dest_dir, label)

        has_any = False
        for ty in sorted(TARGET_YEARS):
            if id_year_source.get((id_s, ty)) == "无图":
                continue
            work_id_dir = os.path.join(WORK_DIR, ty, id_s)
            if not os.path.isdir(work_id_dir):
                continue

            angle_files = {}
            ok = True
            for angle in ANGLES:
                for ext in ['.png', '.jpg']:
                    p = os.path.join(work_id_dir, f"{angle}{ext}")
                    if os.path.exists(p):
                        angle_files[angle] = p
                        break
                if angle not in angle_files:
                    ok = False
                    break
            if not ok:
                continue

            if not has_any:
                os.makedirs(out_folder, exist_ok=True)
                has_any = True

            out_path = os.path.join(out_folder, f"{label}_{ty}.jpg")
            stitch_images(angle_files, out_path)
            stitch_count += 1
            tag = "完整" if all_years_ok else "缺年"
            print(f"  ✓ [{tag}] {label}_{ty}.jpg")

        if has_any:
            if all_years_ok:
                stitch_complete += 1
            else:
                stitch_miss += 1

    print(f"\n  拼接完成，共 {stitch_count} 张图片")
    print(f"  全年份齐全 → output/: {stitch_complete} 个采样点")
    print(f"  缺少年份  → output_miss/: {stitch_miss} 个采样点")

    # ----------------------------------------------------------
    # 步骤 4：生成日志
    # ----------------------------------------------------------
    print("\n[步骤4] 生成日志...")

    matched_ids = set(img_lat_to_id.values())

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['id', 'lon', 'lat', '匹配方式', '2014年图像来源', '2022年图像来源'])
        for i, lo, la in csv_records:
            if i in matched_ids:
                mt = match_details[i]
                s14 = id_year_source.get((i, "2014"), "无图")
                s22 = id_year_source.get((i, "2022"), "无图")
            else:
                mt, s14, s22 = "未匹配", "—", "—"
            w.writerow([i, lo, la, mt, s14, s22])
    print(f"  日志: {LOG_PATH}")

    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write("=" * 60 + "\n")
        f.write("上海街景四向拼合图 v3 — 汇总报告\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"CSV 采样点总数: {len(csv_records)}\n")
        f.write(f"磁盘可用年份: {available_years}\n\n")
        for target, chain in TARGET_YEARS.items():
            present = [y for y in chain if y in available_years]
            missing = [y for y in chain if y not in available_years]
            f.write(f"{target} 替补链: {chain}\n")
            f.write(f"  可用: {present}  缺失: {missing}\n")
        f.write(f"\n匹配成功: {len(matched_ids)}\n")
        f.write(f"拼接图数: {stitch_count}\n")
        f.write(f"全年份齐全 (output): {stitch_complete} 个采样点\n")
        f.write(f"缺少年份 (output_miss): {stitch_miss} 个采样点\n\n")
        f.write("--- 详情 ---\n")
        for img_lat in matched_img_lats:
            i, lo, la = img_lat_to_info[img_lat]
            mt = match_details[i]
            s14 = id_year_source.get((i, "2014"), "无图")
            s22 = id_year_source.get((i, "2022"), "无图")
            yrs = sorted(lat_year_folders[img_lat].keys())
            f.write(f"  id={i}  lon={lo}  lat={la}\n")
            f.write(f"    匹配={mt}  原始年份={yrs}  2014={s14}  2022={s22}\n")
    print(f"  报告: {REPORT_PATH}")

    # ----------------------------------------------------------
    # 步骤 5：清理冗余
    # ----------------------------------------------------------
    print("\n[步骤5] 清理冗余文件...")

    # 5a. 清理 work 下的中间年份子目录
    if CLEAN_WORK_INTERMEDIATES:
        for entry in os.listdir(WORK_DIR):
            fp = os.path.join(WORK_DIR, entry)
            if os.path.isdir(fp) and re.match(r'^\d{4}$', entry):
                shutil.rmtree(fp)
                print(f"  删除 work/{entry}/")

    # 5b. 清理根目录下残留的 {id}_{lon}_{lat} 文件夹
    if CLEAN_ROOT_RESIDUALS:
        for entry in os.listdir(BASE_DIR):
            if entry in PROTECTED_NAMES:
                continue
            fp = os.path.join(BASE_DIR, entry)
            if os.path.isdir(fp) and re.match(r'^\d+_\d+\.\d+_\d+\.\d+', entry):
                shutil.rmtree(fp)
                print(f"  删除 {entry}/")

    print("\n" + "=" * 60)
    print("全部完成！")
    print(f"  全年份齐全: {OUTPUT_DIR}")
    print(f"  缺少年份:   {OUTPUT_MISS}")
    print("=" * 60)


if __name__ == "__main__":
    main()
