# -*- coding: utf-8 -*-
"""以管理员身份运行：等游戏在前台且提示为「拋竿」，发一次左键，看是否进入「刺鱼」。结果写到 elev_test.log"""
import ctypes, ctypes.wintypes as wt, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "elev_test.log")


def out(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%H:%M:%S ") + msg + "\n")


try:
    import autofish as af
    af.DEBUG_STATES = False
    adv = ctypes.windll.advapi32
    k32 = ctypes.windll.kernel32
    tok = wt.HANDLE()
    adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(tok))
    el = wt.DWORD(); n = wt.DWORD()
    adv.GetTokenInformation(tok, 20, ctypes.byref(el), 4, ctypes.byref(n))
    out(f"elevated={bool(el.value)}")
    hud = af.HudReader()
    t0 = time.time(); st = None; stable = 0
    while time.time() - t0 < 60:
        st = hud.state() if af.game_in_front() else "(not front)"
        stable = stable + 1 if st == "I" else 0
        if stable >= 4:
            break
        time.sleep(0.3)
    out(f"state before click: {st} {hud.last_texts[:3]}")
    if st != "I":
        out("abort: not idle / game not in front")
        sys.exit()
    af.left_click()
    out("clicked")
    r = af.wait_state(hud, "W", 8.0)
    out(f"result: {'WORKED - entered W' if r == 'W' else 'no reaction'} texts={hud.last_texts[:3]}")
    if r == "W":
        time.sleep(1.0)
        # 收回来，别让线一直泡在水里
        af.left_click()
        out("reeled back")
except Exception as e:
    out(f"error: {e!r}")
