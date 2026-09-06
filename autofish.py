# -*- coding: utf-8 -*-
"""
三角洲行动 · 自动钓鱼

流程：拋竿(左键) → 等咬钩音效 → 刺鱼(左键) → 等收竿动画结束 → 重复
感知：
  - 咬钩：系统声音环回(WASAPI loopback)，与录好的咬钩音效做归一化互相关
  - 状态：截屏右下提示区，OCR 读提示文字：「抛竿」空闲 / 「退出钓鱼·刺鱼」线在水里 /
          「收线·摆杆」路亚拉鱼阶段(脚本不处理) / 其他(菜单、背包等)
          对提示位置、行数、背景亮度都不敏感，换钓点不用改
安全：
  - 只有游戏窗口在前台时才发送点击
  - F8 暂停/继续，F9 退出，Ctrl+C 退出
  - 提示区长时间不是钓鱼界面 → 自动停
"""
import ctypes
import ctypes.wintypes as wt
import multiprocessing
import os
import random
import sys
import threading
import time
from collections import deque

import numpy as np
import cv2
import mss
import soundcard as sc
from PIL import Image
from scipy import signal
from rapidocr_onnxruntime import RapidOCR

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

if os.name == "nt":
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass


def _bundle_dir():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


HERE = _bundle_dir()
ASSETS = os.path.join(HERE, "assets")

# ---------------- 可调参数 ----------------
GAME_EXE = "DeltaForceClient-Win64-Shipping.exe"
HUD_REGION = {"left": 2300, "top": 780, "width": 260, "height": 320}   # 2560x1440 右下提示区(含各种行数排版)
HUD_UPSCALE = 2               # OCR 前放大倍数，小字识别更稳
MSG_REGION = {"left": 830, "top": 1110, "width": 900, "height": 110}    # 屏幕中央提示文字（如「当前状态下无法拋竿」）
CAST_RETRY = 6                # 提示无法拋竿时最多补点几次
CAST_RETRY_GAP = 0.5          # 补点间隔

SR = 48000                    # 环回采样率
DS = 6                        # 降采样倍数 -> 8 kHz
BLOCK = 0.05                  # 每块音频秒数
BITE_NCC = 0.6                # 咬钩互相关阈值（实测真咬钩 ≥0.88，其他 <0.3）
SHOW_NCC = 0.5                # 上鱼(展示鱼)提示音阈值（实测 0.76~0.97，其他 <0.4）
SHOW_TIMEOUT = 6.0            # 刺鱼后最多等多久上鱼音，超时按固定延迟处理
CAST_AFTER_CANCEL = 1.0       # 取消动画后隔多久拋竿（太快游戏会提示无法拋竿）
BAIT_PER_PACK = 5             # 每包鱼饵几个，用完游戏要上饵
REBAIT_DELAY = 1.5            # 上饵耗时，每钓满一包多等这么久再拋
STEP_DELAY_MIN = 0.2          # 每次按键前额外等待的秒数下限
STEP_DELAY_MAX = 0.5          # 每次按键前额外等待的秒数上限（每次随机，避免固定节奏）
BITE_REACT_DELAY = 0.50       # 检测到咬钩后再等多少秒点刺鱼（宁晚不早；音效本身需 0.3s 才能确认，合计约 0.8s）
HOLD_RMB_WHILE_WAITING = True # 等咬钩时按住右键（缩放），屏蔽别人的咬钩/上鱼声音
CAST_FAIL_BACKOFF = 2.0       # 连续拋竿失败（比如还在走路没到水边）后，每次多等这么久再试

CAST_TIMEOUT = 8.0            # 拋竿后多久没进入「刺鱼」状态就重发（拋竿动画期间提示会消失 3~4s）
BITE_TIMEOUT = 75.0           # 等咬钩最长秒数，超时先收竿再重拋
REEL_TIMEOUT = 20.0           # 刺鱼后多久没回到「拋竿」状态就放弃等待
IDLE_STABLE = 0.8             # 「拋竿」状态需稳定多久才拋（避开收竿动画尾巴）
POST_REEL_DELAY = 4.5         # （不取消动画时）刺鱼后收竿/展示鱼的动画时长，期间游戏拒收拋竿
CANCEL_ANIM = True            # 刺鱼后再点一下左键取消展示鱼动画，省 3~4 秒
ANIM_CANCEL_DELAY = 1.0       # 刺鱼后隔多久点那一下取消
POST_CANCEL_DELAY = 3.0       # 取消动画后再等多久才拋竿（太快会卡住）
POST_IDLE_STABLE = 2.0        # 收竿后「拋竿」提示需稳定多久才认为游戏已就绪
STATE_FLICKER = 1.5           # 待咬钩时提示区偏离「刺鱼」需持续多久才算状态真变了
HUD_POLL = 0.5                # 待咬钩期间多久看一次提示区（OCR 约 0.2s 一次，别太频）
DEBUG_STATES = True           # 打印提示区状态跳变
NOT_FISHING_STOP = 90.0       # 提示区连续多久不是钓鱼界面则自动退出
MAX_CATCH = 0                 # >0 时钓到这么多条后停止；0 不限
# -----------------------------------------

VK_F8, VK_F9 = 0x77, 0x78
u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------- 输入 ----------------
PUL = ctypes.POINTER(ctypes.c_ulong)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", PUL)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("mi", MOUSEINPUT)]


def _mouse(flag):
    inp = INPUT(0, MOUSEINPUT(0, 0, 0, flag, 0, None))
    u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def left_click(hold=0.06):
    time.sleep(random.uniform(STEP_DELAY_MIN, STEP_DELAY_MAX))
    _mouse(0x0002)  # LEFTDOWN
    time.sleep(hold)
    _mouse(0x0004)  # LEFTUP
    time.sleep(hold)


_rmb_down = False


def right_down():
    """等咬钩时按住右键（缩放视角），游戏会屏蔽别人的咬钩/上鱼声音。"""
    global _rmb_down
    if not _rmb_down:
        time.sleep(random.uniform(STEP_DELAY_MIN, STEP_DELAY_MAX))
        _mouse(0x0008)  # RIGHTDOWN
        _rmb_down = True


def right_up():
    global _rmb_down
    if _rmb_down:
        _mouse(0x0010)  # RIGHTUP
        _rmb_down = False
        time.sleep(0.05)


def key_pressed(vk):
    return bool(u32.GetAsyncKeyState(vk) & 0x8000)


def foreground_exe():
    hwnd = u32.GetForegroundWindow()
    pid = wt.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    n = wt.DWORD(512)
    ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
    k32.CloseHandle(h)
    return os.path.basename(buf.value) if ok else ""


def game_in_front():
    return foreground_exe().lower() == GAME_EXE.lower()


# ---------------- 屏幕状态 ----------------
class HudReader:
    def __init__(self):
        self.sct = mss.MSS()
        try:  # 限制 OCR 线程数，别把 CPU 吃满
            self.ocr = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
        except TypeError:
            self.ocr = RapidOCR()
        self._last = None
        self.last_texts = []

    @staticmethod
    def classify(texts):
        """OCR 在亮背景上会认错个别字（抛等/更换鱼馆），所以每个状态给多个线索。"""
        j = "|".join(texts)
        if "收线" in j or "摆杆" in j or "收綫" in j:
            return "R"
        if any(k in j for k in ("刺", "退出", "钓鱼", "缩放")):
            return "W"
        if any(k in j for k in ("竿", "鱼饵", "更换", "抛")):
            return "I"
        return "-"

    def center_text(self):
        img = self.sct.grab(MSG_REGION)
        bgr = np.asarray(Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX"))[:, :, ::-1]
        res, _ = self.ocr(np.ascontiguousarray(bgr))
        return "|".join(r[1] for r in (res or []))

    def cast_refused(self, window=1.2):
        """拋竿后一小段时间内屏幕中央是否出现「无法拋竿」。"""
        t0 = time.time()
        while time.time() - t0 < window:
            if "无法" in self.center_text():
                return True
            time.sleep(0.1)
        return False

    def state(self):
        """'W' 线在水里(可刺鱼) / 'I' 空闲(可拋竿) / 'R' 路亚拉鱼中 / '-' 其他"""
        img = self.sct.grab(HUD_REGION)
        bgr = np.asarray(Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX"))[:, :, ::-1]
        if HUD_UPSCALE != 1:
            bgr = cv2.resize(bgr, None, fx=HUD_UPSCALE, fy=HUD_UPSCALE, interpolation=cv2.INTER_CUBIC)
        res, _ = self.ocr(np.ascontiguousarray(bgr))
        self.last_texts = [r[1] for r in (res or [])]
        s = self.classify(self.last_texts)
        if DEBUG_STATES and s != self._last:
            log(f"    [hud] {self._last} -> {s}  {self.last_texts[:3]}")
            self._last = s
        return s


# ---------------- 咬钩音检测（独立进程，避免 OCR 抢 CPU 造成音频丢帧） ----------------
def _audio_worker(conn, armed, stop_flag):
    import warnings
    warnings.filterwarnings("ignore")
    import numpy as np
    import soundcard as sc
    from scipy import signal
    sr = SR // DS
    def load(name):
        t = signal.decimate(np.load(os.path.join(ASSETS, name)), DS, zero_phase=True)[: int(0.30 * sr)]
        return (t - t.mean()) / t.std()
    # armed.value: 0 不听 / 1 听咬钩 / 2 听上鱼提示音
    tpls = {1: ("bite", load("bite_wave.npy"), BITE_NCC), 2: ("show", load("show_wave.npy"), SHOW_NCC)}
    n = len(tpls[1][1])
    win = np.ones(n)
    raw = deque(maxlen=int(1.2 * SR))
    tail_raw = int((0.30 + BLOCK + 0.02) * SR)
    spk = sc.default_speaker()
    mic = sc.get_microphone(id=spk.name, include_loopback=True)
    frames = int(SR * BLOCK)
    conn.send(("info", f"音频环回: {spk.name}"))
    peak = 0.0
    last_report = time.time()
    with mic.recorder(samplerate=SR, channels=2, blocksize=frames) as rec:
        while not stop_flag.value:
            d = rec.record(numframes=frames).mean(axis=1)
            raw.extend(d)
            if not armed.value or len(raw) < tail_raw:
                continue
            kind, tpl, thr = tpls.get(armed.value, tpls[1])
            a = np.asarray(raw)
            tail = signal.decimate(a[-tail_raw:], DS, zero_phase=True)
            c = signal.correlate(tail, tpl, mode="valid")
            s1 = signal.correlate(tail, win, mode="valid")
            s2 = signal.correlate(tail ** 2, win, mode="valid")
            e = np.sqrt(np.maximum(s2 - s1 ** 2 / n, 1e-9))
            ncc = c / (e * np.sqrt(n))
            m = float(ncc.max())
            peak = max(peak, m)
            if m >= thr:
                armed.value = 0
                conn.send((kind, m, time.time()))
                peak = 0.0
            elif time.time() - last_report > 1.0:
                conn.send(("ncc", peak))
                last_report = time.time()


class BiteListener:
    """主进程侧的句柄：arm/disarm/event/last_ncc 与旧接口一致。"""

    def __init__(self):
        self._armed = multiprocessing.Value("i", 0)
        self._stop = multiprocessing.Value("i", 0)
        self._conn, child = multiprocessing.Pipe()
        self._proc = multiprocessing.Process(target=_audio_worker, args=(child, self._armed, self._stop), daemon=True)
        self.event = threading.Event()
        self.last_ncc = 0.0
        self.last_t = 0.0
        self._reader = threading.Thread(target=self._pump, daemon=True)

    def start(self):
        self._proc.start()
        self._reader.start()

    @property
    def armed(self):
        return bool(self._armed.value)

    @property
    def stop(self):
        return bool(self._stop.value)

    @stop.setter
    def stop(self, v):
        self._stop.value = 1 if v else 0

    def arm(self, kind="bite"):
        self.event.clear()
        self.kind = kind
        self._armed.value = 2 if kind == "show" else 1

    def disarm(self):
        self._armed.value = 0
        self.event.clear()

    def _pump(self):
        while not self._stop.value:
            try:
                if not self._conn.poll(0.1):
                    continue
                msg = self._conn.recv()
            except (EOFError, BrokenPipeError, OSError):
                if not self._stop.value:
                    log("音频进程已退出，咬钩检测停止！请重启脚本。")
                return
            if msg[0] in ("bite", "show"):
                self.last_ncc = msg[1]
                self.last_t = msg[2]
                self.event.set()
            elif msg[0] == "ncc":
                self.last_ncc = max(self.last_ncc, msg[1])
            elif msg[0] == "info":
                log(msg[1])


# ---------------- 主循环 ----------------
def wait_state(hud, target, timeout, stable=0.0, poll=0.1):
    """等待 HUD 进入 target 集合中的状态并保持 stable 秒。返回状态或 None(超时)。"""
    t0 = time.time()
    since = None
    while time.time() - t0 < timeout:
        if check_hotkeys():
            return None
        s = hud.state()
        if s in target:
            since = since or time.time()
            if time.time() - since >= stable:
                return s
        else:
            since = None
        time.sleep(poll)
    return None


_paused = False
_quit = False
_last_key = 0.0


def check_hotkeys():
    """F8 暂停/继续，F9 退出。返回 True 表示应该中断当前等待。"""
    global _paused, _quit, _last_key
    now = time.time()
    if now - _last_key > 0.4:
        if key_pressed(VK_F9):
            _quit = True
            _last_key = now
            return True
        if key_pressed(VK_F8):
            _paused = not _paused
            _last_key = now
            log("已暂停 (F8 继续)" if _paused else "继续")
            return True
    return _quit


MODES = {"1": "台钓", "2": "路亚"}


def print_banner():
    print()
    print("=" * 46)
    print("  三角洲行动 · 自动钓鱼")
    print("=" * 46)
    print("  1. 先打开游戏，走到水边，拿出鱼竿")
    print("  2. 下面直接回车（默认台钓）")
    print("  3. 用鼠标点一下游戏窗口，把它切到最前面")
    print("  4. 脚本开始自动抛竿 / 听咬钩 / 刺鱼")
    print()
    print("  F8  暂停 / 继续")
    print("  F9  退出")
    print("  游戏不在前台时，不会点鼠标")
    print("  提示区按 2560×1440 全屏标定，其它分辨率可能对不齐")
    print("  这是宏，封号风险自负")
    print("=" * 46)
    print()


def choose_mode():
    """命令行参数 --mode 台钓/路亚，或启动时手动选。"""
    for i, a in enumerate(sys.argv):
        if a == "--mode" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    print("选择模式：")
    print("  1  台钓（拋竿 → 等咬钩音 → 刺鱼）  ← 直接回车就用这个")
    print("  2  路亚（尚未实现）")
    while True:
        try:
            c = input("输入 1 或 2 后回车 [默认 1]: ").strip() or "1"
        except EOFError:
            return MODES["1"]
        if c in MODES:
            return MODES[c]


def wait_exit():
    if not getattr(sys, "frozen", False):
        return
    try:
        input("按回车关闭窗口...")
    except Exception:
        time.sleep(8)


def single_instance():
    """同时跑两个实例会互相点（一个拋竿另一个立刻刺鱼），用命名互斥量挡住。"""
    k32.CreateMutexW(None, True, "Global\\DeltaForceAutoFish")
    if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        log("已有一个自动钓鱼实例在运行，本实例退出。先关掉那个再开。")
        return False
    return True


def main():
    global _paused
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW("三角洲行动 · 自动钓鱼")
        except Exception:
            pass
    missing = [n for n in ("bite_wave.npy", "show_wave.npy") if not os.path.isfile(os.path.join(ASSETS, n))]
    if missing:
        log(f"缺少音效文件: {', '.join(missing)}（assets 目录）")
        return
    if not single_instance():
        return
    print_banner()
    mode = choose_mode()
    if mode != "台钓":
        log(f"「{mode}」模式尚未实现，先用台钓。")
        return
    log(f"模式：{mode}")
    hud = HudReader()
    ear = BiteListener()
    ear.start()
    log("自动钓鱼启动。F8 暂停/继续，F9 或 Ctrl+C 退出。切到游戏窗口即开始。")
    catches = casts = misses = cast_fail = 0
    not_fishing_since = None

    while not _quit:
        if check_hotkeys() and _quit:
            break
        if _paused or not game_in_front():
            right_up()
            time.sleep(0.3)
            continue

        s = hud.state()
        if s == "-":
            not_fishing_since = not_fishing_since or time.time()
            if time.time() - not_fishing_since > NOT_FISHING_STOP:
                log("长时间不在钓鱼界面，自动退出")
                break
            time.sleep(0.3)
            continue
        not_fishing_since = None
        if s == "R":
            # 路亚拉鱼阶段（长按左键拖鼠标），脚本不处理，等玩家拉完回到空闲
            log("检测到「收线/摆杆」(路亚拉鱼)，脚本不处理，请手动拉鱼")
            ear.disarm()
            wait_state(hud, "I-", REEL_TIMEOUT * 3, stable=0.5, poll=0.5)
            continue

        # 1) 空闲 → 拋竿
        if s == "I":
            if wait_state(hud, "I", 3.0, stable=IDLE_STABLE) != "I":
                continue
            if not game_in_front():
                continue
            ear.disarm()
            left_click()
            casts += 1
            log(f"拋竿 #{casts}")
            if wait_state(hud, "W", CAST_TIMEOUT) != "W":
                cast_fail += 1
                back = min(CAST_FAIL_BACKOFF * cast_fail, 20.0)
                log(f"拋竿后未进入待咬钩状态（不在水边？），{back:.0f}s 后重试")
                time.sleep(back)
                continue
            cast_fail = 0
            ear.arm()

        # 2) 线在水里 → 等咬钩（按住右键缩放，屏蔽别人的声音）
        if not ear.armed and not ear.event.is_set():
            ear.arm()
        if HOLD_RMB_WHILE_WAITING:
            right_down()
        t0 = time.time()
        got = False
        timed_out = False
        left_since = None
        next_poll = 0.0
        while True:
            if time.time() - t0 >= BITE_TIMEOUT:
                timed_out = True
                break
            if check_hotkeys() or _paused or not game_in_front():
                break
            if ear.event.wait(0.05):
                got = True
                break
            if time.time() < next_poll:
                continue
            next_poll = time.time() + HUD_POLL
            st = hud.state()
            if st == "W":
                left_since = None
                continue
            # 状态变了：I/R 是明确离开了待咬钩（玩家手动收竿等）；'-' 可能只是缩放时提示被遮，多给点时间
            left_since = left_since or time.time()
            limit = STATE_FLICKER if st in ("I", "R") else STATE_FLICKER * 4
            if time.time() - left_since >= limit:
                break
        if not (got or timed_out):
            right_up()
            ear.disarm()
            continue

        # 3) 刺鱼（先松右键再点左键）
        if got:
            time.sleep(BITE_REACT_DELAY)
            right_up()
            left_click()
            log(f"咬钩 ncc={ear.last_ncc:.2f} 等待{time.time()-t0:.1f}s → 刺鱼")
        else:
            right_up()
            left_click()
            misses += 1
            log(f"{BITE_TIMEOUT:.0f}s 无咬钩，收竿重拋 (第{misses}次)")
        ear.disarm()
        ear.last_ncc = 0.0
        if got:
            catches += 1
            log(f"✓ 第 {catches} 条")
            if MAX_CATCH and catches >= MAX_CATCH:
                log("达到设定条数，停止")
                break

        # 4) 听到上鱼(展示鱼)提示音 → 立刻左键取消动画 → 紧接着左键拋竿
        if CANCEL_ANIM:
            ear.arm("show")
            heard = ear.event.wait(SHOW_TIMEOUT)
            ear.disarm()
            if not heard:
                log(f"{SHOW_TIMEOUT:.0f}s 没听到上鱼音，按固定延迟处理")
                time.sleep(ANIM_CANCEL_DELAY)
            if game_in_front() and not _paused and not _quit:
                left_click()                      # 取消动画
                time.sleep(CAST_AFTER_CANCEL)
                if got and BAIT_PER_PACK and catches % BAIT_PER_PACK == 0:
                    log(f"钓满 {BAIT_PER_PACK} 条，等上饵 {REBAIT_DELAY:.1f}s")
                    time.sleep(REBAIT_DELAY)
                left_click()                      # 直接拋竿
                casts += 1
                log(f"上鱼音{'✓' if heard else '✗'} → 取消动画 → 拋竿 #{casts}")
                for k in range(CAST_RETRY):       # 屏幕中央提示「当前状态下无法拋竿」就补点
                    if not hud.cast_refused():
                        break
                    time.sleep(CAST_RETRY_GAP)
                    left_click()
                    log(f"  提示无法拋竿，补点第 {k + 1} 次")
                # 进了待咬钩就回到循环顶部直接等咬钩；没进则顶部按空闲流程重拋
                if wait_state(hud, "W", CAST_TIMEOUT) != "W":
                    log("快速拋竿未进入待咬钩状态，走常规流程")
        else:
            time.sleep(POST_REEL_DELAY)
            if wait_state(hud, "IW", REEL_TIMEOUT, stable=POST_IDLE_STABLE) is None:
                log("收竿后未回到空闲/待咬钩状态，继续观察")

    right_up()
    ear.stop = True
    log(f"结束：拋竿 {casts} 次，咬钩 {catches} 次，空竿 {misses} 次")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        main()
    except KeyboardInterrupt:
        log("Ctrl+C 退出")
    except Exception:
        import traceback
        traceback.print_exc()
        log("出错了，把上面的文字截图保存下来")
    finally:
        right_up()
        wait_exit()
