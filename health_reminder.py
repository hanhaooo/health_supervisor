# -*- coding: utf-8 -*-
"""
健康生活小助手 —— 久坐 · 喝水提醒
========================================
双击 exe 后自动打开浏览器设置页，改完点保存即生效（无需重启）。
提醒用托盘气泡通知（右下角、不抢焦点、几秒消失），不影响工作。

功能：
  1. 定时提醒喝水 / 起身（托盘气泡，不抢焦点）
  2. 双击打开设置页（内置本地 HTTP 服务 + 浏览器）
  3. 托盘图标 + 右键菜单（设置/暂停/继续/喝水/起身/结束/今日清零/退出）
  4. 喝水/起身计数 + 每日统计
  5. 数据持久化到 %APPDATA%\\HealthyLifeAssistant（设置记住、第二天清零）
  6. 结束生成 HTML 报告并用浏览器打开

运行：
    python health_reminder.py              # 默认：打开设置页 + 后台提醒
    python health_reminder.py --no-browser # 不自动打开设置页（静默后台）
    python health_reminder.py --no-tray    # 不用托盘图标
"""

import ctypes
import os
import sys
import time
import json
import argparse
import threading
import webbrowser
import configparser
from datetime import datetime, date
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

# ---------- 默认配置（五个设置项） ----------
DEFAULT = {
    "water_min": 60,        # 喝水提醒间隔（分钟）
    "cup_ml": 250,          # 每次饮水量（ml）
    "daily_water_ml": 1900, # 每日喝水目标（ml）
    "sit_min": 60,          # 久坐提醒间隔（分钟）
    "daily_stand": 7,       # 每日起身次数目标
}

MB_OK = 0x0000
MB_ICONINFORMATION = 0x40
MB_ICONQUESTION = 0x20
MB_YESNO = 0x0004
MB_TOPMOST = 0x40000
MB_SYSTEMMODAL = 0x1000
IDYES = 6

APP_DIR = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "HealthyLifeAssistant")
DATA_FILE = os.path.join(APP_DIR, "data.json")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.ini")
SETTINGS_HTML = os.path.join(BASE_DIR, "settings.html")
PORT = 8765

WATER_MSGS = [
    "该喝水啦！起来接杯水吧 💧",
    "喝水时间到，补充一下水分～",
    "别光顾着忙，喝口水歇一歇 💧",
]
SIT_MSGS = [
    "该起身啦！站起来活动活动，看看远处 👀",
    "久坐伤身，起来走两步，放松下眼睛～",
    "休息一下：伸个懒腰，眺望远处 5 分钟 🧘",
]


# ==================== 数据持久化 ====================
class Store:
    """负责把设置和每日记录存到 %APPDATA%\\HealthyLifeAssistant\\data.json"""

    def __init__(self):
        self.today = date.today().isoformat()
        self.settings = dict(DEFAULT)
        self.water_ml = 0
        self.water_count = 0
        self.stand_count = 0
        self.records = []          # [{"type": "water"|"stand", "time": "HH:MM:SS", "ml": ...}]
        self._load()

    def _load(self):
        if not os.path.exists(DATA_FILE):
            return
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return
        if "settings" in d:
            self.settings.update(d["settings"])
        if d.get("date") == self.today:
            self.water_ml = d.get("water_ml", 0)
            self.water_count = d.get("water_count", 0)
            self.stand_count = d.get("stand_count", 0)
            self.records = d.get("records", [])
        else:
            self._save()  # 跨天，重置数据

    def _save(self):
        os.makedirs(APP_DIR, exist_ok=True)
        d = {
            "date": self.today,
            "settings": self.settings,
            "water_ml": self.water_ml,
            "water_count": self.water_count,
            "stand_count": self.stand_count,
            "records": self.records,
        }
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def apply_settings(self, new_settings):
        """更新设置并写回 data.json + config.ini"""
        for k in DEFAULT:
            v = new_settings.get(k)
            if v is not None:
                try:
                    self.settings[k] = max(1, int(v))
                except (ValueError, TypeError):
                    pass
        self._save()
        self._write_config_ini()

    def _write_config_ini(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                f.write("[interval]\n")
                f.write(f"water_min = {self.settings['water_min']}\n")
                f.write(f"cup_ml = {self.settings['cup_ml']}\n")
                f.write(f"daily_water_ml = {self.settings['daily_water_ml']}\n")
                f.write(f"sit_min = {self.settings['sit_min']}\n")
                f.write(f"daily_stand = {self.settings['daily_stand']}\n")
        except Exception:
            pass

    def drink(self, ml=None):
        ml = ml or int(self.settings["cup_ml"])
        self.water_ml += ml
        self.water_count += 1
        self.records.append({"type": "water", "time": datetime.now().strftime("%H:%M:%S"), "ml": ml})
        self._save()

    def stand(self):
        self.stand_count += 1
        self.records.append({"type": "stand", "time": datetime.now().strftime("%H:%M:%S")})
        self._save()

    def reset_today(self):
        self.water_ml = 0
        self.water_count = 0
        self.stand_count = 0
        self.records = []
        self._save()


# ==================== 提示音 ====================
def beep():
    try:
        ctypes.windll.user32.MessageBeep(0xFFFFFFFF)
    except Exception:
        pass


def ask_yes_no(title, text):
    """弹「是/否」确认框（这个可以置顶，因为是用户主动操作）"""
    r = ctypes.windll.user32.MessageBoxW(0, text, title, MB_YESNO | MB_ICONQUESTION | MB_TOPMOST)
    return r == IDYES


# ==================== 托盘图标 ====================
try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except Exception:
    TRAY_AVAILABLE = False


def make_icon_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((12, 20, 52, 60), fill=(41, 128, 235, 255))
    d.polygon([(32, 6), (14, 26), (50, 26)], fill=(41, 128, 235, 255))
    return img


# ==================== 本地设置服务 ====================
class SettingsServer:
    """内置 HTTP 服务：提供设置页 + 接收保存请求"""

    def __init__(self, app):
        self.app = app
        self.httpd = None

    def start(self):
        def _run():
            handler = self._make_handler()
            self.httpd = HTTPServer(("127.0.0.1", PORT), handler)
            self.httpd.serve_forever()

        threading.Thread(target=_run, daemon=True).start()

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass

    def _make_handler(self):
        app = self.app

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # 静默

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/" or parsed.path == "/settings":
                    self._serve_settings()
                elif parsed.path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                else:
                    self.send_error(404)

            def do_POST(self):
                parsed = urlparse(self.path)
                if parsed.path == "/save":
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode("utf-8", errors="ignore")
                    data = parse_qs(body)
                    new_settings = {k: v[0] for k, v in data.items()}
                    app.apply_settings_from_web(new_settings)
                    resp = json.dumps({"ok": True}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)
                else:
                    self.send_error(404)

            def _serve_settings(self):
                # 读取 settings.html，注入当前配置值
                try:
                    with open(SETTINGS_HTML, "r", encoding="utf-8") as f:
                        html = f.read()
                except Exception:
                    html = "<h1>设置页缺失</h1>"
                # 注入当前值到页面（通过 script 里的 init 数据）
                init_data = json.dumps(app.store.settings, ensure_ascii=False)
                inject = f"<script>window.__INIT__ = {init_data};</script>"
                html = html.replace("</head>", inject + "</head>")
                data = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler


# ==================== 报告生成 ====================
def generate_report(store, session):
    now = datetime.now()
    start = session["start"]
    end = session.get("end") or now
    paused = session.get("paused_seconds", 0)
    duration = max(1, (end - start).total_seconds() - paused)
    hours = duration / 3600

    avg_water_str = f"{duration / 60 / store.water_count:.0f} 分钟" if store.water_count > 0 else "—"
    avg_stand_str = f"{duration / 60 / store.stand_count:.0f} 分钟" if store.stand_count > 0 else "—"

    rows = ""
    for r in store.records:
        if r["type"] == "water":
            rows += f'<tr><td>💧 喝水</td><td>{r["time"]}</td><td>{r.get("ml", "")} ml</td></tr>'
        else:
            rows += f'<tr><td>👀 起身</td><td>{r["time"]}</td><td>—</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>健康生活报告</title>
<style>
body{{font-family:'Microsoft YaHei',sans-serif;background:#f5f7fa;margin:0;padding:40px;color:#333}}
.card{{max-width:720px;margin:0 auto;background:#fff;border-radius:16px;padding:40px;box-shadow:0 4px 20px rgba(0,0,0,.08)}}
h1{{font-size:24px;margin:0 0 8px}}.sub{{color:#999;font-size:13px;margin-bottom:30px}}
.stats{{display:flex;gap:20px;margin-bottom:30px}}
.stat{{flex:1;background:#f0f5ff;border-radius:12px;padding:20px;text-align:center}}
.stat .num{{font-size:34px;font-weight:bold;color:#2980e3}}.stat .label{{color:#777;font-size:13px;margin-top:6px}}
.stat.sit .num{{color:#e67e22}}
.info{{background:#fafafa;border-radius:12px;padding:20px;margin-bottom:30px;line-height:1.9}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #eee;font-size:14px}}th{{color:#999;font-weight:normal}}
.foot{{text-align:center;color:#bbb;font-size:12px;margin-top:30px}}
</style></head><body>
<div class="card"><h1>健康生活报告</h1>
<div class="sub">{start.strftime("%Y-%m-%d %H:%M")} 生成</div>
<div class="stats">
<div class="stat water"><div class="num">{store.water_ml}</div><div class="label">喝水量 (ml)</div></div>
<div class="stat sit"><div class="num">{store.stand_count}</div><div class="label">起身次数</div></div>
<div class="stat"><div class="num">{hours:.1f}</div><div class="label">本次时长 (小时)</div></div>
</div>
<div class="info">开始时间：{start.strftime("%H:%M:%S")}　·　结束时间：{end.strftime("%H:%M:%S")}<br>
暂停时长：{paused/60:.0f} 分钟　·　喝水次数：{store.water_count} 次<br>
平均多久喝一次水：{avg_water_str}　·　平均多久起身一次：{avg_stand_str}</div>
<table><tr><th>动作</th><th>时间</th><th>数量</th></tr>
{rows if rows else '<tr><td colspan="3" style="text-align:center;color:#ccc">暂无记录</td></tr>'}
</table><div class="foot">健康生活小助手 · 记得照顾好自己</div></div></body></html>"""

    os.makedirs(APP_DIR, exist_ok=True)
    path = os.path.join(APP_DIR, f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


# ==================== 主程序 ====================
class ReminderApp:
    def __init__(self, store, tray=None):
        self.store = store
        self.tray = tray
        self.paused = False
        self.running = True
        self.session = {
            "start": datetime.now(),
            "end": None,
            "paused_seconds": 0.0,
            "pause_start": None,
        }
        self.next_water = time.time() + int(store.settings["water_min"]) * 60
        self.next_sit = time.time() + int(store.settings["sit_min"]) * 60
        self.water_idx = 0
        self.sit_idx = 0

    # ---- 不打扰的通知（托盘气泡） ----
    def notify(self, title, text):
        if self.tray:
            try:
                self.tray.notify(text, title)
                return
            except Exception:
                pass
        # 兜底：轻量弹窗（不置顶、不模态，不抢焦点）
        try:
            ctypes.windll.user32.MessageBoxW(0, text, title, MB_OK | MB_ICONINFORMATION)
        except Exception:
            pass

    # ---- 设置 ----
    def apply_settings_from_web(self, new_settings):
        self.store.apply_settings(new_settings)
        # 热更新倒计时
        self.next_water = time.time() + int(self.store.settings["water_min"]) * 60
        self.next_sit = time.time() + int(self.store.settings["sit_min"]) * 60
        self.notify("✅ 设置已保存", f"喝水每 {self.store.settings['water_min']} 分钟 · 起身每 {self.store.settings['sit_min']} 分钟")

    # ---- 托盘/菜单动作 ----
    def do_drink(self):
        self.store.drink()
        self.next_water = time.time() + int(self.store.settings["water_min"]) * 60
        beep()
        self.notify("💧 已记录喝水", f"今日已饮水 {self.store.water_ml} ml（{self.store.water_count} 次）")

    def do_stand(self):
        self.store.stand()
        self.next_sit = time.time() + int(self.store.settings["sit_min"]) * 60
        beep()
        self.notify("👀 已记录起身", f"今日已起身活动 {self.store.stand_count} 次")

    def do_pause(self):
        if not self.paused:
            self.paused = True
            self.session["pause_start"] = time.time()
            self.notify("⏸ 已暂停", "提醒已暂停，继续后接着计时")

    def do_resume(self):
        if self.paused:
            self.paused = False
            if self.session.get("pause_start"):
                self.session["paused_seconds"] += time.time() - self.session["pause_start"]
                self.session["pause_start"] = None
            self.next_water = time.time() + int(self.store.settings["water_min"]) * 60
            self.next_sit = time.time() + int(self.store.settings["sit_min"]) * 60
            self.notify("▶ 已继续", "接着上次的进度继续计时")

    def do_end(self):
        self.session["end"] = datetime.now()
        if self.paused and self.session.get("pause_start"):
            self.session["paused_seconds"] += time.time() - self.session["pause_start"]
        path = generate_report(self.store, self.session)
        self.store.reset_today()
        webbrowser.open("file:///" + path.replace("\\", "/"))
        self.running = False

    def do_reset(self):
        if ask_yes_no("今日清零", "确定要把今日的喝水/起身统计清零吗？\n（不会影响提醒计时）"):
            self.store.reset_today()
            self.notify("已清零", "今日统计已清零")

    # ---- 主循环 ----
    def run(self):
        try:
            while self.running:
                now = time.time()
                if not self.paused:
                    if now >= self.next_water:
                        msg = WATER_MSGS[self.water_idx % len(WATER_MSGS)]
                        self.water_idx += 1
                        self.next_water = now + int(self.store.settings["water_min"]) * 60
                        beep()
                        self.notify("💧 喝水提醒", msg)

                    if now >= self.next_sit:
                        msg = SIT_MSGS[self.sit_idx % len(SIT_MSGS)]
                        self.sit_idx += 1
                        self.next_sit = now + int(self.store.settings["sit_min"]) * 60
                        beep()
                        self.notify("👀 起身提醒", msg)

                time.sleep(1)
        except KeyboardInterrupt:
            pass
        self.session["end"] = datetime.now()


def main():
    parser = argparse.ArgumentParser(description="健康生活小助手")
    parser.add_argument("--water", type=int, default=None, help="喝水提醒间隔（分钟）")
    parser.add_argument("--sit", type=int, default=None, help="久坐提醒间隔（分钟）")
    parser.add_argument("--no-tray", action="store_true", help="不使用托盘图标")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开设置页")
    args = parser.parse_args()

    store = Store()

    # config.ini 覆盖默认（如果 data.json 里已有设置，Store 已加载，config 里也有的话优先 data.json）
    if os.path.exists(CONFIG_FILE):
        cp = configparser.ConfigParser()
        try:
            cp.read(CONFIG_FILE, encoding="utf-8")
            for key in DEFAULT:
                if cp.has_option("interval", key):
                    store.settings[key] = cp.getint("interval", key)
        except Exception:
            pass

    if args.water:
        store.settings["water_min"] = max(1, args.water)
    if args.sit:
        store.settings["sit_min"] = max(1, args.sit)

    print("=" * 50)
    print("  健康生活小助手 已启动")
    print(f"  · 喝水提醒：每 {store.settings['water_min']} 分钟（每次 {store.settings['cup_ml']} ml）")
    print(f"  · 起身提醒：每 {store.settings['sit_min']} 分钟")
    print(f"  · 今日已喝：{store.water_ml} ml（{store.water_count} 次）· 已起身 {store.stand_count} 次")
    print("  右键托盘图标可打开设置/暂停/退出")
    print("=" * 50)

    # 先创建托盘（app 需要引用它做 notify）
    tray = None
    if TRAY_AVAILABLE and not args.no_tray:
        tray = pystray.Icon("healthy", make_icon_image(), "健康生活小助手")

    app = ReminderApp(store, tray)

    # 设置服务
    server = SettingsServer(app)
    server.start()

    # 托盘菜单
    if tray:
        def _quit():
            app.running = False
            server.stop()
            tray.stop()

        def _open_settings():
            webbrowser.open(f"http://127.0.0.1:{PORT}/")

        tray.menu = pystray.Menu(
            pystray.MenuItem("打开设置", _open_settings, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("暂停提醒", lambda: app.do_pause()),
            pystray.MenuItem("继续计时", lambda: app.do_resume()),
            pystray.MenuItem("喝了一杯", lambda: app.do_drink()),
            pystray.MenuItem("起身了", lambda: app.do_stand()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("今日清零", lambda: app.do_reset()),
            pystray.MenuItem("结束（出报告）", lambda: app.do_end()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", _quit),
        )

    # 双击默认行为：自动打开设置页
    if not args.no_browser:
        def _open_later():
            time.sleep(1.0)  # 等 HTTP 服务就绪
            webbrowser.open(f"http://127.0.0.1:{PORT}/")

        threading.Thread(target=_open_later, daemon=True).start()

    # 提醒主循环放到后台线程
    loop_thread = threading.Thread(target=app.run, daemon=True)
    loop_thread.start()

    try:
        if tray:
            tray.run()  # 托盘在主线程阻塞运行
        else:
            loop_thread.join()
    finally:
        app.running = False
        server.stop()


if __name__ == "__main__":
    main()
