"""本地真实验证码解题探针：真实弹出腾讯点选验证码 -> 用当前 ICR+点击逻辑作答 -> 读结果。

目的：在不动 GitHub Actions、不登录、不消耗签到次数的前提下，验证「当前算法」
（ICR 模板匹配 + 按渲染矩形映射坐标 + ActionChains 点击 + 提交）对真实验证码是否有效。

用法：python probe_solve.py [headless|headful] [轮数]
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

HERE = Path(__file__).resolve().parent
OUT = HERE / "out_solve"
OUT.mkdir(exist_ok=True)
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
import ICR  # noqa: E402

MODE = sys.argv[1] if len(sys.argv) > 1 else "headless"
TRIES = int(sys.argv[2]) if len(sys.argv) > 2 else 5
# linux runner 用 Chrome，Windows 本地用 Edge
BROWSER = sys.argv[3] if len(sys.argv) > 3 else ("chrome" if os.name != "nt" else "edge")

STEALTH = r"""
(function(){
  try{Object.defineProperty(navigator,'webdriver',{get:()=>undefined});}catch(e){}
  try{if(!window.chrome){window.chrome={runtime:{},loadTimes:function(){},csi:function(){},app:{}};}}catch(e){}
})();
"""

if BROWSER == "chrome":
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    _WebDriver = webdriver.Chrome
    _kwargs = {"options": None}
else:
    from selenium.webdriver.edge.options import Options
    from selenium.webdriver.edge.service import Service
    _WebDriver = webdriver.Edge
    _kwargs = {"options": None}

opts = Options()
opts.add_argument("--no-sandbox")
opts.add_argument("--disable-dev-shm-usage")
opts.add_argument("--window-size=1920,1080")
opts.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
opts.add_experimental_option("excludeSwitches", ["enable-automation"])
opts.add_experimental_option("useAutomationExtension", False)
if MODE == "headless":
    opts.add_argument("--headless=new")
elif MODE == "oldheadless":
    opts.add_argument("--headless")


def url_from_style(style):
    m = re.search(r"url\(([^)]+)\)", style or "")
    return m.group(1).strip().strip('"').strip("'") if m else None


def human_track(dx, dy):
    steps = max(4, min(14, int(max(abs(dx), abs(dy)) ** 0.5)))
    weights = [(steps - i) ** 1.5 for i in range(steps)]
    total = sum(weights)
    track, mx, my, acc = [], 0, 0, 0.0
    for w in weights:
        acc += w
        tx, ty = round(dx * acc / total), round(dy * acc / total)
        track.append((tx - mx, ty - my))
        mx, my = tx, ty
    track[-1] = (track[-1][0] + dx - mx, track[-1][1] + dy - my)
    return track


def click_point(driver, slide_bg, ox, oy):
    w, h = driver.execute_script(
        "const r=arguments[0].getBoundingClientRect();return [r.width,r.height];", slide_bg)
    if w <= 0 or h <= 0:
        return False
    dx, dy = int(ox - w / 2), int(oy - h / 2)
    a = ActionChains(driver)
    a.move_to_element(slide_bg)
    for sx, sy in human_track(dx, dy):
        if sx or sy:
            a.move_by_offset(sx, sy)
        a.pause(0.02)
    a.click().perform()
    return True


def enter_captcha_frame(driver, timeout=20):
    driver.switch_to.default_content()

    def ready(d):
        for f in d.find_elements(By.XPATH, "//iframe[contains(@id,'tcaptcha') or contains(@src,'tcaptcha')]"):
            s = f.size or {}
            if s.get("width", 0) > 0 and s != {"height": 150, "width": 300}:
                return f
        return False

    frame = WebDriverWait(driver, timeout, poll_frequency=0.5).until(ready)
    driver.switch_to.frame(frame)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "slideBg")))
    return frame


def solve_once(driver, i):
    work = OUT / f"t{i}"
    work.mkdir(exist_ok=True)
    enter_captcha_frame(driver)

    # 等 background style / instruction src 就绪
    def bg_ready(d):
        els = d.find_elements(By.ID, "slideBg")
        return bool(els and url_from_style(els[0].get_attribute("style")))

    WebDriverWait(driver, 15).until(bg_ready)
    slide_bg = driver.find_element(By.ID, "slideBg")
    bg_url = url_from_style(slide_bg.get_attribute("style"))
    sprite = WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.XPATH, "//*[@id='instruction']/div/img")))
    sprite_url = sprite.get_attribute("src")
    (work / "captcha.jpg").write_bytes(requests.get(bg_url, timeout=20).content)
    (work / "sprite.jpg").write_bytes(requests.get(sprite_url, timeout=20).content)

    positions = ICR.find_part_positions(str(work / "captcha.jpg"), str(work / "sprite.jpg"), "template")
    print(f"[try {i}] 识别到 {len(positions)} 个位置: {[(int(x),int(y)) for x,y in positions]}", flush=True)

    import cv2
    bg = cv2.imread(str(work / "captcha.jpg"))
    raw_w, raw_h = bg.shape[1], bg.shape[0]
    r = driver.execute_script("const r=arguments[0].getBoundingClientRect();return [r.width,r.height];", slide_bg)
    bw, bh = float(r[0]), float(r[1])
    print(f"[try {i}] 背景 {raw_w}x{raw_h} -> 渲染 {bw:.1f}x{bh:.1f}", flush=True)

    if bg is not None and positions:
        for k, (x, y) in enumerate(positions):
            cv2.circle(bg, (int(x), int(y)), 24, (0, 0, 255), 3)
            cv2.putText(bg, str(k + 1), (int(x) - 8, int(y) + 10), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
        cv2.imwrite(str(work / "annotated.jpg"), bg)

    if not positions:
        return None
    for k, (x, y) in enumerate(positions):
        tx, ty = x / raw_w * bw, y / raw_h * bh
        hit = driver.execute_script(
            "const b=document.getElementById('slideBg').getBoundingClientRect();"
            "const e=document.elementFromPoint(b.left+arguments[0], b.top+arguments[1]);"
            "return e?{tag:e.tagName,id:e.id,cls:String(e.className).slice(0,80)}:null;", tx, ty)
        print(f"[try {i}] 图案{k+1} 图({int(x)},{int(y)}) -> 点({tx:.1f},{ty:.1f}) 命中:{hit}", flush=True)
        if not click_point(driver, slide_bg, tx, ty):
            return None
        time.sleep(0.6)

    # 点击后标记层
    try:
        html = driver.execute_script(
            "const el=document.getElementById('slideBg');const h=el&&el.parentElement;"
            "return h?h.outerHTML.slice(0,1500):'';")
        print(f"[try {i}] 标记层片段: {html[:400]}", flush=True)
    except Exception as e:
        print(f"[try {i}] 标记层读取失败 {e}", flush=True)

    shot = OUT / f"t{i}" / "frame.png"
    shot.write_bytes(driver.get_screenshot_as_png())

    try:
        confirm = WebDriverWait(driver, 10).until(EC.presence_of_element_located(
            (By.XPATH, '//*[@id="tcStatus"]/div[2]/div[2]/div/div')))
        confirm.click()
    except TimeoutException:
        print(f"[try {i}] 未找到提交按钮", flush=True)
        return None
    time.sleep(5)
    try:
        cls = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, '//*[@id="tcOperation"]'))).get_attribute("class")
    except TimeoutException:
        cls = "<no tcOperation>"
    print(f"[try {i}] tcOperation class = {cls!r}", flush=True)
    return "show-success" in (cls or "")


def show_captcha(driver):
    """重新加载登录页并弹出一个全新验证码（保证每轮是未使用过的新图）。"""
    driver.switch_to.default_content()
    driver.get("https://app.rainyun.com/auth/login")
    WebDriverWait(driver, 30).until(lambda d: d.execute_script("return typeof TencentCaptcha === 'function'"))
    driver.execute_script(
        "window.__res=null;"
        "var c=new TencentCaptcha('2039519451',function(r){window.__res=JSON.stringify(r);},{});c.show();")


def _reload_captcha_in_frame(driver):
    """在验证码 iframe 内点 #reload 换一张新图（仿真实代码，避免重载页面触发 beforeunload 卡死）。"""
    try:
        driver.find_element(By.XPATH, '//*[@id="reload"]').click()
        time.sleep(1.8)
        return True
    except Exception:
        return False


def main():
    print(f"mode={MODE} tries={TRIES} browser={BROWSER}", flush=True)
    driver = _WebDriver(options=opts)
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH})
        ok = 0
        done = 0
        shown = False
        for i in range(1, TRIES + 1):
            try:
                if not shown:
                    show_captcha(driver)
                    shown = True
                elif not _reload_captcha_in_frame(driver):
                    # 无法在原弹窗刷新（如上轮已成功关闭），退回重载页面
                    try:
                        driver.switch_to.alert.accept()
                    except Exception:
                        pass
                    show_captcha(driver)
                res = solve_once(driver, i)
                if res is None:
                    print(f"[try {i}] SKIP(识别/点击失败)", flush=True)
                    continue
                done += 1
                if res:
                    ok += 1
                    print(f"[try {i}] PASS", flush=True)
                    shown = False  # 成功后弹窗关闭，下一轮重载页面
                    try:
                        print("  cb __res =", driver.execute_script("return window.__res;"), flush=True)
                    except Exception:
                        pass
                else:
                    print(f"[try {i}] FAIL", flush=True)  # 保持 shown=True，下一轮用 #reload
            except Exception as e:
                print(f"[try {i}] 异常: {type(e).__name__}: {str(e)[:150]}", flush=True)
                shown = False
        print(f"===== 通过 {ok}/{done}（有效样本 {done}）=====", flush=True)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
