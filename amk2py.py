"""Convert Robot-Soft Automatic Mouse and Keyboard (.amk) scripts to standalone Python (.py)."""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from typing import Any

from amk_parser import decode_xip_image, encode_bgr_png_b64, parse_amk_file


SUPPORTED_STMT_TYPES = frozenset({2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 15, 17, 18, 29, 33, 34, 36, 43, 46, 47})
SUPPORTED_EXPR_TYPES = frozenset({43, 45, 256, 257, 258, 262, 1024, 2049, 2052, 2089})
SUPPORTED_BUILTIN_FUNCS = frozenset(
    {
        "CaptureScreen",
        "GetMousePositionX",
        "GetMousePositionY",
        "InvokeCustomFunction",
        "MessageBox",
        "Rect",
        "TypeText",
        "Variable.RefCount",
        "XLS_GetCell",
        "XLS_Open",
    }
)

VK_NAMES = {
    0x08: "Backspace",
    0x09: "Tab",
    0x0D: "Enter",
    0x1B: "Esc",
    0x20: "Space",
    0x25: "Left",
    0x26: "Up",
    0x27: "Right",
    0x28: "Down",
    0x2E: "Delete",
    0x31: "1",
    0x41: "A",
    0x43: "C",
    0x44: "D",
    0x4B: "K",
    0x4E: "N",
    0x51: "Q",
    0x53: "S",
    0x56: "V",
    0x73: "F4",
    0x74: "F5",
    0x7A: "F11",
    0x7B: "F12",
    0xBB: "Equals(+)",
}


def sanitize_var(raw: str) -> str:
    name = raw.lstrip("$")
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not cleaned:
        cleaned = "var"
    return f"v_{cleaned}"


def sanitize_func(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", raw.strip()).lower()
    if not cleaned:
        cleaned = "unnamed"
    return f"func_{cleaned}"


def parse_vk_int(raw: Any) -> int:
    if isinstance(raw, int):
        return raw
    s = str(raw).strip()
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def format_vk_comment(vk: int, ctrl: bool = False, shift: bool = False, alt: bool = False, win: bool = False) -> str:
    parts = [m for m, on in [("Ctrl", ctrl), ("Shift", shift), ("Alt", alt), ("Win", win)] if on]
    parts.append(VK_NAMES.get(vk, f"0x{vk:02X}"))
    return "+".join(parts)


def collect_vars(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        t = node.get("TYPE")
        if t == 256 and "$VAR" in node:
            out.add(sanitize_var(node["$VAR"]))
        elif t in (8, 33) and "$VAR" in node:
            out.add(sanitize_var(node["$VAR"]))
        for v in node.values():
            collect_vars(v, out)
    elif isinstance(node, list):
        for item in node:
            collect_vars(item, out)


class AmkTranspiler:
    """Transpiles a parsed AMK AST into standalone Python source code."""

    def __init__(self, source_name: str, ast: dict[str, Any]) -> None:
        self.source_name = source_name
        self.ast = ast
        self.templates: dict[str, str] = {}
        self._img_counter = 0
        self._loop_counter = 0

    def _emit_expr(self, node: Any) -> str:
        if not isinstance(node, dict):
            return repr(node)
        t = node.get("TYPE")
        if t not in SUPPORTED_EXPR_TYPES:
            raise ValueError(f"Unsupported expression TYPE {t}: {node!r}")
        if t == 256:
            return sanitize_var(node["$VAR"])
        if t == 257:
            s = str(node["$NUM"]).strip()
            try:
                return str(int(s))
            except ValueError:
                return str(float(s))
        if t == 258:
            return repr(node["$TXT"])
        if t == 262:
            return "None"
        if t == 2052:
            return repr(bool(node.get("BOOL", 0)))
        if t == 2089:
            return sanitize_func(node["$FUN"])
        if t == 43:
            return f"rt.add({self._emit_expr(node['#V1'])}, {self._emit_expr(node['#V2'])})"
        if t == 45:
            return f"rt.sub({self._emit_expr(node['#V1'])}, {self._emit_expr(node['#V2'])})"
        if t == 1024:
            return f"rt.eq({self._emit_expr(node['#V1'])}, {self._emit_expr(node['#V2'])})"
        if t == 2049:
            fun = node.get("$FUN")
            if fun not in SUPPORTED_BUILTIN_FUNCS:
                raise ValueError(f"Unsupported AMK builtin function {fun!r} in {self.source_name}")
            args = node.get("A", [])
            if fun == "MessageBox":
                return f"rt.message_box({self._emit_expr(args[0])})"
            if fun == "TypeText":
                use_clip = self._emit_expr(args[1]) if len(args) > 1 else "False"
                return f"rt.type_text({self._emit_expr(args[0])}, {use_clip})"
            if fun == "GetMousePositionX":
                return "rt.get_mouse_x()"
            if fun == "GetMousePositionY":
                return "rt.get_mouse_y()"
            if fun == "Rect":
                a = ", ".join(self._emit_expr(x) for x in args)
                return f"rt.rect({a})"
            if fun == "CaptureScreen":
                cursor = self._emit_expr(args[1]) if len(args) > 1 else "False"
                return f"rt.capture_screen_image({self._emit_expr(args[0])}, {cursor})"
            if fun == "InvokeCustomFunction":
                target_fn = self._emit_expr(args[0])
                return f"{target_fn}(rt)"
            if fun == "XLS_Open":
                vis = self._emit_expr(args[1]) if len(args) > 1 else "False"
                return f"rt.xls_open({self._emit_expr(args[0])}, {vis})"
            if fun == "XLS_GetCell":
                a = ", ".join(self._emit_expr(x) for x in args)
                return f"rt.xls_get_cell({a})"
            if fun == "Variable.RefCount":
                return "None  # Variable.RefCount (no-op)"
        raise ValueError(f"Unhandled expression TYPE {t}")

    def _register_find_img_templates(self, cmd: dict[str, Any]) -> list[tuple[str, int]]:
        pairs: list[tuple[str, int]] = []
        img_indices = sorted(
            int(k[3:]) for k in cmd.keys() if k.startswith("IMG") and k[3:].isdigit()
        )
        for idx in img_indices:
            hex_str = cmd[f"IMG{idx}"]
            bgr = decode_xip_image(hex_str)
            b64 = encode_bgr_png_b64(bgr)
            key = f"img_{self._img_counter}"
            self._img_counter += 1
            self.templates[key] = b64
            sml = int(cmd.get(f"SML{idx}", 95))
            pairs.append((key, sml))
        return pairs

    def _find_img_call_expr(self, cmd: dict[str, Any], is_if: bool) -> str:
        pairs = self._register_find_img_templates(cmd)
        if cmd.get("$WHR") == "rg" and all(k in cmd for k in ("RGNL", "RGNT", "RGNR", "RGNB")):
            region_repr = repr((int(cmd["RGNL"]), int(cmd["RGNT"]), int(cmd["RGNR"]), int(cmd["RGNB"])))
        else:
            region_repr = "None"
        tout = cmd.get("TOUT", 10)
        tun = cmd.get("$TUN", "s")
        fdo = cmd.get("$FDO", "click")
        tdo = cmd.get("$TDO", None)
        nam = cmd.get("$NAM", "")
        return (
            f"rt.find_image({pairs!r}, region={region_repr}, "
            f"timeout={tout!r}, timeout_unit={tun!r}, fdo={fdo!r}, "
            f"tdo={tdo!r}, is_if={is_if!r}, name={nam!r})"
        )

    def _find_xls_sheet_for_var(
        self, cmds: list[dict[str, Any]], start_idx: int, raw_var: str
    ) -> tuple[str, str] | None:
        """Look ahead in sibling/nested commands for XLS_GetCell(wb, sheet, $var, col)."""

        def search_expr(node: Any) -> tuple[str, str] | None:
            if isinstance(node, dict):
                if node.get("TYPE") == 2049 and node.get("$FUN") == "XLS_GetCell":
                    args = node.get("A", [])
                    if (
                        len(args) >= 3
                        and isinstance(args[2], dict)
                        and args[2].get("TYPE") == 256
                        and args[2].get("$VAR") == raw_var
                    ):
                        return self._emit_expr(args[0]), self._emit_expr(args[1])
                for v in node.values():
                    found = search_expr(v)
                    if found is not None:
                        return found
            elif isinstance(node, list):
                for item in node:
                    found = search_expr(item)
                    if found is not None:
                        return found
            return None

        return search_expr(cmds[start_idx + 1 :])

    def _emit_disabled_cmd(self, cmd: dict[str, Any], indent: int, lines: list[str]) -> None:
        pad = "    " * indent
        t = cmd.get("TYPE")
        nam = f" ({cmd['$NAM']})" if "$NAM" in cmd else ""
        if t == 34:
            expr = self._emit_expr(cmd["#COD"])
            lines.append(f"{pad}# [DISABLED]{nam} {expr}")
        elif t == 2:
            imgs = sorted(k for k in cmd.keys() if k.startswith("IMG"))
            lines.append(
                f"{pad}# [DISABLED]{nam} find_image(imgs={imgs}, whr={cmd.get('$WHR')!r}, "
                f"tout={cmd.get('TOUT')!r}{cmd.get('$TUN', 's')})"
            )
        elif t == 15:
            vk = parse_vk_int(cmd["KEY"])
            lbl = format_vk_comment(
                vk,
                bool(cmd.get("CTRL")),
                bool(cmd.get("SHFT")),
                bool(cmd.get("ALT")),
                bool(cmd.get("WIN")),
            )
            lines.append(f"{pad}# [DISABLED] key_press({lbl}, count={cmd.get('NKEY', 1)})")
        elif t == 12:
            lines.append(
                f"{pad}# [DISABLED] mouse_move_abs({self._emit_expr(cmd['#XP'])}, {self._emit_expr(cmd['#YP'])})"
            )
        elif t == 5:
            lines.append(
                f"{pad}# [DISABLED] mouse_click(button={cmd.get('MBTN', 0)}, click_type={cmd.get('CLKT', 0)})"
            )
        elif t == 8:
            lines.append(f"{pad}# [DISABLED]{nam} loop(lptp={cmd.get('LPTP')}, to={cmd.get('TO')})")
        else:
            lines.append(f"{pad}# [DISABLED]{nam} TYPE={t}")
        sub = cmd.get("#SUB", {}).get("I", [])
        if sub:
            for child in sub:
                self._emit_disabled_cmd(child, indent + 1, lines)

    def _emit_block(self, cmds: list[dict[str, Any]], indent: int, lines: list[str], *, disabled: bool = False) -> int:
        """Emit Python statements for a list of AMK commands. Returns executable stmt count."""
        pad = "    " * indent
        exec_count = 0
        i = 0
        n = len(cmds)

        while i < n:
            cmd = cmds[i]
            t = cmd.get("TYPE")
            if t not in SUPPORTED_STMT_TYPES:
                raise ValueError(f"Unsupported command TYPE {t} in {self.source_name}: {cmd!r}")

            is_dis = disabled or bool(cmd.get("DIS"))

            # Structural block openers that consume sibling ELSE/END_IF or END_LOOP or END_FUNC
            if (t == 2 and cmd.get("IF") == 1) or t == 36:
                then_cmds = cmd.get("#SUB", {}).get("I", [])
                else_cmds: list[dict[str, Any]] = []
                else_dly: int | float | None = None
                else_dis = False
                end_dly: int | float | None = None

                if i + 1 < n and cmds[i + 1].get("TYPE") == 6:
                    else_node = cmds[i + 1]
                    else_cmds = else_node.get("#SUB", {}).get("I", [])
                    else_dly = else_node.get("DLY")
                    else_dis = bool(else_node.get("DIS"))
                    i += 1
                    if i + 1 < n and cmds[i + 1].get("TYPE") == 7:
                        end_dly = cmds[i + 1].get("DLY")
                        i += 1
                elif i + 1 < n and cmds[i + 1].get("TYPE") == 7:
                    end_dly = cmds[i + 1].get("DLY")
                    i += 1

                if is_dis:
                    self._emit_disabled_cmd(cmd, indent, lines)
                    if else_cmds:
                        lines.append(f"{pad}# [DISABLED] else:")
                        for ec in else_cmds:
                            self._emit_disabled_cmd(ec, indent + 1, lines)
                else:
                    nam_cmt = f"  # {cmd['$NAM']}" if "$NAM" in cmd else ""
                    if t == 2:
                        cond = self._find_img_call_expr(cmd, is_if=True)
                    else:
                        cond = self._emit_expr(cmd["#COD"])
                    lines.append(f"{pad}if {cond}:{nam_cmt}")
                    if "DLY" in cmd:
                        lines.append(f"{pad}    rt.sleep_ms({cmd['DLY']})")
                        then_count = 1 + self._emit_block(then_cmds, indent + 1, lines)
                    else:
                        then_count = self._emit_block(then_cmds, indent + 1, lines)
                    if then_count == 0:
                        lines.append(f"{pad}    pass")

                    if else_cmds or else_dly is not None:
                        lines.append(f"{pad}else:")
                        else_count = self._emit_block(else_cmds, indent + 1, lines, disabled=else_dis)
                        if else_dly is not None and not else_dis:
                            lines.append(f"{pad}    rt.sleep_ms({else_dly})")
                            else_count += 1
                        if else_count == 0:
                            lines.append(f"{pad}    pass")

                    if end_dly is not None:
                        lines.append(f"{pad}rt.sleep_ms({end_dly})")
                    exec_count += 1
                i += 1
                continue

            if t == 8:
                body_cmds = cmd.get("#SUB", {}).get("I", [])
                end_dly = None
                if i + 1 < n and cmds[i + 1].get("TYPE") == 9:
                    end_dly = cmds[i + 1].get("DLY")
                    i += 1

                if is_dis:
                    self._emit_disabled_cmd(cmd, indent, lines)
                else:
                    lptp = cmd.get("LPTP")
                    nam_cmt = f"  # {cmd['$NAM']}" if "$NAM" in cmd else ""
                    if lptp == 0:
                        to_val = int(cmd.get("TO", 1))
                        lines.append(f"{pad}for _ in range({to_val}):{nam_cmt}")
                    elif lptp == 3:
                        self._loop_counter += 1
                        lid = f"{self.source_name}#{self._loop_counter}"
                        cond = self._emit_expr(cmd["#COD"])
                        lines.append(f"{pad}while rt.truthy({cond}) and rt.loop_guard({lid!r}):{nam_cmt}")
                    elif lptp == 4:
                        var_name = sanitize_var(cmd["$VAR"])
                        v1 = self._emit_expr(cmd["#V1"])
                        v2 = self._emit_expr(cmd["#V2"])
                        lines.append(f"{pad}for {var_name} in range(int({v1}), int({v2}) + 1):{nam_cmt}")
                    else:
                        raise ValueError(f"Unsupported LOOP LPTP={lptp} in {self.source_name}")

                    body_count = 0
                    if "DLY" in cmd:
                        lines.append(f"{pad}    rt.sleep_ms({cmd['DLY']})")
                        body_count += 1
                    body_count += self._emit_block(body_cmds, indent + 1, lines)
                    if end_dly is not None:
                        lines.append(f"{pad}    rt.sleep_ms({end_dly})")
                        body_count += 1
                    if body_count == 0:
                        lines.append(f"{pad}    pass")
                    exec_count += 1
                i += 1
                continue

            if t == 46:
                # Nested TYPE 46 inside a function (seen in Ki so F6 / Ky so F6 new): inline its body!
                sub_cmds = cmd.get("#SUB", {}).get("I", [])
                if i + 1 < n and cmds[i + 1].get("TYPE") == 47:
                    i += 1
                lines.append(f"{pad}# [Nested block: {cmd.get('$FUN', 'Entry')}]")
                exec_count += self._emit_block(sub_cmds, indent, lines, disabled=is_dis)
                i += 1
                continue

            if t in (6, 7, 9, 47):
                # Standalone closing markers if any appear unexpectedly
                i += 1
                continue

            if is_dis:
                self._emit_disabled_cmd(cmd, indent, lines)
                i += 1
                continue

            if t == 43:
                cmt = cmd.get("$CMT", "")
                lines.append(f"{pad}# {cmt}")
                i += 1
                continue

            if t == 2:
                call = self._find_img_call_expr(cmd, is_if=False)
                nam_cmt = f"  # {cmd['$NAM']}" if "$NAM" in cmd else ""
                lines.append(f"{pad}{call}{nam_cmt}")
                exec_count += 1
            elif t == 3:
                dx = self._emit_expr(cmd["#XO"])
                dy = self._emit_expr(cmd["#YO"])
                lines.append(f"{pad}rt.mouse_drag_offset({dx}, {dy})")
                exec_count += 1
            elif t == 4:
                dx = self._emit_expr(cmd["#XO"])
                dy = self._emit_expr(cmd["#YO"])
                lines.append(f"{pad}rt.mouse_move_offset({dx}, {dy})")
                exec_count += 1
            elif t == 5:
                btn = int(cmd.get("MBTN", 0))
                clkt = int(cmd.get("CLKT", 0))
                lines.append(f"{pad}rt.mouse_click(button={btn}, click_type={clkt})")
                exec_count += 1
            elif t == 11:
                lines.append(f"{pad}break")
                exec_count += 1
            elif t == 12:
                xp = self._emit_expr(cmd["#XP"])
                yp = self._emit_expr(cmd["#YP"])
                lines.append(f"{pad}rt.mouse_move_abs({xp}, {yp})")
                exec_count += 1
            elif t == 15:
                vk = parse_vk_int(cmd["KEY"])
                ctrl = bool(cmd.get("CTRL", 0))
                shift = bool(cmd.get("SHFT", 0))
                alt = bool(cmd.get("ALT", 0))
                win = bool(cmd.get("WIN", 0))
                cnt = int(cmd.get("NKEY", 1))
                cmt = format_vk_comment(vk, ctrl, shift, alt, win)
                lines.append(
                    f"{pad}rt.key_press(0x{vk:02X}, ctrl={ctrl}, shift={shift}, "
                    f"alt={alt}, win={win}, count={cnt})  # {cmt}"
                )
                exec_count += 1
            elif t == 17:
                vk = parse_vk_int(cmd["KEY"])
                lines.append(f"{pad}rt.key_down(0x{vk:02X})  # {VK_NAMES.get(vk, f'0x{vk:02X}')}")
                exec_count += 1
            elif t == 18:
                vk = parse_vk_int(cmd["KEY"])
                lines.append(f"{pad}rt.key_up(0x{vk:02X})  # {VK_NAMES.get(vk, f'0x{vk:02X}')}")
                exec_count += 1
            elif t == 29:
                tit = cmd.get("$TIT", "")
                appr = bool(cmd.get("APPR", 1))
                tout = cmd.get("TOUT", 10)
                tun = cmd.get("$TUN", "s")
                swit = bool(cmd.get("SWIT", 1))
                lines.append(
                    f"{pad}rt.wait_window({tit!r}, appear={appr!r}, timeout={tout!r}, "
                    f"unit={tun!r}, switch={swit!r})"
                )
                exec_count += 1
            elif t == 33:
                var_name = sanitize_var(cmd["$VAR"])
                val_node = cmd["#VAL"]
                val_expr = self._emit_expr(val_node)
                xls_target = None
                if (
                    indent == 1
                    and var_name == "v_x"
                    and isinstance(val_node, dict)
                    and val_node.get("TYPE") == 257
                ):
                    xls_target = self._find_xls_sheet_for_var(cmds, i, cmd["$VAR"])
                if xls_target is not None:
                    wb_expr, sheet_expr = xls_target
                    is_interactive = sheet_expr == "'Sheet5'" or (
                        "chi dinh thu thuat" in self.source_name.lower()
                    )
                    lines.append(
                        f"{pad}{var_name} = rt.get_start_row({val_expr}, {wb_expr}, "
                        f"{sheet_expr}, interactive_if_unset={is_interactive!r})"
                    )
                else:
                    lines.append(f"{pad}{var_name} = {val_expr}")
                exec_count += 1
            elif t == 34:
                expr = self._emit_expr(cmd["#COD"])
                nam_cmt = f"  # {cmd['$NAM']}" if "$NAM" in cmd else ""
                lines.append(f"{pad}{expr}{nam_cmt}")
                exec_count += 1

            if "DLY" in cmd:
                lines.append(f"{pad}rt.sleep_ms({cmd['DLY']})")
                exec_count += 1

            i += 1

        return exec_count

    def generate_python(self, runtime_source: str) -> str:
        top_cmds = self.ast.get("I", [])
        func_blocks: list[tuple[str, list[dict[str, Any]]]] = []
        i = 0
        while i < len(top_cmds):
            cmd = top_cmds[i]
            t = cmd.get("TYPE")
            if t == 46:
                fn_name = sanitize_func(cmd.get("$FUN", "Entry"))
                sub_cmds = cmd.get("#SUB", {}).get("I", [])
                func_blocks.append((fn_name, sub_cmds))
                i += 1
                if i < len(top_cmds) and top_cmds[i].get("TYPE") == 47:
                    i += 1
            elif t == 47:
                i += 1
            else:
                raise ValueError(f"Unexpected top-level TYPE {t} in {self.source_name}")

        if not func_blocks:
            func_blocks.append(("func_entry", []))

        macro_lines: list[str] = []
        entry_func = "func_entry"
        for idx, (fn_name, sub_cmds) in enumerate(func_blocks):
            if idx == 0:
                entry_func = fn_name
            var_set: set[str] = set()
            collect_vars(sub_cmds, var_set)
            macro_lines.append(f"def {fn_name}(rt: AmkRuntime) -> None:")
            if var_set:
                for vname in sorted(var_set):
                    macro_lines.append(f'    {vname}: Any = ""')
            body_lines: list[str] = []
            exec_count = self._emit_block(sub_cmds, 1, body_lines)
            macro_lines.extend(body_lines)
            if exec_count == 0:
                macro_lines.append("    pass")
            macro_lines.append("")
            macro_lines.append("")

        runtime_body = extract_embeddable_runtime(runtime_source)
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        header = [
            f'"""Standalone Python automation script converted from: {self.source_name}',
            "",
            f"Generated by amk2py.py on {now_str}.",
            "Nhấn F12 bất kỳ lúc nào để dừng khẩn cấp.",
            '"""',
            "",
            "from __future__ import annotations",
            "",
            "import argparse",
            "import base64",
            "import ctypes",
            "from ctypes import wintypes",
            "import datetime",
            "import math",
            "import os",
            "import sys",
            "import time",
            "from typing import Any, Callable",
            "",
            "import cv2",
            "import numpy as np",
            "",
            "",
            "# ===========================================================================",
            "# Macro workflow (converted from .amk)",
            "# ===========================================================================",
            "",
        ]

        templates_lines = [
            "# ===========================================================================",
            "# Embedded lossless PNG templates",
            "# ===========================================================================",
            "",
            "TEMPLATES: dict[str, str] = {",
        ]
        for k, b64 in self.templates.items():
            templates_lines.append(f"    {k!r}: {b64!r},")
        templates_lines.extend(["}", "", ""])

        footer = [
            "",
            "# ===========================================================================",
            "# Entry point",
            "# ===========================================================================",
            "",
            "def main() -> int:",
            "    return run_entry(",
            f"        {entry_func},",
            "        TEMPLATES,",
            f"        {f'Converted AMK script: {self.source_name}'!r},",
            "        __file__,",
            "    )",
            "",
            "",
            'if __name__ == "__main__":',
            "    sys.exit(main())",
            "",
        ]

        return "\n".join(header + macro_lines + templates_lines + [runtime_body] + footer)


def extract_embeddable_runtime(runtime_source: str) -> str:
    """Return runtime body after the import block so it embeds cleanly once."""
    marker = "# ---------------------------------------------------------------------------\n# Win32 constants"
    idx = runtime_source.find(marker)
    if idx < 0:
        raise ValueError("Could not locate Win32 constants marker in amk_runtime.py")
    return runtime_source[idx:].rstrip()


def convert_file(amk_path: str, out_dir: str, runtime_source: str) -> tuple[str, int, int]:
    """Convert a single .amk file into a standalone .py file in out_dir."""
    ast = parse_amk_file(amk_path)
    base_name = os.path.basename(amk_path)
    stem = os.path.splitext(base_name)[0]
    out_path = os.path.join(out_dir, f"{stem}.py")

    transpiler = AmkTranspiler(base_name, ast)
    py_code = transpiler.generate_python(runtime_source)
    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(py_code)

    line_count = py_code.count("\n") + 1
    return out_path, len(transpiler.templates), line_count


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert .amk files to standalone executable .py scripts.")
    parser.add_argument("input", help="Đường dẫn file .amk hoặc thư mục chứa các file .amk")
    parser.add_argument("-o", "--output", default=None, help="Thư mục xuất các file .py")
    args = parser.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    runtime_path = os.path.join(here, "amk_runtime.py")
    with open(runtime_path, "r", encoding="utf-8") as f:
        runtime_source = f.read()

    inp = args.input
    if os.path.isfile(inp):
        amk_files = [inp]
        default_out = os.path.dirname(inp)
    elif os.path.isdir(inp):
        amk_files = [
            os.path.join(inp, fn)
            for fn in sorted(os.listdir(inp))
            if os.path.isfile(os.path.join(inp, fn)) and fn.lower().endswith(".amk")
        ]
        default_out = os.path.join(inp, "Python")
    else:
        print(f"Lỗi: Không tìm thấy đường dẫn {inp}", file=sys.stderr)
        return 1

    if not amk_files:
        print(f"Không tìm thấy file .amk nào trong {inp}", file=sys.stderr)
        return 1

    out_dir = args.output or default_out
    print(f"Đang chuyển đổi {len(amk_files)} file .amk -> {out_dir}")
    for fp in amk_files:
        out_path, tmpl_cnt, lines = convert_file(fp, out_dir, runtime_source)
        print(f"  [OK] {os.path.basename(fp)} -> {os.path.basename(out_path)} ({tmpl_cnt} templates, {lines} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
