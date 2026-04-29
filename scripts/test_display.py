"""Step 2 验收脚本:DisplayController 完整命令 + 协议错误处理 + 触摸交互。

阶段:
  [1] 命令探测   — 把 6 条合法命令逐条发出去,期望 ack ok=true
  [2] 错误处理   — 故意发畸形 / 非法命令,期望 ack ok=false + 标准 error 码
  [3] 触摸交互   — 屏上显示 prompt,等审批事件(可选,需要你在屏前点)

用法:
  uv run python scripts/test_display.py                      # 阶段 1+2
  uv run python scripts/test_display.py --touch              # 阶段 1+2+3
  uv run python scripts/test_display.py --port /dev/cu.XXX --timeout 3

输出:每个测试一行 [PASS/FAIL/PARTIAL/TIMEOUT],最后汇总表格 + 计数。
  PASS    实际结果跟期望完全一致
  PARTIAL ok=false 对了,但 error 码跟协议规范不一致
  FAIL    实际跟期望相反(期望 ok 实际 reject,或反之)
  TIMEOUT 设备没回 ack(通常表示 Agent B 还没实现这条命令)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lelamp.transport import DisplayController, find_display_port  # noqa: E402


def _wait_ack(disp: DisplayController, cmd_name: str, timeout: float):
    return disp.wait_for(lambda m: m.get("ack") == cmd_name, timeout=timeout)


def _record(results, label, expected, msg):
    """记录一条测试结果。expected = 'ok' | 'reject' | 'reject:<error_code>'"""
    if msg is None:
        verdict = "TIMEOUT"
        actual = "—"
    elif expected == "ok":
        if msg.get("ok"):
            verdict, actual = "PASS", "ok"
        else:
            verdict, actual = "FAIL", f"ok=false error={msg.get('error')}"
    else:  # expected starts with "reject"
        if not msg.get("ok"):
            actual = f"ok=false error={msg.get('error', '?')}"
            if ":" in expected:
                want = expected.split(":", 1)[1]
                verdict = "PASS" if msg.get("error") == want else "PARTIAL"
            else:
                verdict = "PASS"
        else:
            verdict, actual = "FAIL", "ok=true(设备未拒绝)"
    results.append({"label": label, "expected": expected, "actual": actual, "verdict": verdict})
    return verdict


def stage_1(disp, log, results, timeout):
    log.info("─── 阶段 1:命令探测(期望 ok=true)───")
    tests = [
        # (label,                 send fn,                                                                       ack_name)
        ("ping seq=99",           lambda: disp.ping(seq=99),                                                     "ping"),
        ("set_brightness 80",     lambda: disp.set_brightness(80),                                               "set_brightness"),
        ("set_state idle",        lambda: disp.set_state("idle"),                                                "set_state"),
        ("set_state busy",        lambda: disp.set_state("busy"),                                                "set_state"),
        ("set_progress 50",       lambda: disp.set_progress(50),                                                 "set_progress"),
        ("set_text subtitle",     lambda: disp.set_text("subtitle", "Refactoring auth module..."),               "set_text"),
        ("set_text meta",         lambda: disp.set_text("meta", "3.2k tokens · 12s"),                            "set_text"),
        ("set_state attention",   lambda: disp.set_state("attention"),                                           "set_state"),
        ("show_prompt",           lambda: disp.show_prompt(id="probe_1", tool="Bash", command="ls /tmp"),        "show_prompt"),
        ("set_state failed",      lambda: disp.set_state("failed"),                                              "set_state"),
        ("set_state celebrate",   lambda: disp.set_state("celebrate"),                                           "set_state"),
        ("set_state sleep",       lambda: disp.set_state("sleep"),                                               "set_state"),
    ]
    for label, send_fn, ack_name in tests:
        time.sleep(0.3)
        send_fn()
        msg = _wait_ack(disp, ack_name, timeout=timeout)
        verdict = _record(results, label, "ok", msg)
        log.info(f"  {verdict:<8}  {label}")


def stage_2(disp, log, results, timeout):
    log.info("─── 阶段 2:错误处理(期望 ok=false + error 码)───")
    tests = [
        # (label,                    payload,                                              ack_name,        expected)
        ("[err] unknown cmd",        {"cmd": "nonexistent_cmd"},                           "nonexistent_cmd", "reject:unknown_cmd"),
        ("[err] missing state",      {"cmd": "set_state"},                                 "set_state",       "reject:missing_field"),
        ("[err] unknown state",      {"cmd": "set_state", "state": "foobar"},              "set_state",       "reject:unknown_state"),
        ("[err] unknown text id",    {"cmd": "set_text", "id": "nope", "text": "x"},       "set_text",        "reject:unknown_text_id"),
        ("[err] invalid pct type",   {"cmd": "set_progress", "pct": "abc"},                "set_progress",    "reject:invalid_value"),
    ]
    for label, payload, ack_name, expected in tests:
        time.sleep(0.3)
        disp.send_raw(payload)
        msg = _wait_ack(disp, ack_name, timeout=timeout)
        verdict = _record(results, label, expected, msg)
        actual = results[-1]["actual"]
        log.info(f"  {verdict:<8}  {label}  →  {actual}")


def stage_3(disp, log, timeout):
    log.info("─── 阶段 3:触摸交互 ───")

    # 前置 1:attention 屏必须真的切过去,否则屏上不会有按钮
    disp.set_state("attention")
    ack = disp.wait_for(lambda m: m.get("ack") == "set_state", timeout=2.0)
    if not (ack and ack.get("ok")):
        log.warning("  跳过:set_state attention 未 ok=true(Agent B 还没实现 attention 屏)")
        log.warning("        屏上不会出现 ✓/✗ 按钮,在屏前等没意义。")
        return False

    # 前置 2:show_prompt 必须成功,否则屏上不会渲染按钮
    pid = "touch_test_42"
    disp.show_prompt(id=pid, tool="Bash", command="rm -rf node_modules", desc="(test_display 触摸验收)")
    ack = disp.wait_for(lambda m: m.get("ack") == "show_prompt", timeout=2.0)
    if not (ack and ack.get("ok")):
        log.warning("  跳过:show_prompt 未 ok=true(Agent B 还没实现 prompt 渲染)")
        log.warning("        屏上不会出现 ✓/✗ 按钮,在屏前等没意义。")
        return False

    log.info(f"  ✓ 前置就绪 — 请在 {timeout:.0f}s 内点屏 ✓ 或 ✗(Ctrl-C 取消)...")
    try:
        decision = disp.wait_for_approval(prompt_id=pid, timeout=timeout)
    except KeyboardInterrupt:
        log.info("  用户中止")
        return False
    if decision is None:
        log.warning("  TIMEOUT,未收到 evt:approval(Agent B 可能还没实现按钮 → 事件)")
        return False
    log.info(f"  ✓ 收到 evt:approval  decision={decision}")
    return True


def _print_table(results):
    print()
    print(f"{'测试':<30} {'期望':<26} {'实际':<36} {'判定':<10}")
    print("-" * 102)
    for r in results:
        print(f"{r['label']:<30} {r['expected']:<26} {r['actual']:<36} {r['verdict']:<10}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=None, help="USB CDC 端口,不填则按 VID 自动找")
    parser.add_argument("--touch", action="store_true", help="启用阶段 3 触摸验收")
    parser.add_argument("--timeout", type=float, default=2.0, help="单条命令 ack 超时秒数")
    parser.add_argument("--touch-timeout", type=float, default=30.0, help="阶段 3 等触摸的秒数")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    log = logging.getLogger("test_display")

    try:
        port = args.port or find_display_port()
    except RuntimeError as e:
        log.error("找不到显示设备: %s", e)
        return 1
    log.info("显示设备端口: %s", port)

    results: list[dict] = []
    disp = DisplayController(port=port)

    try:
        with disp:
            time.sleep(0.5)  # 给 reader 线程稳一下
            stage_1(disp, log, results, args.timeout)
            stage_2(disp, log, results, args.timeout)
            if args.touch:
                stage_3(disp, log, args.touch_timeout)
            # 收尾:回到 idle(若设备未实现也无所谓)
            disp.set_state("idle")
            time.sleep(0.3)
    except KeyboardInterrupt:
        log.info("用户中止 (SIGINT)")
        _print_table(results)
        return 130

    _print_table(results)

    n_total   = len(results)
    n_pass    = sum(1 for r in results if r["verdict"] == "PASS")
    n_partial = sum(1 for r in results if r["verdict"] == "PARTIAL")
    n_fail    = sum(1 for r in results if r["verdict"] == "FAIL")
    n_to      = sum(1 for r in results if r["verdict"] == "TIMEOUT")
    log.info(f"汇总:总 {n_total} | PASS {n_pass} | PARTIAL {n_partial} | FAIL {n_fail} | TIMEOUT {n_to}")
    log.info("  PARTIAL = ok=false 对了但 error 码跟协议不一致,可让 Agent B 修")
    log.info("  TIMEOUT = 设备没回 ack,通常说明 Agent B 还没实现这条命令")

    return 0 if (n_fail == 0 and n_to == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
