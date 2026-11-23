"""Công cụ soi tọa độ chuột, kéo chọn vùng và chụp lại ảnh mẫu cho script Python (.amk)."""

from __future__ import annotations

import ast
import base64
import ctypes
from ctypes import wintypes
import datetime
import os
import py_compile
import re
import shutil
import sys
import tempfile
import time
from typing import Any

import cv2
import numpy as np

from amk_runtime import (
    BITMAPINFO,
    BITMAPINFOHEADER,
    DIB_RGB_COLORS,
    SRCCOPY,
    _enable_dpi_awareness,
)


HELPER_FILES = frozenset(
    {
        "run_menu.py",
        "amk2py.py",
        "amk_parser.py",
        "amk_runtime.py",
        "amk_tools.py",
    }
)


def _img_sort_key(key: str) -> tuple[int, str]:
    m = re.match(r"^img_(\d+)$", key)
    if m:
        return (int(m.group(1)), key)
    return (999999, key)


def inspect_script_templates(script_path: str) -> list[dict[str, Any]]:
    """Inspect a converted .py script and return ordered metadata for each template."""
    with open(script_path, "r", encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source, filename=script_path)
    templates_dict: dict[str, str] = {}
    usage_map: dict[str, dict[str, Any]] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "TEMPLATES" and node.value is not None:
                val = ast.literal_eval(node.value)
                if isinstance(val, dict):
                    templates_dict = {str(k): str(v) for k, v in val.items()}
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "TEMPLATES":
                    val = ast.literal_eval(node.value)
                    if isinstance(val, dict):
                        templates_dict = {str(k): str(v) for k, v in val.items()}
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "find_image"
            and node.args
        ):
            try:
                pairs = ast.literal_eval(node.args[0])
            except Exception:
                continue
            kw_map = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            region = None
            name = ""
            if "region" in kw_map:
                try:
                    region = ast.literal_eval(kw_map["region"])
                except Exception:
                    region = None
            if "name" in kw_map:
                try:
                    name = str(ast.literal_eval(kw_map["name"]) or "")
                except Exception:
                    name = ""
            if isinstance(pairs, (list, tuple)):
                for item in pairs:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        k = str(item[0])
                        sml = item[1]
                        if k not in usage_map:
                            usage_map[k] = {
                                "line_no": int(node.lineno),
                                "name": name,
                                "sml": sml,
                                "region": region,
                            }

    results: list[dict[str, Any]] = []
    for key in sorted(templates_dict.keys(), key=_img_sort_key):
        b64 = templates_dict[key]
        raw = base64.b64decode(b64)
        arr = np.frombuffer(raw, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        h, w = (int(bgr.shape[0]), int(bgr.shape[1])) if bgr is not None else (0, 0)
        info = usage_map.get(key, {})
        results.append(
            {
                "key": key,
                "line_no": info.get("line_no", 0),
                "name": info.get("name", ""),
                "sml": info.get("sml", 95),
                "region": info.get("region", None),
                "width": w,
                "height": h,
                "b64": b64,
            }
        )
    return results


def export_script_templates(
    script_path: str, out_dir: str, only_key: str | None = None
) -> list[str]:
    """Decode embedded templates from script_path to lossless PNG files in out_dir."""
    items = inspect_script_templates(script_path)
    if only_key is not None:
        items = [it for it in items if it["key"] == only_key]
        if not items:
            raise KeyError(f"Không tìm thấy template {only_key!r} trong {script_path}")

    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    for it in items:
        raw_png = base64.b64decode(it["b64"])
        arr = np.frombuffer(raw_png, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            raise ValueError(f"Ảnh mẫu {it['key']} bị hỏng dữ liệu PNG")
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", it["name"]).strip("_")
        suffix = f"_{safe_name}" if safe_name else ""
        fn = f"L{it['line_no']:03d}_{it['key']}{suffix}.png"
        out_path = os.path.join(out_dir, fn)
        ok, buf = cv2.imencode(".png", bgr)
        if not ok:
            raise RuntimeError(f"Không thể ghi PNG cho {it['key']}")
        with open(out_path, "wb") as f:
            f.write(buf.tobytes())
        written.append(out_path)
    return written


def replace_template_in_script(
    script_path: str,
    key: str,
    new_bgr: np.ndarray,
    *,
    new_region: tuple[int, int, int, int] | None = None,
    backup_dir: str | None = None,
) -> dict[str, Any]:
    """Surgically replace TEMPLATES[key] (and optional region) with automatic backup."""
    if new_bgr is None or new_bgr.ndim != 3 or new_bgr.shape[0] < 1 or new_bgr.shape[1] < 1:
        raise ValueError("Ảnh mẫu mới không hợp lệ")

    ok, buf = cv2.imencode(".png", new_bgr)
    if not ok:
        raise RuntimeError("Không thể mã hóa ảnh mẫu mới sang PNG")
    new_b64 = base64.b64encode(buf.tobytes()).decode("ascii")

    with open(script_path, "r", encoding="utf-8") as f:
        orig_text = f.read()

    lines = orig_text.splitlines(keepends=True)
    in_templates = False
    replaced_tpl = False
    templates_start_idx = len(lines)
    tpl_pat = re.compile(
        rf"^(\s*['\"]{re.escape(key)}['\"]\s*:\s*)['\"][^'\"]*['\"](\s*,?\s*\r?\n?)$"
    )

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not in_templates and stripped.startswith("TEMPLATES") and stripped.endswith("{"):
            in_templates = True
            templates_start_idx = idx
            continue
        if in_templates:
            if stripped == "}":
                break
            m = tpl_pat.match(line)
            if m:
                lines[idx] = f"{m.group(1)}{new_b64!r}{m.group(2)}"
                replaced_tpl = True
                break

    if not replaced_tpl:
        raise KeyError(f"Không tìm thấy mục '{key}' trong từ điển TEMPLATES của {script_path}")

    region_updated = False
    matched_line_no = 0
    if new_region is not None:
        reg_tuple = tuple(int(v) for v in new_region)
        key_token_1 = f"'{key}'"
        key_token_2 = f'"{key}"'
        reg_pat = re.compile(r"region\s*=\s*(?:None|\([^)]*\))")
        for idx in range(templates_start_idx):
            ln = lines[idx]
            if "rt.find_image(" in ln and (key_token_1 in ln or key_token_2 in ln):
                new_ln, cnt = reg_pat.subn(f"region={reg_tuple!r}", ln, count=1)
                if cnt == 1:
                    lines[idx] = new_ln
                    region_updated = True
                    matched_line_no = idx + 1
                    break

    script_abs = os.path.abspath(script_path)
    script_dir = os.path.dirname(script_abs)
    stem = os.path.splitext(os.path.basename(script_abs))[0]
    bdir = backup_dir or os.path.join(script_dir, "backups")
    os.makedirs(bdir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(bdir, f"{stem}_{ts}.py")
    shutil.copy2(script_abs, backup_path)

    new_text = "".join(lines)
    with open(script_abs, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_text)

    # Verify syntax via py_compile into a temp .pyc so no __pycache__ is left behind
    tmp_pyc = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pyc", delete=False) as tf:
            tmp_pyc = tf.name
        py_compile.compile(script_abs, cfile=tmp_pyc, doraise=True)
    except Exception:
        shutil.copy2(backup_path, script_abs)
        raise
    finally:
        if tmp_pyc and os.path.exists(tmp_pyc):
            try:
                os.remove(tmp_pyc)
            except OSError:
                pass

    return {
        "backup_path": backup_path,
        "key": key,
        "width": int(new_bgr.shape[1]),
        "height": int(new_bgr.shape[0]),
        "region_updated": region_updated,
        "line_no": matched_line_no,
    }


def grab_full_screen_bgr() -> np.ndarray:
    """Capture the primary screen (1:1 physical DPI-aware pixels) into a BGR numpy array."""
    _enable_dpi_awareness()
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    w = int(user32.GetSystemMetrics(0))
    h = int(user32.GetSystemMetrics(1))

    hdc_screen = user32.GetDC(None)
    if not hdc_screen:
        raise RuntimeError("GetDC(0) failed")
    hdc_mem = None
    hbmp = None
    old_obj = None
    try:
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
        old_obj = gdi32.SelectObject(hdc_mem, hbmp)
        if not gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, 0, 0, SRCCOPY):
            raise RuntimeError("BitBlt failed")

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0

        buf = (ctypes.c_ubyte * (w * h * 4))()
        rows = gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
        if rows != h:
            raise RuntimeError(f"GetDIBits returned {rows} rows, expected {h}")
        bgra = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4))
        return bgra[:, :, :3].copy()
    finally:
        if hdc_mem and old_obj:
            gdi32.SelectObject(hdc_mem, old_obj)
        if hbmp:
            gdi32.DeleteObject(hbmp)
        if hdc_mem:
            gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)


def select_screen_region_gui(
    screen_bgr: np.ndarray, prompt_text: str = "Kéo chuột trái để chọn vùng"
) -> tuple[int, int, int, int] | None:
    """Open a DPI-aware fullscreen overlay to drag-select (L, T, R, B). Returns None on cancel."""
    _enable_dpi_awareness()
    import tkinter as tk

    sh, sw = screen_bgr.shape[:2]
    ok, png_buf = cv2.imencode(".png", screen_bgr)
    if not ok:
        raise RuntimeError("Không thể nén ảnh màn hình sang PNG")
    b64_png = base64.b64encode(png_buf.tobytes()).decode("ascii")

    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    root.geometry(f"{sw}x{sh}+0+0")
    root.attributes("-topmost", True)
    root.configure(cursor="crosshair")

    photo = tk.PhotoImage(master=root, data=b64_png)
    canvas = tk.Canvas(root, width=sw, height=sh, highlightthickness=0, bd=0, cursor="crosshair")
    canvas.pack(fill="both", expand=True)
    canvas.create_image(0, 0, anchor="nw", image=photo)

    # Top instruction banner
    banner_w = min(sw - 40, 860)
    bx0 = (sw - banner_w) // 2
    canvas.create_rectangle(bx0, 10, bx0 + banner_w, 44, fill="#111111", outline="#FFCC00", width=2)
    canvas.create_text(
        sw // 2,
        27,
        text=f"{prompt_text}  |  Kéo chuột trái để khoanh vùng — ESC hoặc chuột phải để hủy",
        fill="#FFCC00",
        font=("Segoe UI", 11, "bold"),
    )

    state: dict[str, Any] = {
        "start": None,
        "rect_id": None,
        "hud_bg": None,
        "hud_txt": None,
        "result": None,
    }

    def on_press(event: Any) -> None:
        state["start"] = (int(event.x), int(event.y))

    def on_drag(event: Any) -> None:
        if state["start"] is None:
            return
        sx, sy = state["start"]
        ex, ey = max(0, min(sw, int(event.x))), max(0, min(sh, int(event.y)))
        l, r = min(sx, ex), max(sx, ex)
        t, b = min(sy, ey), max(sy, ey)
        if state["rect_id"] is not None:
            canvas.delete(state["rect_id"])
        if state["hud_bg"] is not None:
            canvas.delete(state["hud_bg"])
        if state["hud_txt"] is not None:
            canvas.delete(state["hud_txt"])

        state["rect_id"] = canvas.create_rectangle(l, t, r, b, outline="#FF2222", width=2)
        hud_label = f"({l}, {t}, {r}, {b})  |  {r - l} x {b - t} px"
        ty = t - 26 if t > 65 else b + 12
        tx = min(max(10, l), max(10, sw - 260))
        state["hud_bg"] = canvas.create_rectangle(
            tx - 4, ty - 2, tx + 250, ty + 20, fill="#111111", outline="#FF2222", width=1
        )
        state["hud_txt"] = canvas.create_text(
            tx + 4, ty + 9, anchor="w", text=hud_label, fill="#FFFFFF", font=("Consolas", 10, "bold")
        )

    def on_release(event: Any) -> None:
        if state["start"] is None:
            return
        sx, sy = state["start"]
        ex, ey = max(0, min(sw, int(event.x))), max(0, min(sh, int(event.y)))
        l, r = min(sx, ex), max(sx, ex)
        t, b = min(sy, ey), max(sy, ey)
        if (r - l) >= 2 and (b - t) >= 2:
            state["result"] = (l, t, r, b)
        root.quit()
        root.destroy()

    def on_cancel(_event: Any = None) -> None:
        state["result"] = None
        root.quit()
        root.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    canvas.bind("<Button-3>", on_cancel)
    root.bind("<Escape>", on_cancel)

    root.deiconify()
    root.focus_force()
    root.mainloop()
    return state["result"]


def _drain_console_keys() -> None:
    try:
        import msvcrt

        while msvcrt.kbhit():
            msvcrt.getwch()
    except Exception:
        pass


def _prompt_countdown(default_sec: int = 3) -> None:
    raw = input(f"Chờ trước khi chụp màn hình (giây, Enter = {default_sec}s): ").strip()
    sec = float(default_sec) if raw == "" else max(0.0, float(raw))
    for rem in range(int(sec), 0, -1):
        print(f"  Chụp màn hình sau {rem}s...", flush=True)
        time.sleep(1.0)
    frac = sec - int(sec)
    if frac > 0:
        time.sleep(frac)


def _discover_macro_scripts(folder: str) -> list[str]:
    scripts: list[str] = []
    for fn in sorted(os.listdir(folder), key=lambda s: s.lower()):
        if not fn.lower().endswith(".py") or fn in HELPER_FILES or fn.startswith("test_"):
            continue
        fp = os.path.join(folder, fn)
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                head = f.read(8192)
        except OSError:
            continue
        if "def func_entry(" in head:
            scripts.append(fn)
    return scripts


def run_coord_inspector() -> None:
    """Real-time physical (1920x1080 DPI-aware) mouse coordinate & offset inspector."""
    _enable_dpi_awareness()
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = wintypes.SHORT
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.GetPixel.restype = wintypes.DWORD

    print("\n" + "-" * 68)
    print("  SOI TỌA ĐỘ CHUỘT (ĐỘ PHÂN GIẢI VẬT LÝ DPI-AWARE)")
    print("  - Di chuột đến vị trí cần lấy trên màn hình HIS:")
    print("  - Nhấn F8  : Ghi nhận tọa độ A  -> rt.mouse_move_abs(x, y)")
    print("  - Nhấn F9  : Tính độ lệch từ A -> rt.mouse_move_offset / mouse_drag_offset")
    print("  - Nhấn ESC : Quay lại menu")
    print("-" * 68)

    VK_F8, VK_F9, VK_ESCAPE = 0x77, 0x78, 0x1B
    # Clear stale key edge state
    user32.GetAsyncKeyState(VK_F8)
    user32.GetAsyncKeyState(VK_F9)
    user32.GetAsyncKeyState(VK_ESCAPE)

    anchor: tuple[int, int] | None = None
    prev_f8 = prev_f9 = False
    pt = wintypes.POINT()

    while True:
        user32.GetCursorPos(ctypes.byref(pt))
        x, y = int(pt.x), int(pt.y)
        hdc = user32.GetDC(None)
        bgr_str = ""
        if hdc:
            try:
                c = int(gdi32.GetPixel(hdc, x, y))
                r, g, b = c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF
                bgr_str = f"BGR=({b:3d},{g:3d},{r:3d})"
            finally:
                user32.ReleaseDC(None, hdc)

        anchor_txt = f" | Neo A={anchor}" if anchor else ""
        print(f"\r  Chuột: ({x:4d}, {y:4d})  {bgr_str}{anchor_txt}    ", end="", flush=True)

        f8 = bool(user32.GetAsyncKeyState(VK_F8) & 0x8000)
        f9 = bool(user32.GetAsyncKeyState(VK_F9) & 0x8000)
        esc = bool(user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)

        if esc:
            print("\n  Đã thoát chế độ soi tọa độ.")
            _drain_console_keys()
            return

        if f8 and not prev_f8:
            anchor = (x, y)
            print(f"\n  [F8] Tọa độ A = ({x}, {y})  {bgr_str}")
            print(f"       -> rt.mouse_move_abs({x}, {y})")

        if f9 and not prev_f9:
            if anchor is None:
                print("\n  [F9] Hãy nhấn F8 tại điểm gốc A trước khi nhấn F9.")
            else:
                dx, dy = x - anchor[0], y - anchor[1]
                print(f"\n  [F9] Từ A{anchor} đến ({x}, {y}): dx={dx}, dy={dy}")
                print(f"       -> rt.mouse_move_offset({dx}, {dy})")
                print(f"       -> rt.mouse_drag_offset({dx}, {dy})")

        prev_f8, prev_f9 = f8, f9
        time.sleep(0.05)


def run_region_picker() -> None:
    """Capture screen after countdown and print region=(L, T, R, B) from drag selection."""
    print("\n" + "-" * 68)
    print("  KÉO CHỌN VÙNG TÌM KIẾM region=(L, T, R, B)")
    print("-" * 68)
    try:
        _prompt_countdown(3)
    except ValueError:
        print("Giá trị giây không hợp lệ.")
        return
    screen = grab_full_screen_bgr()
    rect = select_screen_region_gui(screen, "Kéo chọn vùng cần đo tọa độ region=(L, T, R, B)")
    if rect is None:
        print("  Đã hủy chọn vùng.")
        return
    l, t, r, b = rect
    w, h = r - l, b - t
    cx, cy = l + w // 2, t + h // 2
    print(f"\n  [OK] Vùng đã chọn: region=({l}, {t}, {r}, {b})")
    print(f"       Kích thước  : {w} x {h} px | Tâm: ({cx}, {cy})")


def _pick_script_file(folder: str) -> str | None:
    scripts = _discover_macro_scripts(folder)
    if not scripts:
        print("  Không tìm thấy script .py nào trong thư mục này.")
        return None
    print("\n  Danh sách script:")
    for idx, fn in enumerate(scripts, start=1):
        print(f"    [{idx:2d}] {fn}")
    print("    [ 0] Quay lại")
    raw = input("  Chọn số thứ tự script: ").strip()
    if not raw or raw == "0":
        return None
    try:
        pos = int(raw)
    except ValueError:
        print("  Lựa chọn không hợp lệ.")
        return None
    if pos < 1 or pos > len(scripts):
        print(f"  Vui lòng chọn từ 1 đến {len(scripts)}.")
        return None
    return os.path.join(folder, scripts[pos - 1])


def _print_script_templates(items: list[dict[str, Any]]) -> None:
    print("\n  Danh sách ảnh mẫu nhúng trong script:")
    for idx, it in enumerate(items, start=1):
        line_str = f"L{it['line_no']:03d}" if it["line_no"] else "L---"
        name_str = f" | {it['name']!r}" if it["name"] else ""
        print(
            f"    [{idx:2d}] {it['key']:7s} | {line_str} | {it['width']}x{it['height']} px"
            f" | sml={it['sml']} | region={it['region']}{name_str}"
        )


def run_template_recapture(folder: str) -> None:
    """Interactive flow to recapture and surgically replace TEMPLATES['img_X'] in a script."""
    script_path = _pick_script_file(folder)
    if not script_path:
        return
    items = inspect_script_templates(script_path)
    if not items:
        print("  Script này không chứa ảnh mẫu nào trong TEMPLATES.")
        return

    _print_script_templates(items)
    raw_sel = input("  Chọn STT hoặc tên ảnh mẫu cần chụp lại (vd: 1 hoặc img_5, Enter = hủy): ").strip()
    if not raw_sel:
        return

    target_item = None
    if raw_sel.isdigit():
        idx = int(raw_sel)
        if 1 <= idx <= len(items):
            target_item = items[idx - 1]
    else:
        for it in items:
            if it["key"] == raw_sel:
                target_item = it
                break
    if target_item is None:
        print("  Không tìm thấy ảnh mẫu đã chọn.")
        return

    key = target_item["key"]
    print(
        f"\n  Chuẩn bị chụp lại {key} (cũ: {target_item['width']}x{target_item['height']} px, "
        f"dòng L{target_item['line_no']:03d})."
    )
    print("  Hãy chuyển sang cửa sổ phần mềm HIS trước khi hết đếm ngược.")
    try:
        _prompt_countdown(3)
    except ValueError:
        print("  Giá trị giây không hợp lệ.")
        return

    screen = grab_full_screen_bgr()
    rect = select_screen_region_gui(screen, f"Khoanh vùng biểu tượng mới cho {key}")
    if rect is None:
        print("  Đã hủy chụp lại ảnh mẫu.")
        return

    l, t, r, b = rect
    new_bgr = screen[t:b, l:r].copy()
    print(f"\n  Đã cắt ảnh mới cho {key}: {r - l}x{b - t} px tại ({l}, {t}, {r}, {b}).")

    new_region: tuple[int, int, int, int] | None = None
    if target_item["region"] is not None:
        print(f"  Vùng region hiện tại của {key}: {target_item['region']}")
    ans_reg = input(
        "  Cập nhật cả region? (n = giữ nguyên, y = đặt đúng vùng vừa chọn, p = mở rộng +-30px): "
    ).strip().lower()
    if ans_reg == "y":
        new_region = (l, t, r, b)
    elif ans_reg == "p":
        sh, sw = screen.shape[:2]
        new_region = (max(0, l - 30), max(0, t - 30), min(sw, r + 30), min(sh, b + 30))

    info = replace_template_in_script(script_path, key, new_bgr, new_region=new_region)
    print(f"\n  [OK] Đã cập nhật {key} ({info['width']}x{info['height']} px) vào {os.path.basename(script_path)}.")
    print(f"       Bản sao lưu : {info['backup_path']}")
    if new_region is not None:
        if info["region_updated"]:
            print(f"       Đã cập nhật region={new_region} tại dòng L{info['line_no']:03d}.")
        else:
            print("       Không tự động đổi được dòng region (vui lòng kiểm tra thủ công nếu cần).")


def run_template_exporter(folder: str) -> None:
    """Export embedded PNG templates of a selected script for visual inspection."""
    script_path = _pick_script_file(folder)
    if not script_path:
        return
    items = inspect_script_templates(script_path)
    if not items:
        print("  Script này không chứa ảnh mẫu nào.")
        return

    _print_script_templates(items)
    raw_sel = input("  Nhập tên ảnh (vd: img_0) hoặc Enter để xuất TẤT CẢ: ").strip()
    only_key = raw_sel if raw_sel else None
    stem = os.path.splitext(os.path.basename(script_path))[0]
    out_dir = os.path.join(folder, "exported_templates", stem)
    try:
        written = export_script_templates(script_path, out_dir, only_key=only_key)
    except Exception as exc:
        print(f"  [LỖI] {exc}")
        return

    print(f"\n  [OK] Đã xuất {len(written)} ảnh PNG vào:\n       {out_dir}")
    ans_open = input("  Mở thư mục chứa ảnh ngay? (y/N): ").strip().lower()
    if ans_open in ("y", "yes", "c", "co"):
        try:
            os.startfile(out_dir)  # type: ignore[attr-defined]
        except Exception as exc:
            print(f"  Không mở được Explorer: {exc}")


def run_tools_menu(folder: str) -> None:
    """Interactive sub-menu for coordinate picking, region selection, and template recapture."""
    _enable_dpi_awareness()
    while True:
        print("\n" + "=" * 68)
        print("  CÔNG CỤ SOI TỌA ĐỘ, CHỌN VÙNG & SỬA ẢNH MẪU")
        print("=" * 68)
        print("  [1] Soi tọa độ chuột (x, y) theo thời gian thực (F8 lấy điểm, F9 đo lệch)")
        print("  [2] Kéo chọn vùng trên màn hình để lấy region=(L, T, R, B)")
        print("  [3] Chụp lại & cập nhật ảnh mẫu TEMPLATES['img_X'] trong file .py")
        print("  [4] Xuất ảnh mẫu của script ra file .png để kiểm tra")
        print("  [0] Quay lại menu chính")
        print("-" * 68)

        choice = input("Chọn công cụ (0-4): ").strip()
        if not choice or choice == "0":
            return
        if choice == "1":
            run_coord_inspector()
        elif choice == "2":
            run_region_picker()
        elif choice == "3":
            run_template_recapture(folder)
        elif choice == "4":
            run_template_exporter(folder)
        else:
            print("Lựa chọn không hợp lệ. Vui lòng chọn từ 0 đến 4.")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    run_tools_menu(os.path.dirname(os.path.abspath(__file__)))
