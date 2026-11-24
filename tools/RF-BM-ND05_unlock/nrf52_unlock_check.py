#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nRF52840 APPROTECT status, explicitly authorized mass-erase unlock, and SWD verification.
Requires pyOCD CLI. Default commands are read-only. Python 3.9+.
"""
import argparse
import re
import shutil
import subprocess
import sys
import time

CTRL_IDR = 0x02880000
AHB_IDR = 0x24770011
CPUID = 0x410FC241


def pyocd_run(args, commands, no_init=True):
    exe = shutil.which(args.pyocd)
    if exe is None:
        raise RuntimeError(f"找不到 {args.pyocd}。请先运行 python -m pip install -U pyocd")
    argv = [exe, "commander", "-t", "nrf52840", "-f", str(args.frequency),
            "-O", "auto_unlock=false"]
    if args.uid:
        argv.extend(["-u", args.uid])
    if no_init:
        argv.append("-N")
    # Commander supports -c/--command. 'exit' makes the call noninteractive.
    argv.extend(["-c", "; ".join(commands + ["exit"])])
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=args.timeout, check=False)
    except subprocess.TimeoutExpired as ex:
        raise RuntimeError(f"PyOCD 超时（{args.timeout}s），请检查目标电源及调试器") from ex
    output = (p.stdout or "") + "\n" + (p.stderr or "")
    if args.verbose:
        print("--- PyOCD 原始输出 ---\n" + output.strip() + "\n--------------------")
    return p.returncode, output


def ap_reg(output, ap, offset):
    # Example: AP register 0x100000c = 0x00000001
    addr = (ap << 24) | offset
    match = re.search(rf"AP register\s+0x{addr:x}\s*=\s*0x([0-9a-fA-F]+)", output, re.I)
    return int(match.group(1), 16) if match else None


def mem_reg(output, addr):
    # Example: e000ed00:  410fc241 |A..A|
    match = re.search(rf"(?m)^\s*(?:0x)?{addr:08x}:\s*(?:0x)?([0-9a-fA-F]{{8}})\b", output, re.I)
    return int(match.group(1), 16) if match else None


def ctrl_check(args):
    _, out = pyocd_run(args, ["initdp", "makeap 1", "readap 1 0x0fc",
                              "readap 1 0x008", "readap 1 0x00c"])
    ident = ap_reg(out, 1, 0xfc)
    busy = ap_reg(out, 1, 0x08)
    protect = ap_reg(out, 1, 0x0c)
    print(f"CTRL-AP IDR       : {fmt(ident)}")
    print(f"ERASEALLSTATUS    : {fmt(busy)}")
    print(f"APPROTECTSTATUS   : {fmt(protect)}")
    if ident != CTRL_IDR:
        print("[失败] 无法确认 Nordic CTRL-AP，检查 SWD/VTref/供电/调试器。")
        return None
    if busy != 0:
        print("[警告] 擦除控制器非空闲或状态读取失败，不要开始其他操作。")
        return None
    if protect == 0:
        print("[锁定] APPROTECT 已启用，AHB-AP/CPU 可能不可访问。")
    elif protect == 1:
        print("[解锁] 当前上电周期 APPROTECT 已禁用。")
    else:
        print("[未知] 保护状态异常，不能据此宣称已解锁。")
        return None
    return protect


def has_pyocd_error(rc, output):
    # Commander may return zero even when an individual command failed.
    return rc != 0 or bool(re.search(r"(?im)(?:^|\n)\s*(?:error:|.*\b(?:Traceback|ERROR:|failed|timed out)\b)", output))


def show_failure(rc, output):
    print(f"[PyOCD] 退出码：{rc}")
    print("--- PyOCD 原始错误/输出 ---")
    print(output.strip() or "（没有输出）")
    print("--------------------------")


def fmt(value):
    return f"0x{value:08X}" if value is not None else "未读到"


def verify(args):
    print("\n[1/2] 读取 CTRL-AP（只读，绝不自动解锁）")
    protect = ctrl_check(args)
    if protect is None:
        return 2
    if protect == 0:
        print("\n[结论] 芯片受保护；未尝试访问受保护内存，也未执行擦除。")
        return 1
    print("\n[2/2] 检查 AHB-AP 与 CPU（只读）")
    _, ap_out = pyocd_run(args, ["initdp", "makeap 0"])
    m = re.search(r"AP#0 IDR\s*=\s*0x([0-9a-fA-F]+)", ap_out, re.I)
    ap0 = int(m.group(1), 16) if m else None
    print("AHB-AP IDR        :", fmt(ap0))
    if ap0 != AHB_IDR:
        print("[结论] 保护已解除，但 AHB-AP IDR 异常。")
        return 2
    # -N + reinit was observed to restore access after mass erase in this case.
    _, mem_out = pyocd_run(args, ["initdp", "makeap 0", "reinit",
                                   "read32 0xE000ED00", "read32 0x10000100",
                                   "read32 0x10000104", "read32 0x10001208",
                                   "read32 0x00000000"])
    cpuid = mem_reg(mem_out, 0xe000ed00)
    part = mem_reg(mem_out, 0x10000100)
    variant = mem_reg(mem_out, 0x10000104)
    uicr = mem_reg(mem_out, 0x10001208)
    vector = mem_reg(mem_out, 0x00000000)
    for name, value in [("Cortex-M4 CPUID", cpuid), ("FICR.PART", part),
                        ("FICR.VARIANT", variant), ("UICR.APPROTECT", uicr),
                        ("Flash[0]", vector)]:
        print(f"{name:19}: {fmt(value)}")
    if cpuid != CPUID or part != 0x52840:
        print("[结论] AHB-AP 已识别，但 CPU/芯片信息读取未通过。可加 --verbose 诊断。")
        return 2
    variant_txt = "".join(chr((variant >> shift) & 0xff) for shift in (24, 16, 8, 0)) if variant is not None else "?"
    print(f"芯片版本          : {variant_txt}")
    if vector == 0xffffffff:
        print("[提示] Flash 起始字处于擦除状态；空白芯片显示 Lockup 并不等于 SWD 故障。")
    if variant_txt == "AAD0" and uicr == 0xffffffff:
        print("[提示] AAD0 旧版保护机制：UICR.APPROTECT 擦除态符合禁用保护状态。")
    elif uicr is not None:
        print("[提示] 不要仅凭 UICR 值判断其他版本芯片重新上电后的保护状态。")
    print("\n[通过] 当前上电周期：CTRL-AP 开放，AHB-AP 与 CPU 可访问。")
    print("[提醒] 持久解锁必须真实断开目标 VCC、重新上电后再运行 verify。")
    return 0


def unlock(args):
    print("[预检查] 先读取保护状态，不进行任何写入。")
    current = ctrl_check(args)
    if current is None:
        return 2
    if current == 1:
        print("已经解锁，无需擦除。继续验证：")
        return verify(args)
    print("\n!!! 不可逆风险：解锁将执行 MASS ERASE，清除原有 Flash 固件和 UICR 配置。")
    print("!!! 这不是备份操作；不能恢复被擦除的原厂固件。")
    if not args.erase_all:
        print("未执行。要解锁需显式指定 --erase-all，并在交互确认时输入 ERASE。")
        return 1
    if not sys.stdin.isatty():
        print("[拒绝] 解锁必须在交互终端人工确认，禁止管道或无人值守运行。")
        return 2
    token = input("确认永久擦除目标全部固件？请输入大写 ERASE：").strip()
    if token != "ERASE":
        print("操作取消，芯片未擦除。")
        return 1
    # Nordic CTRL-AP ERASEALL register: AP#1 offset 0x004.
    # This is a destructive one-shot command, only reached after checking the
    # exact AP ID, lock status, and explicit interactive confirmation.
    # Do NOT automatically retry the write if the command returns an error:
    # the erase may already have started even if the host lost the response.
    print("[执行] CTRL-AP ERASEALL：writeap 1 0x004 1（只触发一次）")
    rc, output = pyocd_run(args, ["initdp", "makeap 1", "writeap 1 0x004 1"])
    if has_pyocd_error(rc, output):
        print("[警告] 发起 ERASEALL 的命令返回异常；擦除是否已开始尚不明确。")
        show_failure(rc, output)
        print("[处理] 不会自动重发擦除命令。先运行 verify 查看最新状态。")
        return 2

    print("[轮询] 等待擦除完成并读取 APPROTECTSTATUS（后续均为只读）")
    deadline = time.monotonic() + args.erase_timeout
    last_status = None
    while time.monotonic() < deadline:
        rc, out = pyocd_run(args, ["initdp", "makeap 1", "readap 1 0x008", "readap 1 0x00C"])
        if has_pyocd_error(rc, out):
            show_failure(rc, out)
            print("[警告] 状态读取失败，解锁结果不确定；请重新运行 verify。")
            return 2
        busy = ap_reg(out, 1, 0x008)
        status = ap_reg(out, 1, 0x00c)
        if (busy, status) != last_status:
            print(f"ERASEALLSTATUS={fmt(busy)}  APPROTECTSTATUS={fmt(status)}")
            last_status = busy, status
        if busy == 0 and status == 1:
            print("[成功] CTRL-AP 报告擦除已结束且 APPROTECT 已解除。")
            print("[复核] 尝试只读验证 CPU 访问：")
            result = verify(args)
            if result != 0:
                print("[提示] CTRL-AP 解锁成功，但 CPU 验证尚未通过。可在独立 verify 进程中重试；不要再次擦除。")
            return result
        if busy not in (0, 1) or status not in (0, 1):
            print("[异常] 寄存器读数非法或缺失，停止操作，不会重复擦除。")
            print(out.strip())
            return 2
        time.sleep(1)

    print(f"[超时] {args.erase_timeout}s 内未观察到 APPROTECTSTATUS=1；不会再次触发擦除。")
    print("[处理] 请运行 verify --verbose，检查目标供电与 CTRL-AP 返回值。")
    return 2



def list_probes(args):
    """列出本机 CMSIS-DAP，不连接目标，不改动芯片。"""
    exe = shutil.which(args.pyocd)
    if exe is None:
        print("[错误] 找不到 pyocd。请执行 python -m pip install -U pyocd")
        return 2
    try:
        result = subprocess.run([exe, "list"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=args.timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[错误] 无法列举调试器：{exc}")
        return 2
    print((result.stdout or "") + (result.stderr or ""))
    return 0 if result.returncode == 0 else 2


def interactive_menu(args):
    """无参数启动时的主菜单；所有破坏性动作必须二次确认。"""
    if not sys.stdin.isatty():
        print("[提示] 当前非交互终端。请使用 verify、unlock --erase-all 等命令参数。")
        return 2
    while True:
        print("\n" + "=" * 56)
        print(" RF-BM-ND05 / ND05I  nRF52840 SWD 排障工具")
        print("=" * 56)
        print(f" 调试器 UID : {args.uid or '自动选择'}")
        print(f" SWD 频率   : {args.frequency} Hz")
        print(f" 详细输出   : {'开启' if args.verbose else '关闭'}")
        print(" 1. 检测保护状态 / 验证 SWD（只读，推荐）")
        print(" 2. 解锁 APPROTECT（整片擦除！须输入 ERASE）")
        print(" 3. 断电重启后验证（只读；需手动断电上电）")
        print(" 4. 列出已连接的 CMSIS-DAP 调试器")
        print(" 5. 设置调试器 UID、SWD 频率、详细输出")
        print(" 0. 退出")
        try:
            choice = input("\n请选择 [0-5]：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return 0
        if choice == "0":
            return 0
        if choice == "5":
            print("\n当前可通过 pyocd list 查看调试器 UID。")
            try:
                uid = input(f"调试器 UID（回车保留当前，输入 auto 自动选择）[{args.uid or 'auto'}]：").strip()
                if uid:
                    args.uid = None if uid.lower() == "auto" else uid
                freq = input(f"SWD 频率 Hz（回车保留）[{args.frequency}]：").strip()
                if freq:
                    n = int(freq)
                    if n <= 0:
                        raise ValueError("频率必须大于 0")
                    args.frequency = n
                verbosity = input(f"详细输出（y/n，回车保留）[{'y' if args.verbose else 'n'}]：").strip().lower()
                if verbosity in ("y", "yes"):
                    args.verbose = True
                elif verbosity in ("n", "no"):
                    args.verbose = False
            except (ValueError, EOFError) as exc:
                print(f"[提示] 输入无效：{exc}")
            continue
        try:
            if choice == "1":
                rc = verify(args)
            elif choice == "2":
                # 直接进入显式的双重确认流程，不依赖用户输入命令参数。
                old = args.erase_all
                args.erase_all = True
                try:
                    rc = unlock(args)
                finally:
                    args.erase_all = old
            elif choice == "3":
                print("\n请先断开目标模组供电（注意 SWD 线可能反向供电），再重新上电。")
                answer = input("已完成实际断电及重新上电？输入 YES 进行只读验证：").strip()
                if answer != "YES":
                    print("[取消] 未声明完成断电；不会将软件重新连接视为断电验证。")
                    continue
                rc = verify(args)
                if rc == 0:
                    print("[提示] 本次验证通过；断电操作由操作者确认，脚本无法独立验证电源是否真的断开。")
            elif choice == "4":
                rc = list_probes(args)
            else:
                print("无效选项，请输入 0-5。")
                continue
        except (OSError, RuntimeError, KeyboardInterrupt) as exc:
            print(f"[错误] {exc}")
            rc = 2
        print(f"\n[操作结束] 状态码：{rc}（0=成功，1=仍锁定/取消，2=异常）")
        try:
            input("按回车返回主菜单……")
        except (EOFError, KeyboardInterrupt):
            return rc

def main():
    parser = argparse.ArgumentParser(description="nRF52840 APPROTECT 排障工具（默认只读）")
    parser.add_argument("action", nargs="?", choices=["verify", "unlock", "menu"], default="menu",
                        help="menu=交互菜单(默认)，verify=只读检查，unlock=擦除解锁")
    parser.add_argument("--erase-all", action="store_true", help="仅 unlock 可用；同意整片擦除")
    parser.add_argument("--uid", help="有多个 DAPLink 时指定 pyocd list 显示的 Probe UID")
    parser.add_argument("--frequency", type=int, default=100000, help="SWD 频率 Hz，默认 100000")
    parser.add_argument("--timeout", type=int, default=90, help="单次 PyOCD 调用超时秒数")
    parser.add_argument("--erase-timeout", type=int, default=90, help="触发 ERASEALL 后等待解除保护的总秒数，默认 90")
    parser.add_argument("--pyocd", default="pyocd", help="pyocd 可执行文件名称/路径")
    parser.add_argument("--verbose", action="store_true", help="打印 PyOCD 原始输出以辅助诊断")
    args = parser.parse_args()
    if args.frequency <= 0 or args.timeout <= 0 or args.erase_timeout <= 0:
        parser.error("--frequency、--timeout 与 --erase-timeout 必须大于 0")
    if args.action != "unlock" and args.erase_all:
        parser.error("--erase-all 只能用于 unlock")
    try:
        if args.action == "menu":
            return interactive_menu(args)
        return verify(args) if args.action == "verify" else unlock(args)
    except (OSError, RuntimeError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
