"""Trình khởi chạy menu dòng lệnh cho các script tự động hóa Python đã chuyển đổi từ .amk."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from typing import Any


HELPER_FILES = frozenset(
    {
        "run_menu.py",
        "amk2py.py",
        "amk_parser.py",
        "amk_runtime.py",
        "amk_tools.py",
    }
)
CONFIG_FILENAME = "menu_config.json"


def _ensure_dpi_aware() -> None:
    try:
        from amk_runtime import _enable_dpi_awareness

        _enable_dpi_awareness()
    except Exception:
        pass


def configure_console_utf8() -> None:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _read_script_head(filepath: str) -> str | None:
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read(32768)
    except OSError:
        return None


def _extract_excel_keys_from_head(head: str) -> list[str]:
    xls_matches = re.findall(r"rt\.xls_open\((['\"])(.+?)\1", head)
    if xls_matches:
        seen_xls: list[str] = []
        for _, raw_path in xls_matches:
            base = raw_path.replace("\\\\", "/").replace("\\", "/").rsplit("/", 1)[-1]
            stem, ext = os.path.splitext(base)
            fname = f"{stem}{ext or '.xlsx'}"
            if fname not in seen_xls:
                seen_xls.append(fname)
        return seen_xls
    if "rt.xls_open(" in head:
        return ["Database.xlsx"]
    return []


def get_script_excel_keys(filepath: str) -> list[str]:
    """Return ordered unique Excel filenames required by a converted script."""
    head = _read_script_head(filepath)
    if not head or "def func_entry(" not in head:
        return []
    return _extract_excel_keys_from_head(head)


def _extract_start_row_info_from_head(head: str) -> tuple[int, str, str, bool] | None:
    """Extract (default_row, sheet_name, raw_excel_path, is_interactive) from script head."""
    xls_match = re.search(r"rt\.xls_open\((['\"])(.+?)\1", head)
    raw_excel_path = xls_match.group(2) if xls_match else "Database"

    m = re.search(
        r"v_x\s*=\s*rt\.get_start_row\(\s*(\d+)\s*,\s*\w+\s*,\s*(['\"])(.+?)\2"
        r"(?:\s*,\s*interactive_if_unset=(True|False))?\s*\)",
        head,
    )
    if m:
        default_row = int(m.group(1))
        sheet_name = m.group(3)
        interactive_str = m.group(4)
        is_interactive = (
            (interactive_str == "True")
            if interactive_str is not None
            else (sheet_name == "Sheet5")
        )
        return default_row, sheet_name, raw_excel_path, is_interactive

    m_legacy = re.search(
        r"v_x\s*=\s*(\d+)\s*\n\s*\w+\s*=\s*rt\.xls_get_cell\(\s*\w+\s*,\s*(['\"])(.+?)\2\s*,\s*v_x\b",
        head,
    )
    if m_legacy:
        default_row = int(m_legacy.group(1))
        sheet_name = m_legacy.group(3)
        return default_row, sheet_name, raw_excel_path, (sheet_name == "Sheet5")
    return None


def get_script_start_row_info(filepath: str) -> tuple[int, str, str, bool] | None:
    """Return (default_row, sheet_name, raw_excel_path, is_interactive) if script uses row cursor v_x."""
    head = _read_script_head(filepath)
    if not head or "def func_entry(" not in head:
        return None
    return _extract_start_row_info_from_head(head)


def discover_scripts(folder: str) -> list[tuple[str, str]]:
    """Return list of (filename, status_badge) for converted macro scripts."""
    scripts: list[tuple[str, str]] = []
    for fn in sorted(os.listdir(folder), key=lambda s: s.lower()):
        if not fn.lower().endswith(".py") or fn in HELPER_FILES or fn.startswith("test_"):
            continue
        fp = os.path.join(folder, fn)
        if not os.path.isfile(fp):
            continue
        head = _read_script_head(fp)
        if not head or "def func_entry(" not in head:
            continue

        tags: list[str] = []
        entry_part = head.split("def func_entry(rt: AmkRuntime) -> None:", 1)[1].split(
            "\n# =====", 1
        )[0]
        non_empty_stmts = [
            ln.strip()
            for ln in entry_part.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if non_empty_stmts == ["pass"]:
            tags.append("trống - file nguồn không có lệnh")
        excel_keys = _extract_excel_keys_from_head(head)
        if excel_keys:
            tags.append(f"cần {', '.join(excel_keys)}")
        row_info = _extract_start_row_info_from_head(head)
        if row_info is not None and row_info[3]:
            tags.append(f"chọn dòng v_x (mặc định {row_info[0]})")
        badge = f" [{', '.join(tags)}]" if tags else ""
        scripts.append((fn, badge))
    return scripts


def load_config(folder: str) -> dict[str, Any]:
    """Load persisted menu configuration (Excel path overrides) from menu_config.json."""
    cfg_path = os.path.join(folder, CONFIG_FILENAME)
    default_cfg: dict[str, Any] = {"excel_paths": {}}
    if not os.path.isfile(cfg_path):
        return default_cfg
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default_cfg
    if not isinstance(data, dict):
        return default_cfg
    raw_paths = data.get("excel_paths")
    if not isinstance(raw_paths, dict):
        return default_cfg
    clean_paths = {
        str(k): str(v) for k, v in raw_paths.items() if isinstance(k, str) and isinstance(v, str) and v.strip()
    }
    return {"excel_paths": clean_paths}


def save_config(folder: str, cfg: dict[str, Any]) -> bool:
    """Persist menu configuration to menu_config.json inside folder."""
    cfg_path = os.path.join(folder, CONFIG_FILENAME)
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except OSError as exc:
        print(f"[CẢNH BÁO] Không thể lưu cấu hình vào {cfg_path}: {exc}")
        return False


def normalize_excel_input(raw: str) -> str:
    """Clean pasted path (quotes, env vars, missing .xlsx extension)."""
    s = raw.strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    if not s:
        return ""
    s = os.path.expandvars(os.path.expanduser(s))
    s = os.path.normpath(s)
    if not os.path.isfile(s) and os.path.splitext(s)[1].lower() not in (".xlsx", ".xlsm", ".xls"):
        for ext in (".xlsx", ".xlsm", ".xls"):
            cand = s + ext
            if os.path.isfile(cand):
                return cand
    return s


def browse_excel_file(title: str, initial_dir: str) -> str | None:
    """Open native Windows file picker for Excel files; returns None if cancelled/unavailable."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        kwargs: dict[str, Any] = {
            "parent": root,
            "title": title,
            "filetypes": [("Excel files", "*.xlsx *.xlsm *.xls"), ("All files", "*.*")],
        }
        if initial_dir and os.path.isdir(initial_dir):
            kwargs["initialdir"] = initial_dir
        chosen = filedialog.askopenfilename(**kwargs)
        return os.path.normpath(chosen) if chosen else None
    except Exception:
        return None
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


def parse_choice_args(choice: str) -> list[str]:
    """Split CLI arguments preserving Windows backslashes and stripping outer quotes."""
    try:
        tokens = shlex.split(choice, posix=False)
    except ValueError:
        tokens = choice.split()
    cleaned: list[str] = []
    for tok in tokens:
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'"):
            cleaned.append(tok[1:-1])
        else:
            cleaned.append(tok)
    return cleaned


def _has_manual_excel_flag(extra_args: list[str]) -> bool:
    return any(a == "--excel" or a.startswith("--excel=") for a in extra_args)


def _has_manual_start_row_flag(extra_args: list[str]) -> bool:
    return any(a == "--start-row" or a.startswith("--start-row=") for a in extra_args)


def _extract_manual_excel_flag(extra_args: list[str]) -> str | None:
    for idx, a in enumerate(extra_args):
        if a == "--excel" and idx + 1 < len(extra_args):
            return extra_args[idx + 1]
        if a.startswith("--excel="):
            return a.split("=", 1)[1]
    return None


def resolve_excel_for_preview(
    folder: str,
    raw_excel_path: str,
    excel_keys: list[str],
    extra_args: list[str],
    cfg: dict[str, Any],
) -> str | None:
    """Locate the Excel file path for previewing rows before launching a script."""
    manual = _extract_manual_excel_flag(extra_args)
    saved = ""
    if excel_keys:
        saved = cfg.get("excel_paths", {}).get(excel_keys[0], "").strip()
    override = manual or saved or None
    try:
        if folder not in sys.path:
            sys.path.insert(0, folder)
        from amk_runtime import AmkRuntime

        rt = AmkRuntime({}, dry_run=True, quiet=True, excel_override=override, script_dir=folder)
        return rt._resolve_excel_path(raw_excel_path)
    except Exception:
        return None


def preview_excel_sheet_rows(
    folder: str, excel_path: str | None, sheet_name: str, default_row: int = 3
) -> list[tuple[int, str]]:
    """Return list of (row_index, description) from sheet_name using AmkRuntime."""
    if not excel_path or not os.path.isfile(excel_path):
        return []
    try:
        if folder not in sys.path:
            sys.path.insert(0, folder)
        import openpyxl
        from amk_runtime import AmkRuntime

        rt = AmkRuntime({}, dry_run=True, quiet=True, script_dir=folder)
        wb = openpyxl.load_workbook(excel_path, data_only=True, read_only=False)
        try:
            return rt.preview_sheet_rows(wb, sheet_name, default_row)
        finally:
            try:
                wb.close()
            except Exception:
                pass
    except Exception:
        return []


def prompt_start_row_interactive(
    folder: str,
    script_fn: str,
    default_row: int,
    sheet_name: str,
    raw_excel_path: str,
    excel_keys: list[str],
    extra_args: list[str],
    cfg: dict[str, Any],
) -> int:
    """Display Excel rows/hours preview and prompt user for starting row v_x."""
    excel_path = resolve_excel_for_preview(folder, raw_excel_path, excel_keys, extra_args, cfg)
    preview = preview_excel_sheet_rows(folder, excel_path, sheet_name, default_row)
    valid_preview_rows = {r_idx for r_idx, _ in preview}

    print("\n" + "-" * 68)
    print(f"  CHỌN DÒNG BẮT ĐẦU (v_x) - {script_fn} [{sheet_name}]")
    if excel_path:
        print(f"  File Excel: {excel_path}")
    if preview:
        print("-" * 68)
        for r_idx, desc in preview:
            mark = "  <-- mặc định" if r_idx == int(default_row) else ""
            print(f"    Dòng {r_idx:2d} : {desc}{mark}")
    else:
        print("  (Không đọc trước được danh sách dòng từ file Excel; bạn vẫn có thể nhập số dòng)")
    print("-" * 68)

    while True:
        raw = input(
            f"Nhập dòng bắt đầu v_x (Enter = mặc định dòng {default_row}): "
        ).strip()
        if not raw:
            return int(default_row)
        try:
            val = int(raw)
        except ValueError:
            print("  Vui lòng nhập một số nguyên hợp lệ (ví dụ: 3, 4, 5...).")
            continue
        if val < 1:
            print("  Số dòng phải >= 1.")
            continue
        if valid_preview_rows and val not in valid_preview_rows:
            ans = (
                input(
                    f"  [CẢNH BÁO] Dòng {val} không nằm trong danh sách có dữ liệu ({min(valid_preview_rows)}..{max(valid_preview_rows)}). "
                    "Vẫn dùng dòng này? (y/n): "
                )
                .strip()
                .lower()
            )
            if ans not in ("y", "yes", "c", "co"):
                continue
        return val


def build_run_command(
    python_exe: str,
    target_path: str,
    excel_keys: list[str],
    extra_args: list[str],
    cfg: dict[str, Any],
) -> list[str]:
    """Build subprocess command list, injecting saved --excel override when applicable."""
    cmd = [python_exe, target_path, *extra_args]
    if excel_keys and not _has_manual_excel_flag(extra_args):
        saved = cfg.get("excel_paths", {}).get(excel_keys[0], "").strip()
        if saved:
            cmd.extend(["--excel", saved])
    return cmd


def _collect_folder_excel_keys(folder: str, scripts: list[tuple[str, str]]) -> list[str]:
    keys: list[str] = []
    for fn, _ in scripts:
        for k in get_script_excel_keys(os.path.join(folder, fn)):
            if k not in keys:
                keys.append(k)
    if not keys:
        return ["Database.xlsx"]
    keys.sort(key=lambda k: (0 if k.lower() == "database.xlsx" else 1, k.lower()))
    return keys


def _format_db_status(key: str, cfg: dict[str, Any]) -> str:
    saved = cfg.get("excel_paths", {}).get(key, "").strip()
    if not saved:
        return f"{key}: (Tự động dò)"
    status = "OK" if os.path.isfile(saved) else "KHÔNG TỒN TẠI"
    return f"{key}: {saved} [{status}]"


def configure_database_menu(
    folder: str, cfg: dict[str, Any], folder_excel_keys: list[str]
) -> None:
    """Interactive prompt to view, pick, paste, or reset Excel database paths."""
    keys = list(folder_excel_keys) if folder_excel_keys else ["Database.xlsx"]

    while True:
        print("\n" + "-" * 68)
        print("  CẤU HÌNH ĐƯỜNG DẪN FILE EXCEL DATABASE")
        print("-" * 68)
        if len(keys) == 1:
            target_key = keys[0]
            print(f"  Hiện tại -> {_format_db_status(target_key, cfg)}")
        else:
            for idx, key in enumerate(keys, start=1):
                print(f"  [{idx}] {_format_db_status(key, cfg)}")
            print("  [0] Quay lại menu chính")
            sel = input("Chọn loại file Excel cần đổi đường dẫn: ").strip()
            if not sel or sel == "0":
                return
            try:
                pos = int(sel)
            except ValueError:
                print("Lựa chọn không hợp lệ.")
                continue
            if pos < 1 or pos > len(keys):
                print(f"Vui lòng chọn từ 1 đến {len(keys)}.")
                continue
            target_key = keys[pos - 1]
            print(f"\n  Đang cấu hình {target_key} -> {_format_db_status(target_key, cfg)}")

        inp = input(
            "  Nhập đường dẫn mới, 'b' mở hộp thoại chọn file, 'reset' về mặc định (Enter = quay lại): "
        ).strip()
        if not inp:
            if len(keys) == 1:
                return
            continue

        low = inp.lower()
        if low in ("reset", "clear"):
            cfg.setdefault("excel_paths", {}).pop(target_key, None)
            if save_config(folder, cfg):
                print(f"  [OK] Đã đưa {target_key} về chế độ tự động dò.")
            if len(keys) == 1:
                return
            continue

        if low in ("b", "browse"):
            current = cfg.get("excel_paths", {}).get(target_key, "")
            init_dir = os.path.dirname(current) if current else folder
            chosen = browse_excel_file(f"Chọn file {target_key}", init_dir)
            if not chosen:
                print("  Đã hủy chọn file (hoặc môi trường không hỗ trợ hộp thoại chọn file).")
                if len(keys) == 1:
                    return
                continue
            resolved = chosen
        else:
            resolved = normalize_excel_input(inp)

        if not resolved or not os.path.isfile(resolved):
            print(f"  [LỖI] Không tìm thấy file: {resolved or '(trống)'}")
            if len(keys) == 1:
                continue
            continue

        cfg.setdefault("excel_paths", {})[target_key] = resolved
        if save_config(folder, cfg):
            print(f"  [OK] Đã lưu {target_key} = {resolved}")
        if len(keys) == 1:
            return


REQUIRED_PACKAGES: tuple[tuple[str, str], ...] = (
    ("cv2", "opencv-python"),
    ("numpy", "numpy"),
    ("openpyxl", "openpyxl"),
)


def find_missing_dependencies(
    packages: tuple[tuple[str, str], ...] | list[tuple[str, str]] = REQUIRED_PACKAGES,
) -> list[tuple[str, str]]:
    """Return list of (import_name, pip_package) not installed in current Python."""
    missing: list[tuple[str, str]] = []
    for mod_name, pip_name in packages:
        if importlib.util.find_spec(mod_name) is None:
            missing.append((mod_name, pip_name))
    return missing


def ensure_dependencies_interactive(folder: str) -> None:
    """Detect missing runtime packages and offer one-step pip installation."""
    missing = find_missing_dependencies()
    if not missing:
        return

    pip_pkgs = [pip_name for _, pip_name in missing]
    mod_names = [mod_name for mod_name, _ in missing]
    print("\n" + "!" * 68)
    print(f"  CẢNH BÁO: Python hiện tại ({sys.executable})")
    print(f"  chưa cài đủ thư viện cần thiết: {', '.join(mod_names)}")
    print(f"  Gói cần cài: {', '.join(pip_pkgs)}")
    print("!" * 68)
    ans = input("Bạn có muốn tự động cài đặt ngay bằng pip không? (Y/n): ").strip().lower()
    if ans not in ("", "y", "yes", "c", "co"):
        print("Bỏ qua cài đặt. Các script dùng nhận diện ảnh/Excel sẽ báo lỗi thiếu module.")
        return

    req_path = os.path.join(folder, "requirements.txt")
    if os.path.isfile(req_path):
        cmd = [sys.executable, "-m", "pip", "install", "-r", req_path]
    else:
        cmd = [sys.executable, "-m", "pip", "install", *pip_pkgs]
    print(f">>> Đang chạy: {' '.join(cmd)}")
    res = subprocess.run(cmd)
    if res.returncode == 0 and not find_missing_dependencies():
        print("[OK] Đã cài đặt đầy đủ thư viện! Tiếp tục mở menu...\n")
        return

    print("\n[LỖI] Không thể cài đặt tự động (thường do máy chưa có kết nối Internet hoặc thiếu pip).")
    print("Cách khắc phục:")
    print(f"  1. Chạy lại khi có mạng, hoặc chạy file Cai_dat_thu_vien.bat trong {folder}")
    print(f"  2. Hoặc chạy lệnh: {sys.executable} -m pip install {' '.join(pip_pkgs)}")
    print("  3. Nếu máy khoa không có Internet: gửi lại kết quả lệnh 'python --version' để đóng gói bộ cài offline.")
    input("Nhấn Enter để tiếp tục...")


def ensure_logs_dir(folder: str) -> str:
    """Ensure <folder>/logs exists and contains a brief README_LOGS.txt."""
    logs_dir = os.path.join(folder, "logs")
    try:
        os.makedirs(logs_dir, exist_ok=True)
        readme_fp = os.path.join(logs_dir, "README_LOGS.txt")
        if not os.path.isfile(readme_fp):
            with open(readme_fp, "w", encoding="utf-8") as f:
                f.write(
                    "THƯ MỤC LƯU NHẬT KÝ THỰC THI & CHẨN ĐOÁN LỖI (AMK PYTHON LOGS)\n"
                    "================================================================\n"
                    "- Mỗi lần chạy script, một file YYYYMMDD_HHMMSS_<Ten_Script>.log được tự động tạo.\n"
                    "- latest.log       : Bản sao nhật ký của lần chạy gần nhất.\n"
                    "- latest_error.log : Bản sao nhật ký của lần dừng/lỗi gần nhất.\n"
                    "- *_ERROR.png      : Ảnh chụp màn hình tự động tại thời điểm không tìm thấy ảnh/lỗi\n"
                    "                     (khung vàng = vùng tìm kiếm region, dấu đỏ = vị trí gần giống nhất).\n"
                    "- Bạn có thể mở xem trực tiếp hoặc gửi đường dẫn thư mục logs này cho Agent để tìm lỗi.\n"
                )
    except OSError:
        pass
    return logs_dir


def parse_log_summary(filepath: str) -> dict[str, str]:
    """Extract structured summary fields from a saved .log file."""
    info: dict[str, str] = {
        "filename": os.path.basename(filepath),
        "filepath": filepath,
        "script": "",
        "start_time": "",
        "status": "RUNNING",
        "last_step": "",
        "match_detail": "",
        "error_msg": "",
        "screenshot": "",
    }
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return info

    for line in text.splitlines():
        if line.startswith("Script            :"):
            info["script"] = line.split(":", 1)[1].strip()
        elif line.startswith("Thời gian bắt đầu :"):
            info["start_time"] = line.split(":", 1)[1].strip()
        elif line.startswith("Trạng thái        :"):
            info["status"] = line.split(":", 1)[1].strip()
        elif line.startswith("Bước cuối cùng    :"):
            info["last_step"] = line.split(":", 1)[1].strip()
        elif line.startswith("Chi tiết khớp ảnh :"):
            info["match_detail"] = line.split(":", 1)[1].strip()
        elif line.startswith("Thông báo dừng/lỗi:"):
            info["error_msg"] = line.split(":", 1)[1].strip()
        elif line.startswith("Ảnh chụp lúc lỗi  :"):
            info["screenshot"] = line.split(":", 1)[1].strip()
    return info


def list_log_files(logs_dir: str, limit: int = 15) -> list[dict[str, str]]:
    """Return most recent timestamped log summaries (newest first)."""
    if not os.path.isdir(logs_dir):
        return []
    files = sorted(
        (
            fn
            for fn in os.listdir(logs_dir)
            if fn.endswith(".log") and fn not in ("latest.log", "latest_error.log")
        ),
        reverse=True,
    )
    return [parse_log_summary(os.path.join(logs_dir, fn)) for fn in files[: max(1, limit)]]


def view_logs_menu(folder: str) -> None:
    """Interactive menu to inspect recent execution logs and error diagnostics."""
    logs_dir = ensure_logs_dir(folder)
    while True:
        items = list_log_files(logs_dir, limit=15)
        latest_err_fp = os.path.join(logs_dir, "latest_error.log")
        has_latest_err = os.path.isfile(latest_err_fp)

        print("\n" + "=" * 68)
        print("  NHẬT KÝ CHẠY & TRA CỨU LỖI (THƯ MỤC LOGS)")
        print(f"  Đường dẫn: {logs_dir}")
        print("=" * 68)
        if has_latest_err:
            err_info = parse_log_summary(latest_err_fp)
            print(
                f"  [!] LỖI/DỪNG GẦN NHẤT: {err_info['script']} ({err_info['start_time']}) -> {err_info['status']}"
            )
            if err_info["last_step"]:
                print(f"      Bước dừng: {err_info['last_step']}")
            if err_info["error_msg"]:
                print(f"      Chi tiết : {err_info['error_msg']}")
            print("-" * 68)

        if not items:
            print("  (Chưa có file nhật ký .log nào trong thư mục logs)")
        else:
            for idx, it in enumerate(items, start=1):
                badge = "OK" if it["status"] == "SUCCESS" else f"LỖI/DỪNG: {it['status']}"
                detail = it["error_msg"] or it["last_step"]
                if len(detail) > 55:
                    detail = detail[:52] + "..."
                print(
                    f"  [{idx:2d}] [{badge}] {it['start_time']} | {it['script']} | {detail}"
                )

        print("-" * 68)
        print("  [ E] Xem chi tiết lần lỗi/dừng gần nhất (latest_error.log)")
        print("  [ O] Mở thư mục logs trong Windows Explorer")
        print("  [ 0] Quay lại menu chính")
        sel = input("Chọn số thứ tự file log để xem, 'E', 'O' (Enter = quay lại): ").strip()
        if not sel or sel == "0":
            return

        low = sel.lower()
        if low == "o":
            try:
                subprocess.Popen(["explorer.exe", logs_dir])
                print(f"  [OK] Đã mở thư mục: {logs_dir}")
            except Exception as exc:
                print(f"  [LỖI] Không mở được Explorer: {exc}")
            continue

        target_fp: str | None = None
        if low == "e":
            if has_latest_err:
                target_fp = latest_err_fp
            else:
                print("  Chưa có file latest_error.log nào.")
                continue
        else:
            try:
                pos = int(sel)
                if 1 <= pos <= len(items):
                    target_fp = items[pos - 1]["filepath"]
                else:
                    print(f"  Vui lòng chọn từ 1 đến {len(items)}.")
                    continue
            except ValueError:
                print("  Lựa chọn không hợp lệ.")
                continue

        if target_fp and os.path.isfile(target_fp):
            print("\n" + "#" * 68)
            print(f"# NỘI DUNG FILE LOG: {target_fp}")
            print("#" * 68)
            try:
                with open(target_fp, "r", encoding="utf-8", errors="replace") as f:
                    print(f.read().rstrip())
            except OSError as exc:
                print(f"[LỖI] Không đọc được file log: {exc}")
            print("#" * 68)
            input("Nhấn Enter để tiếp tục...")


def main() -> int:
    configure_console_utf8()
    folder = os.path.dirname(os.path.abspath(__file__))
    ensure_logs_dir(folder)
    ensure_dependencies_interactive(folder)
    _ensure_dpi_aware()
    cfg = load_config(folder)

    while True:
        scripts = discover_scripts(folder)
        folder_excel_keys = _collect_folder_excel_keys(folder, scripts)
        print("\n" + "=" * 68)
        print("  DANH SÁCH SCRIPT TỰ ĐỘNG HÓA PYTHON (CHUYỂN TỪ .AMK)")
        print("  Phím dừng khẩn cấp khi đang chạy: F12 (hoặc Ctrl+C trên console)")
        print("-" * 68)
        for key in folder_excel_keys:
            print(f"  {_format_db_status(key, cfg)}")
        print("=" * 68)
        if not scripts:
            print("  Không tìm thấy script .py nào trong thư mục này.")
            return 1

        for idx, (fn, badge) in enumerate(scripts, start=1):
            print(f"  [{idx:2d}] {fn}{badge}")
        print("  [ D] Thay đổi đường dẫn file Excel database")
        print("  [ T] Công cụ soi tọa độ, chọn vùng & chụp lại ảnh mẫu")
        print("  [ L] Xem nhật ký chạy & tra cứu lỗi (thư mục logs)")
        print("  [ 0] Thoát")
        print("-" * 68)

        choice = input("Nhập số thứ tự script muốn chạy (vd: 1, hoặc '1 --dry-run'): ").strip()
        if not choice or choice == "0":
            print("Đã thoát.")
            return 0

        low_choice = choice.lower()
        if low_choice in ("d", "db", "database"):
            configure_database_menu(folder, cfg, folder_excel_keys)
            continue

        if low_choice in ("t", "tool", "tools"):
            if folder not in sys.path:
                sys.path.insert(0, folder)
            from amk_tools import run_tools_menu

            run_tools_menu(folder)
            continue

        if low_choice in ("l", "log", "logs"):
            view_logs_menu(folder)
            continue

        parts = parse_choice_args(choice)
        try:
            num = int(parts[0])
        except ValueError:
            print("Lựa chọn không hợp lệ. Vui lòng nhập số, 'D', 'T' hoặc 'L'.")
            continue

        if num < 1 or num > len(scripts):
            print(f"Vui lòng chọn số từ 1 đến {len(scripts)}.")
            continue

        extra_args = parts[1:]
        target_fn, _ = scripts[num - 1]
        target_path = os.path.join(folder, target_fn)
        excel_keys = get_script_excel_keys(target_path)

        run_cfg = cfg
        if excel_keys and not _has_manual_excel_flag(extra_args):
            saved = cfg.get("excel_paths", {}).get(excel_keys[0], "").strip()
            if saved and not os.path.isfile(saved):
                print(
                    f"[CẢNH BÁO] File {excel_keys[0]} đã cấu hình không tồn tại:\n  {saved}"
                )
                ans = input("Tiếp tục chạy bằng chế độ tự động dò? (y = tiếp tục, n = quay lại menu): ").strip().lower()
                if ans not in ("y", "yes", "c", "co"):
                    continue
                run_cfg = {"excel_paths": {}}

        if "--dry-run" not in extra_args and not _has_manual_start_row_flag(extra_args):
            row_info = get_script_start_row_info(target_path)
            if row_info is not None and row_info[3]:
                default_row, sheet_name, raw_excel_path, _ = row_info
                chosen_row = prompt_start_row_interactive(
                    folder,
                    target_fn,
                    default_row,
                    sheet_name,
                    raw_excel_path,
                    excel_keys,
                    extra_args,
                    run_cfg,
                )
                extra_args.extend(["--start-row", str(chosen_row)])

        if "--dry-run" not in extra_args and "--start-delay" not in extra_args:
            delay_inp = input("Chờ trước khi chạy (giây, Enter = 3s để chuyển cửa sổ): ").strip()
            delay_val = "3" if delay_inp == "" else delay_inp
            try:
                if float(delay_val) > 0:
                    extra_args.extend(["--start-delay", delay_val])
            except ValueError:
                print("Giá trị giây không hợp lệ, dùng mặc định 3s.")
                extra_args.extend(["--start-delay", "3"])

        cmd = build_run_command(sys.executable, target_path, excel_keys, extra_args, run_cfg)
        display_args = " ".join(cmd[2:])
        print(f"\n>>> Đang chạy: {target_fn} {display_args}".rstrip())
        print(">>> Nhấn F12 bất kỳ lúc nào để dừng khẩn cấp.\n")
        res = subprocess.run(cmd)
        print(f"\n<<< Kết thúc {target_fn} (mã thoát: {res.returncode}).")
        input("Nhấn Enter để quay lại menu...")


if __name__ == "__main__":
    sys.exit(main())
