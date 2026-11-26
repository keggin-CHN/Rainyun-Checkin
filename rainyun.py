import io
import json
import logging
import os
import random
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass

import cv2
import ddddocr
import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from config import (
    APP_BASE_URL,
    APP_VERSION,
    CAPTCHA_RETRY_LIMIT,
    COOKIE_FILE,
    DOWNLOAD_MAX_RETRIES,
    DOWNLOAD_RETRY_DELAY,
    DOWNLOAD_TIMEOUT,
    POINTS_TO_CNY_RATE,
)

# 自定义异常：验证码处理过程中可重试的错误
class CaptchaRetryableError(Exception):
    """可重试的验证码处理错误（如下载失败、网络问题等）"""
    pass

try:
    from notify import send
    print("✅ 通知模块加载成功")
except Exception as e:
    print(f"⚠️ 通知模块加载失败：{e}")

    def send(title, content):
        pass

# 创建一个内存缓冲区，用于存储所有日志
log_capture_string = io.StringIO()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
# 配置 logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

#输出到字符串 (新增功能)
string_handler = logging.StreamHandler(log_capture_string)
string_handler.setFormatter(formatter)
logger.addHandler(string_handler)

@dataclass
class RuntimeContext:
    driver: WebDriver
    wait: WebDriverWait
    ocr: ddddocr.DdddOcr
    det: ddddocr.DdddOcr
    temp_dir: str


XPATH_CONFIG = {
    "LOGIN_BTN": "//button[@type='submit' and contains(., '登') and contains(., '录')]",
    "LOGIN_BTN_FALLBACK": '//*[@id="app"]/div[1]/div[1]/div/div[2]/fade/div/div/span/form/button',
    # “每日签到”卡片根节点
    "SIGN_IN_CARD": "//div[contains(@class, 'card') and .//*[contains(concat(' ', normalize-space(@class), ' '), ' card-header ') and .//span[contains(normalize-space(.), '每日签到')]]]",
    # “每日签到”卡片头部（只在该模块内读取状态文案，避免其他任务卡片的“已完成”导致误判）
    "SIGN_IN_HEADER": "//*[contains(concat(' ', normalize-space(@class), ' '), ' card-header ') and .//span[contains(normalize-space(.), '每日签到')]]",
    "SIGN_IN_HEADER_FALLBACK": "//span[contains(normalize-space(.), '每日签到')]/ancestor::div[1]",
    # 新版每日签到卡片头本身是打开签到弹窗的按钮。
    "SIGN_IN_BTN": "//button[contains(concat(' ', normalize-space(@class), ' '), ' daily-check-in-task ') and .//span[contains(normalize-space(.), '每日签到')]]",
    # 兼容旧版卡片中独立的领取/完成/签到操作节点。
    "SIGN_IN_BTN_LEGACY": "//*[contains(concat(' ', normalize-space(@class), ' '), ' card-header ') and .//span[contains(normalize-space(.), '每日签到')]]//*[contains(normalize-space(.), '领取奖励') or contains(normalize-space(.), '去完成') or contains(normalize-space(.), '去签到')][not(.//*[contains(normalize-space(.), '领取奖励') or contains(normalize-space(.), '去完成') or contains(normalize-space(.), '去签到')])]",
    "SIGN_IN_BTN_FALLBACK": "//span[contains(normalize-space(.), '每日签到')]/ancestor::div[1]//*[contains(normalize-space(.), '领取奖励') or contains(normalize-space(.), '去完成') or contains(normalize-space(.), '去签到')][not(.//*[contains(normalize-space(.), '领取奖励') or contains(normalize-space(.), '去完成') or contains(normalize-space(.), '去签到')])]",
    # 新版“每日签到”弹窗面板（BModal 渲染到 body 的签到组件根节点，class 精确为 daily-check-in）
    "DAILY_SIGN_MODAL": "//div[contains(concat(' ', normalize-space(@class), ' '), ' daily-check-in ')]",
    # 页面新版优先通过积分语义定位，旧 XPath 仅作为最后兼容项。
    "POINTS_BY_LABEL": "//*[contains(normalize-space(.), '剩余积分') or contains(normalize-space(.), '当前积分') or contains(normalize-space(.), '积分余额')]/ancestor::*[self::div or self::section][1]//h3",
    "POINTS_HEADER": '//*[@id="app"]/div[1]/div[3]/div[2]/div/div/div[2]/div[1]/div[1]/div/p/div/h3',
}

DAILY_SIGN_CLAIM_TEXTS = ("领取奖励", "去完成", "去签到")
CAPTCHA_IFRAME_XPATH = "//iframe[contains(@id, 'tcaptcha_iframe') or contains(@src, 'tcaptcha')]"
# 新版“每日签到”弹窗内可点击的奖励按钮（优先 500 积分），由前端 DailyCheckIn 组件渲染。
DAILY_SIGN_REWARD_BTN_XPATH = "//div[contains(concat(' ', normalize-space(@class), ' '), ' daily-check-in ')]//button[contains(concat(' ', normalize-space(@class), ' '), ' daily-check-in__reward ')]"
SIGN_DIALOG_XPATH = "//*[@role='dialog' or contains(@class, 'el-dialog') or contains(@class, 'ant-modal') or contains(@class, 'modal') or contains(@class, 'popup') or contains(@class, 'dialog')]"
SIGN_DIALOG_ACTION_XPATH = ".//*[self::a or self::button or self::span][normalize-space(.)='去签到' or normalize-space(.)='立即签到' or normalize-space(.)='开始签到' or normalize-space(.)='签到'][not(.//*[self::a or self::button or self::span][normalize-space(.)='去签到' or normalize-space(.)='立即签到' or normalize-space(.)='开始签到' or normalize-space(.)='签到'])]"
# 只在“每日签到”模块范围内匹配这些完成态文案，绝不扫描整个 page_source
DAILY_SIGN_DONE_PATTERNS = ("已完成", "已领取", "已签到", "明日再来")


def build_app_url(path: str) -> str:
    return f"{APP_BASE_URL}/{path.lstrip('/')}"


def temp_path(ctx: RuntimeContext, filename: str) -> str:
    return os.path.join(ctx.temp_dir, filename)


def clear_temp_dir(temp_dir: str) -> None:
    if not os.path.exists(temp_dir):
        return
    for filename in os.listdir(temp_dir):
        file_path = os.path.join(temp_dir, filename)
        if os.path.isfile(file_path) or os.path.islink(file_path):
            os.remove(file_path)


def dismiss_modal_confirm(driver: WebDriver, wait_seconds: float = 2.0) -> bool:
    """关闭页面可能弹出的确认弹窗（若有）"""
    try:
        modal_wait = WebDriverWait(driver, wait_seconds, poll_frequency=0.3)
        confirm = modal_wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//footer[contains(@id,'modal') and contains(@id,'footer')]//button[contains(normalize-space(.), '确认')]")
            )
        )
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", confirm)
        except Exception:
            pass
        time.sleep(0.2)
        confirm.click()
        logger.info("已关闭页面确认弹窗")
        time.sleep(0.5)
        return True
    except Exception:
        return False


def do_login(ctx: RuntimeContext, user: str, pwd: str) -> bool:
    """执行登录流程（支持失败自动重试）"""
    logger.info("发起登录请求")
    ctx.driver.get(build_app_url("/auth/login"))
    for attempt in range(1, 3):
        try:
            username = ctx.wait.until(EC.visibility_of_element_located((By.NAME, 'login-field')))
            password = ctx.wait.until(EC.visibility_of_element_located((By.NAME, 'login-password')))
            try:
                login_button = ctx.wait.until(EC.visibility_of_element_located((By.XPATH, XPATH_CONFIG["LOGIN_BTN"])))
            except TimeoutException:
                login_button = ctx.wait.until(EC.visibility_of_element_located((By.XPATH, XPATH_CONFIG["LOGIN_BTN_FALLBACK"])))
            username.clear()
            password.clear()
            username.send_keys(user)
            password.send_keys(pwd)
            login_button.click()
        except TimeoutException:
            logger.error("页面加载超时，请尝试延长超时时间或切换到国内网络环境！")
            return False

        try:
            switch_to_visible_captcha_frame(ctx.driver, 8 if attempt > 1 else 15)
            logger.warning("触发验证码！")
            if not process_captcha(ctx):
                logger.error("登录验证码识别失败")
                return False
        except TimeoutException:
            logger.info("未触发验证码")
        finally:
            ctx.driver.switch_to.default_content()

        time.sleep(2)  # 给页面一点点缓冲时间
        try:
            redirect_wait = WebDriverWait(ctx.driver, 20, poll_frequency=0.5)
            redirect_wait.until(EC.url_contains("dashboard"))
            logger.info("登录成功！")
            dismiss_modal_confirm(ctx.driver, 2.0)
            return True
        except TimeoutException:
            current_url = ctx.driver.current_url
            if attempt < 2 and "/auth/login" in current_url:
                logger.warning(f"第 {attempt} 次登录后仍停留在登录页，自动重试一次。当前 URL: {current_url}")
                continue
            logger.error(f"登录超时或失败！当前 URL: {current_url}")
            return False
    return False


def init_selenium(debug: bool, linux: bool) -> WebDriver:
    ops = Options()
    ops.add_argument("--no-sandbox")
    ops.add_argument("--window-size=1920,1080")
    if debug:
        ops.add_experimental_option("detach", True)
    if linux:
        ops.add_argument("--headless")
        ops.add_argument("--disable-gpu")
        ops.add_argument("--disable-dev-shm-usage")
        # 设置 Chromium 二进制路径（支持 ARM 和 AMD64）
        chrome_bin = os.environ.get("CHROME_BIN")
        if chrome_bin and os.path.exists(chrome_bin):
            ops.binary_location = chrome_bin
        # 容器环境使用系统 chromedriver
        chromedriver_path = os.environ.get("CHROMEDRIVER_PATH", "/usr/local/share/chromedriver-linux64/chromedriver")
        if os.path.exists(chromedriver_path):
            driver = webdriver.Chrome(service=Service(chromedriver_path), options=ops)
        else:
            driver = webdriver.Chrome(service=Service("./chromedriver"), options=ops)
    else:
        driver = webdriver.Chrome(service=Service("chromedriver.exe"), options=ops)
    driver.set_window_size(1920, 1080)
    logger.info(f"浏览器窗口尺寸: {driver.get_window_size()}")
    return driver


def download_image(url: str, output_path: str) -> bool:
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    last_error = None
    for attempt in range(1, DOWNLOAD_MAX_RETRIES + 1):
        try:
            response = requests.get(url, timeout=DOWNLOAD_TIMEOUT)
            if response.status_code == 200:
                with open(output_path, "wb") as f:
                    f.write(response.content)
                return True
            last_error = f"status_code={response.status_code}"
            logger.warning(f"下载图片失败 (第 {attempt} 次): {last_error}, URL: {url}")
        except requests.RequestException as e:
            last_error = str(e)
            logger.warning(f"下载图片失败 (第 {attempt} 次): {e}, URL: {url}")
        if attempt < DOWNLOAD_MAX_RETRIES:
            time.sleep(DOWNLOAD_RETRY_DELAY)
    logger.error(f"下载图片失败，已重试 {DOWNLOAD_MAX_RETRIES} 次: {last_error}, URL: {url}")
    return False


def get_url_from_style(style):
    # 修复：添加空值保护
    if not style:
        raise ValueError("style 属性为空，无法解析 URL")
    match = re.search(r"url\(([^)]+)\)", style, re.IGNORECASE)
    if not match:
        raise ValueError(f"无法从 style 中解析 URL: {style}")
    url = match.group(1).strip().strip('"').strip("'")
    return url


def get_width_from_style(style):
    # 修复：添加空值保护
    if not style:
        raise ValueError("style 属性为空，无法解析宽度")
    match = re.search(r"width\s*:\s*([\d.]+)px", style, re.IGNORECASE)
    if not match:
        raise ValueError(f"无法从 style 中解析宽度: {style}")
    return float(match.group(1))


def get_height_from_style(style):
    # 修复：添加空值保护
    if not style:
        raise ValueError("style 属性为空，无法解析高度")
    match = re.search(r"height\s*:\s*([\d.]+)px", style, re.IGNORECASE)
    if not match:
        raise ValueError(f"无法从 style 中解析高度: {style}")
    return float(match.group(1))


def get_element_size(element) -> tuple[float, float]:
    size = element.size or {}
    width = size.get("width", 0)
    height = size.get("height", 0)
    if not width or not height:
        raise ValueError("无法从元素尺寸解析宽高")
    return float(width), float(height)


def process_captcha(ctx: RuntimeContext, retry_count: int = 0):
    if retry_count >= CAPTCHA_RETRY_LIMIT:
        logger.error("验证码重试次数过多，任务失败")
        return False
    try:
        download_captcha_img(ctx)
        if check_captcha(ctx):
            logger.info(f"开始识别验证码 (第 {retry_count + 1} 次尝试)")
            captcha = cv2.imread(temp_path(ctx, "captcha.jpg"))
            # 修复：检查图片是否成功读取
            if captcha is None:
                logger.error("验证码背景图读取失败，可能下载不完整")
                raise CaptchaRetryableError("验证码图片读取失败")
            with open(temp_path(ctx, "captcha.jpg"), 'rb') as f:
                captcha_b = f.read()
            bboxes = ctx.det.detection(captcha_b)
            result = dict()
            for i in range(len(bboxes)):
                x1, y1, x2, y2 = bboxes[i]
                spec = captcha[y1:y2, x1:x2]
                cv2.imwrite(temp_path(ctx, f"spec_{i + 1}.jpg"), spec)
                for j in range(3):
                    similarity, matched = compute_similarity(
                        temp_path(ctx, f"sprite_{j + 1}.jpg"),
                        temp_path(ctx, f"spec_{i + 1}.jpg")
                    )
                    similarity_key = f"sprite_{j + 1}.similarity"
                    position_key = f"sprite_{j + 1}.position"
                    if similarity_key in result.keys():
                        if float(result[similarity_key]) < similarity:
                            result[similarity_key] = similarity
                            result[position_key] = f"{int((x1 + x2) / 2)},{int((y1 + y2) / 2)}"
                    else:
                            result[similarity_key] = similarity
                            result[position_key] = f"{int((x1 + x2) / 2)},{int((y1 + y2) / 2)}"
            if check_answer(result):
                for i in range(3):
                    similarity_key = f"sprite_{i + 1}.similarity"
                    position_key = f"sprite_{i + 1}.position"
                    positon = result[position_key]
                    logger.info(f"图案 {i + 1} 位于 ({positon})，匹配率：{result[similarity_key]}")
                    slide_bg = ctx.wait.until(EC.visibility_of_element_located((By.XPATH, '//*[@id="slideBg"]')))
                    style = slide_bg.get_attribute("style")
                    x, y = int(positon.split(",")[0]), int(positon.split(",")[1])
                    width_raw, height_raw = captcha.shape[1], captcha.shape[0]
                    try:
                        width = get_width_from_style(style)
                        height = get_height_from_style(style)
                    except ValueError:
                        width, height = get_element_size(slide_bg)
                    x_offset, y_offset = float(-width / 2), float(-height / 2)
                    final_x, final_y = int(x_offset + x / width_raw * width), int(y_offset + y / height_raw * height)
                    ActionChains(ctx.driver).move_to_element_with_offset(slide_bg, final_x, final_y).click().perform()
                confirm = ctx.wait.until(
                    EC.element_to_be_clickable((By.XPATH, '//*[@id="tcStatus"]/div[2]/div[2]/div/div')))
                logger.info("提交验证码")
                confirm.click()
                time.sleep(5)
                result_el = ctx.wait.until(EC.visibility_of_element_located((By.XPATH, '//*[@id="tcOperation"]')))
                if 'show-success' in result_el.get_attribute("class"):
                    logger.info("验证码通过")
                    return True
                else:
                    logger.error("验证码未通过，正在重试")
            else:
                logger.error("验证码识别失败，正在重试")
        else:
            logger.error("当前验证码识别率低，尝试刷新")

        reload_btn = ctx.driver.find_element(By.XPATH, '//*[@id="reload"]')
        time.sleep(2)
        reload_btn.click()
        time.sleep(2)
        return process_captcha(ctx, retry_count + 1)
    except (TimeoutException, ValueError, CaptchaRetryableError) as e:
        # 修复：仅捕获预期异常（超时、解析失败、下载失败），其他程序错误直接抛出便于排查
        logger.error(f"验证码处理异常: {type(e).__name__} - {e}")
        # 尝试刷新验证码重试（多选择器兜底）
        try:
            reload_btn = None
            for selector in [
                (By.XPATH, '//*[@id="reload"]'),
                (By.CSS_SELECTOR, '#reload'),
                (By.CSS_SELECTOR, '.tc-fg-item-refresh'),
                (By.CSS_SELECTOR, '[id*="reload"]'),
            ]:
                try:
                    reload_btn = ctx.driver.find_element(*selector)
                    if reload_btn and reload_btn.is_displayed():
                        break
                    reload_btn = None
                except Exception:
                    continue
            if reload_btn:
                logger.info("找到刷新按钮，点击刷新验证码")
                time.sleep(2)
                reload_btn.click()
                time.sleep(3)
                return process_captcha(ctx, retry_count + 1)
            else:
                logger.error("未找到任何刷新按钮，放弃重试")
                return False
        except Exception as refresh_error:
            logger.error(f"无法刷新验证码，放弃重试: {refresh_error}")
            return False


def download_captcha_img(ctx: RuntimeContext):
    clear_temp_dir(ctx.temp_dir)
    slide_bg = ctx.wait.until(EC.visibility_of_element_located((By.XPATH, '//*[@id="slideBg"]')))
    img1_style = slide_bg.get_attribute("style")
    img1_url = get_url_from_style(img1_style)
    logger.info("开始下载验证码图片(1): " + img1_url)
    # 修复：检查下载是否成功
    if not download_image(img1_url, temp_path(ctx, "captcha.jpg")):
        raise CaptchaRetryableError("验证码背景图下载失败")
    sprite = ctx.wait.until(EC.visibility_of_element_located((By.XPATH, '//*[@id="instruction"]/div/img')))
    img2_url = sprite.get_attribute("src")
    logger.info("开始下载验证码图片(2): " + img2_url)
    # 修复：检查下载是否成功
    if not download_image(img2_url, temp_path(ctx, "sprite.jpg")):
        raise CaptchaRetryableError("验证码小图下载失败")


def check_captcha(ctx: RuntimeContext) -> bool:
    raw = cv2.imread(temp_path(ctx, "sprite.jpg"))
    # 修复：检查图片是否成功读取
    if raw is None:
        logger.error("验证码小图读取失败，可能下载不完整")
        return False
    for i in range(3):
        w = raw.shape[1]
        temp = raw[:, w // 3 * i: w // 3 * (i + 1)]
        cv2.imwrite(temp_path(ctx, f"sprite_{i + 1}.jpg"), temp)
        with open(temp_path(ctx, f"sprite_{i + 1}.jpg"), mode="rb") as f:
            temp_rb = f.read()
        if ctx.ocr.classification(temp_rb) in ["0", "1"]:
            return False
    return True


# 检查是否存在重复坐标,快速判断识别错误
def check_answer(d: dict) -> bool:
    # 修复：空字典或不完整结果直接返回 False
    # 需要 3 个 sprite 的 similarity + position = 6 个键
    if not d or len(d) < 6:
        logger.warning(f"验证码识别结果不完整，当前仅有 {len(d)} 个键，预期至少 6 个")
        return False
    flipped = dict()
    for key in d.keys():
        flipped[d[key]] = key
    return len(d.values()) == len(flipped.keys())


def compute_similarity(img1_path, img2_path):
    img1 = cv2.imread(img1_path, cv2.IMREAD_GRAYSCALE)
    img2 = cv2.imread(img2_path, cv2.IMREAD_GRAYSCALE)

    sift = cv2.SIFT_create()
    kp1, des1 = sift.detectAndCompute(img1, None)
    kp2, des2 = sift.detectAndCompute(img2, None)

    if des1 is None or des2 is None:
        return 0.0, 0

    bf = cv2.BFMatcher()
    matches = bf.knnMatch(des1, des2, k=2)

    good = [m for m_n in matches if len(m_n) == 2 for m, n in [m_n] if m.distance < 0.8 * n.distance]

    if len(good) == 0:
        return 0.0, 0

    similarity = len(good) / len(matches)
    return similarity, len(good)


def open_reward_page(ctx: RuntimeContext, timeout: int) -> bool:
    """打开赚取积分页：按“站内菜单点击 -> 直接 URL -> JS 跳转”顺序尝试"""
    reward_path = "/account/reward/earn"
    target_url = build_app_url(reward_path)

    def _wait_url(wait_sec: int = 8) -> bool:
        try:
            return bool(WebDriverWait(ctx.driver, wait_sec, poll_frequency=0.5).until(EC.url_contains(reward_path)))
        except TimeoutException:
            return False

    # 1. 优先尝试点击站内菜单跳转（避免 SPA 完整重载）
    try:
        links = ctx.driver.find_elements(By.XPATH, "//a[contains(@href, '/account/reward/earn')]")
        for link in links:
            try:
                if link.is_displayed() and link.is_enabled():
                    link.click()
                    if _wait_url(8):
                        logger.info("通过站内菜单进入赚取积分页")
                        dismiss_modal_confirm(ctx.driver, 2.0)
                        return True
            except Exception:
                try:
                    ctx.driver.execute_script("arguments[0].click();", link)
                    if _wait_url(8):
                        logger.info("通过站内菜单(JS)进入赚取积分页")
                        dismiss_modal_confirm(ctx.driver, 2.0)
                        return True
                except Exception:
                    continue
    except Exception:
        pass

    # 2. 直接 URL 访问
    ctx.driver.get(target_url)
    if _wait_url(max(8, timeout)):
        logger.info("通过直接 URL 进入赚取积分页")
        dismiss_modal_confirm(ctx.driver, 2.0)
        return True

    # 3. JS 兜底跳转
    fallback_url = f"{target_url}?_ts={int(time.time() * 1000)}"
    try:
        ctx.driver.execute_script("window.location.assign(arguments[0]);", fallback_url)
    except Exception:
        ctx.driver.get(fallback_url)
    ok = _wait_url(max(10, timeout))
    dismiss_modal_confirm(ctx.driver, 2.0)
    return ok


def get_daily_sign_header_text(driver: WebDriver) -> str:
    """只读取“每日签到”卡片头部/容器内的可见文本，绝不读取全页 page_source"""
    for xpath in (XPATH_CONFIG["SIGN_IN_HEADER"], XPATH_CONFIG["SIGN_IN_HEADER_FALLBACK"]):
        try:
            elements = driver.find_elements(By.XPATH, xpath)
            if elements:
                raw_text = (elements[0].get_attribute("innerText") or elements[0].text or "").strip()
                text = re.sub(r"\s+", " ", raw_text).strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


def get_daily_sign_card_text(driver: WebDriver) -> str:
    """读取“每日签到”整个卡片的可见文本（用于诊断）"""
    try:
        elements = driver.find_elements(By.XPATH, XPATH_CONFIG["SIGN_IN_CARD"])
        if elements:
            raw_text = (elements[0].get_attribute("innerText") or elements[0].text or "").strip()
            return re.sub(r"\s+", " ", raw_text).strip()
    except Exception:
        pass
    return ""


def detect_daily_sign_done_pattern(driver: WebDriver) -> str | None:
    """仅在“每日签到”模块头部文本中检测完成态关键字"""
    header_text = get_daily_sign_header_text(driver)
    for pattern in DAILY_SIGN_DONE_PATTERNS:
        if pattern in header_text:
            return pattern
    return None


def wait_daily_sign_done_pattern(driver: WebDriver, timeout: int = 12) -> str | None:
    """等待“每日签到”模块状态变为已完成/已领取"""
    wait = WebDriverWait(driver, timeout, poll_frequency=0.5)
    try:
        return wait.until(lambda d: detect_daily_sign_done_pattern(d) or False)
    except TimeoutException:
        return None


def wait_daily_sign_section_ready(ctx: RuntimeContext, timeout: int) -> bool:
    """等待“每日签到”卡片渲染就绪"""
    wait_sec = max(25, timeout)
    wait = WebDriverWait(ctx.driver, wait_sec, poll_frequency=0.5)

    def _probe(d: WebDriver) -> bool:
        if d.find_elements(By.XPATH, XPATH_CONFIG["SIGN_IN_HEADER"]):
            return True
        if d.find_elements(By.XPATH, XPATH_CONFIG["SIGN_IN_CARD"]):
            return True
        return bool(d.find_elements(By.XPATH, "//span[contains(normalize-space(.), '每日签到')]"))

    try:
        return bool(wait.until(_probe))
    except TimeoutException:
        return False


def find_daily_sign_claim_button(ctx: RuntimeContext, timeout: int):
    """在“每日签到”模块内寻找可点击的领取按钮"""
    wait = WebDriverWait(ctx.driver, timeout, poll_frequency=0.5)

    def _locate(d: WebDriver):
        best_hidden = None
        for xpath in (XPATH_CONFIG["SIGN_IN_BTN"], XPATH_CONFIG["SIGN_IN_BTN_LEGACY"], XPATH_CONFIG["SIGN_IN_BTN_FALLBACK"]):
            elems = d.find_elements(By.XPATH, xpath)
            for el in elems:
                try:
                    if not el.is_enabled():
                        continue
                    if el.is_displayed():
                        return el
                    # 新版卡片头按钮在部分布局下判定为不可见（如被 overlay 遮挡），
                    # 但 JS 点击仍能触发其 click 监听打开弹窗，先记住它作为兜底。
                    if best_hidden is None:
                        best_hidden = el
                except Exception:
                    continue
        return best_hidden or False

    return wait.until(_locate)


def get_daily_sign_button_diagnostic(driver: WebDriver) -> str:
    """返回有限的签到按钮候选诊断，供定位失败日志使用。"""
    details = []
    for key in ("SIGN_IN_BTN", "SIGN_IN_BTN_LEGACY", "SIGN_IN_BTN_FALLBACK"):
        try:
            elements = driver.find_elements(By.XPATH, XPATH_CONFIG[key])
            for element in elements[:2]:
                try:
                    text = re.sub(r"\s+", " ", (element.get_attribute("innerText") or element.text or "")).strip()
                    styles = driver.execute_script(
                        """let n=arguments[0], out=[]; for(let i=0;n&&i<5;i++,n=n.parentElement){
                        let s=getComputedStyle(n),r=n.getBoundingClientRect();
                        out.push(n.tagName.toLowerCase()+'.'+String(n.className||'').replace(/\\s+/g,'.')+
                        '{display:'+s.display+',visibility:'+s.visibility+',opacity:'+s.opacity+
                        ',rect:'+Math.round(r.width)+'x'+Math.round(r.height)+'@'+Math.round(r.x)+','+Math.round(r.y)+'}');}
                        return out.join(' <- ');""",
                        element,
                    )
                    details.append(
                        f"{key}:tag={element.tag_name}, visible={element.is_displayed()}, enabled={element.is_enabled()}, text=[{text[:100]}], styles=[{styles}]"
                    )
                except Exception as e:
                    details.append(f"{key}: candidate inspection failed ({type(e).__name__})")
            if not elements:
                details.append(f"{key}: no matches")
        except Exception as e:
            details.append(f"{key}: XPath error ({type(e).__name__})")
    try:
        generic = driver.find_elements(By.XPATH, "//*[contains(normalize-space(.), '去签到')]")
        other_matches = []
        for element in generic:
            try:
                text = re.sub(r"\s+", " ", (element.get_attribute("innerText") or element.text or "")).strip()
                if not text or len(text) > 100:
                    continue
                styles = driver.execute_script(
                    """let n=arguments[0], out=[]; for(let i=0;n&&i<4;i++,n=n.parentElement){
                    let s=getComputedStyle(n),r=n.getBoundingClientRect();
                    out.push(n.tagName.toLowerCase()+'.'+String(n.className||'').replace(/\\s+/g,'.')+
                    '{display:'+s.display+',visibility:'+s.visibility+',rect:'+Math.round(r.width)+'x'+Math.round(r.height)+'}');}
                    return out.join(' <- ');""",
                    element,
                )
                other_matches.append(f"{element.tag_name},visible={element.is_displayed()},text=[{text}],styles=[{styles}]")
                if len(other_matches) >= 8:
                    break
            except Exception:
                continue
        details.append("page text candidates=[" + " | ".join(other_matches) + "]")
    except Exception as e:
        details.append(f"page text query failed ({type(e).__name__})")
    return " | ".join(details)


def switch_to_visible_captcha_frame(driver: WebDriver, timeout: int = 10):
    """切换到有尺寸的腾讯验证码 iframe。

    注意：headless 布局下弹层可能整体位于视口外，is_displayed() 会误报 False，
    因此只要求 iframe 存在且宽高大于 0，不再依赖 is_displayed()。
    页面残留的隐藏 iframe（宽高为 0）依然会被排除。
    """
    driver.switch_to.default_content()

    def _visible_frame(d):
        for frame in d.find_elements(By.XPATH, CAPTCHA_IFRAME_XPATH):
            try:
                size = frame.size or {}
                if size.get("width", 0) > 0 and size.get("height", 0) > 0:
                    return frame
            except Exception:
                continue
        return False

    frame = WebDriverWait(driver, timeout, poll_frequency=0.5).until(_visible_frame)
    driver.switch_to.frame(frame)
    return frame


def switch_to_new_window_if_opened(driver: WebDriver, existing_handles: set[str], timeout: float = 2.0) -> bool:
    """签到按钮可能打开独立页面；发现新窗口时切换过去继续检测交互。"""
    try:
        new_handle = WebDriverWait(driver, timeout, poll_frequency=0.25).until(
            lambda d: next((handle for handle in d.window_handles if handle not in existing_handles), False)
        )
    except TimeoutException:
        return False
    driver.switch_to.window(new_handle)
    logger.info("签到操作打开了新窗口，已切换到新窗口继续处理")
    return True


def log_sign_modal_diagnostic(driver: WebDriver, stage: str) -> None:
    """输出弹窗/奖励按钮状态诊断，帮助定位新版签到弹窗交互问题。"""
    try:
        panels = driver.find_elements(By.XPATH, XPATH_CONFIG["DAILY_SIGN_MODAL"])
        logger.info(f"[{stage}] 每日签到弹窗面板数量: {len(panels)}")
        for i, panel in enumerate(panels[:2]):
            try:
                logger.info(
                    f"[{stage}] 面板{i}: displayed={panel.is_displayed()}, "
                    f"text=[{re.sub(chr(10), ' ', (panel.get_attribute('innerText') or '')[:120])}]"
                )
            except Exception as e:
                logger.warning(f"[{stage}] 面板{i} 读取失败: {type(e).__name__}")
        rewards = driver.find_elements(By.XPATH, DAILY_SIGN_REWARD_BTN_XPATH)
        logger.info(f"[{stage}] 奖励按钮数量: {len(rewards)}")
        for btn in rewards[:3]:
            try:
                styles = driver.execute_script(
                    """let n=arguments[0], out=[];
                    for(let i=0;n&&i<6;i++,n=n.parentElement){
                      let s=getComputedStyle(n),r=n.getBoundingClientRect();
                      out.push(n.tagName.toLowerCase()+'.'+String(n.className||'').trim().replace(/\\s+/g,'.')+
                      '{display:'+s.display+',visibility:'+s.visibility+',opacity:'+s.opacity+
                      ',rect:'+Math.round(r.width)+'x'+Math.round(r.height)+'}');
                    }
                    return out.join(' <- ');""",
                    btn,
                )
                logger.info(f"[{stage}] 奖励按钮: displayed={btn.is_displayed()}, enabled={btn.is_enabled()}, styles=[{styles}]")
            except Exception as e:
                logger.warning(f"[{stage}] 奖励按钮读取失败: {type(e).__name__}")
    except Exception as e:
        logger.warning(f"[{stage}] 弹窗诊断失败: {type(e).__name__}")


def click_daily_sign_reward_button(driver: WebDriver, timeout: int = 10) -> bool:
    """在新版“每日签到”弹窗中点击奖励按钮（默认第一个：500 积分）。

    前端流程：点击卡片头打开弹窗 -> 弹窗内选择奖励 -> 触发腾讯验证码。
    若奖励按钮尚未出现（弹窗动画中），会持续等待 timeout 秒。
    """
    wait = WebDriverWait(driver, timeout, poll_frequency=0.5)

    def _find(d: WebDriver):
        # 注意：headless 布局下弹窗可能整体位于视口外，is_displayed() 会误报 False，
        # 因此只要求元素存在于 DOM 且可交互（enabled），点击一律走 JS。
        try:
            for btn in d.find_elements(By.XPATH, DAILY_SIGN_REWARD_BTN_XPATH):
                if btn.is_enabled():
                    return btn
        except Exception:
            pass
        return False

    try:
        reward_btn = wait.until(_find)
    except TimeoutException:
        log_sign_modal_diagnostic(driver, "奖励按钮等待超时")
        return False
    reward_text = re.sub(r"\s+", " ", (reward_btn.get_attribute("innerText") or reward_btn.text or "").strip())
    driver.execute_script("arguments[0].click();", reward_btn)
    logger.info(f"已点击每日签到弹窗奖励按钮: [{reward_text}]")
    return True


def click_daily_sign_dialog_action(driver: WebDriver) -> bool:
    """兼容旧交互：在每日签到/转盘弹窗内点击确认签到；新版弹窗则点击奖励按钮。"""
    # 新版弹窗：点击奖励按钮（500 积分 / 抽奖券）后触发验证码
    if driver.find_elements(By.XPATH, XPATH_CONFIG["DAILY_SIGN_MODAL"]):
        return click_daily_sign_reward_button(driver, timeout=1)
    for dialog in driver.find_elements(By.XPATH, SIGN_DIALOG_XPATH):
        try:
            if not dialog.is_displayed():
                continue
            dialog_text = re.sub(r"\s+", " ", (dialog.get_attribute("innerText") or dialog.text or "")).strip()
            if not any(marker in dialog_text for marker in ("每日签到", "转盘")):
                continue
            for action in dialog.find_elements(By.XPATH, SIGN_DIALOG_ACTION_XPATH):
                try:
                    if action.is_displayed() and action.is_enabled():
                        action_text = re.sub(r"\s+", " ", (action.get_attribute("innerText") or action.text or "")).strip()
                        action.click()
                        logger.info(f"已点击每日签到弹窗确认按钮: [{action_text}]")
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def read_current_points(ctx: RuntimeContext, timeout: int = 15) -> int | None:
    """读取页面显示的当前剩余积分"""
    selectors = (XPATH_CONFIG["POINTS_BY_LABEL"], XPATH_CONFIG["POINTS_HEADER"])

    def _find_points_element(driver):
        for xpath in selectors:
            for element in driver.find_elements(By.XPATH, xpath):
                try:
                    if element.is_displayed():
                        raw_text = element.get_attribute("textContent") or element.text or ""
                        digits = re.findall(r"\d+", raw_text)
                        if digits:
                            return int("".join(digits))
                except Exception:
                    continue
        return False

    try:
        return WebDriverWait(ctx.driver, timeout, poll_frequency=0.5).until(_find_points_element)
    except Exception as e:
        logger.warning(f"获取积分信息失败: {e}")
        return None


def run():
    ctx = None
    driver = None
    temp_dir = None
    debug = False
    success = False  # 跟踪签到是否成功，用于设置退出码
    try:
        # 从环境变量读取配置
        timeout = int(os.environ.get("TIMEOUT", "15"))
        max_delay = int(os.environ.get("MAX_DELAY", "90"))
        user = os.environ.get("RAINYUN_USER", "")
        pwd = os.environ.get("RAINYUN_PWD", "")
        # GitHub Action 无状态
        debug = True
        # GitHub Actions 环境一定是linux
        linux = True

        # 检查必要配置
        if not user or not pwd:
            logger.error("请设置 RAINYUN_USER 和 RAINYUN_PWD 环境变量")
            return

        logger.info(f"━━━━━━ 雨云签到 v{APP_VERSION} ━━━━━━")

        delay = random.randint(0, max_delay)
        delay_sec = random.randint(0, 60)
        # 可选：随机延迟（可用于避免集中请求）
        # logger.info(f"随机延时等待 {delay} 分钟 {delay_sec} 秒")
        # time.sleep(delay * 60 + delay_sec)
        
        logger.info("初始化 ddddocr")
        ocr = ddddocr.DdddOcr(ocr=True, show_ad=False)
        det = ddddocr.DdddOcr(det=True, show_ad=False)
        
        logger.info("初始化 Selenium")
        driver = init_selenium(debug=debug, linux=linux)
        
        # 过 Selenium 检测
        with open("stealth.min.js", mode="r") as f:
            js = f.read()
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": js
        })
        
        wait = WebDriverWait(driver, timeout)
        temp_dir = tempfile.mkdtemp(prefix="rainyun-")
        ctx = RuntimeContext(
            driver=driver,
            wait=wait,
            ocr=ocr,
            det=det,
            temp_dir=temp_dir
        )

        # 进行登录
        logged_in = do_login(ctx, user, pwd)
        if not logged_in:
            logger.error("登录失败，任务终止")
            return

        logger.info("正在转到赚取积分页")
        open_reward_page(ctx, timeout)

        if not wait_daily_sign_section_ready(ctx, timeout):
            logger.warning(f"赚取积分页首次加载未检测到每日签到模块，尝试刷新重试 (URL: {ctx.driver.current_url})")
            ctx.driver.refresh()
            dismiss_modal_confirm(ctx.driver, 2.0)
            if not wait_daily_sign_section_ready(ctx, timeout):
                raise Exception(f"赚取积分页加载超时：未找到每日签到模块 (URL: {ctx.driver.current_url})")

        start_points = read_current_points(ctx, timeout=timeout)
        if start_points is not None:
            logger.info(f"签到前积分: {start_points} | 约为 {start_points / POINTS_TO_CNY_RATE:.2f} 元")

        header_text = get_daily_sign_header_text(ctx.driver)
        logger.info(f"每日签到模块当前文案: [{header_text}]")

        # 1. 先检查“每日签到”模块内部是否已经显示完成状态
        done_pattern = detect_daily_sign_done_pattern(ctx.driver)
        if done_pattern:
            logger.info(f"今日已签到（每日签到模块检测到：{done_pattern}），跳过签到流程")
            success = True
            return

        # 2. 寻找并点击“每日签到”模块内的领取按钮
        original_window_handle = ctx.driver.current_window_handle
        window_handles_before_click = set(ctx.driver.window_handles)
        try:
            earn = find_daily_sign_claim_button(ctx, timeout)
            btn_text = re.sub(r"\s+", " ", (earn.get_attribute("innerText") or earn.text or "").strip())
            logger.info(f"点击每日签到按钮: [{btn_text}]")
            try:
                if earn.is_displayed():
                    earn.click()
                else:
                    logger.warning("每日签到按钮在当前布局中不可见，改用 JS 触发点击事件")
                    ctx.driver.execute_script("arguments[0].click();", earn)
            except Exception:
                ctx.driver.execute_script("arguments[0].click();", earn)
        except TimeoutException:
            done_pattern = detect_daily_sign_done_pattern(ctx.driver)
            if done_pattern:
                logger.info(f"今日已签到（每日签到模块检测到：{done_pattern}），跳过签到流程")
                success = True
                return
            header_text = get_daily_sign_header_text(ctx.driver)
            card_text = get_daily_sign_card_text(ctx.driver)
            button_details = get_daily_sign_button_diagnostic(ctx.driver)
            logger.error(f"每日签到按钮定位失败诊断: header_text=[{header_text}], card_text=[{card_text[:180]}], candidates=[{button_details}]")
            raise Exception("未找到每日签到领取按钮，且每日签到模块未显示已签到状态")

        logger.info("处理验证码")
        try:
            switch_to_new_window_if_opened(ctx.driver, window_handles_before_click)
            log_sign_modal_diagnostic(ctx.driver, "点击卡片按钮后")
            try:
                switch_to_visible_captcha_frame(ctx.driver, timeout=max(8, timeout))
            except TimeoutException:
                # 新版前端：点击“去签到”后先弹出“每日签到”弹窗，需在弹窗中点击奖励按钮才会触发验证码
                if not click_daily_sign_reward_button(ctx.driver, timeout=max(8, timeout)):
                    raise
                switch_to_visible_captcha_frame(ctx.driver, timeout=max(8, timeout))
            if not process_captcha(ctx):
                logger.error(f"验证码重试次数过多，任务失败。当前页面状态: {ctx.driver.current_url}")
                raise Exception("验证码识别重试次数过多，签到失败")
        except TimeoutException:
            logger.warning("未检测到可见验证码 iframe，已检查签到弹窗，继续核验签到状态")
        finally:
            ctx.driver.switch_to.default_content()

        # 3. 验证码处理后，确认“每日签到”模块确实变为完成状态（或积分增加）
        done_pattern = wait_daily_sign_done_pattern(ctx.driver, timeout=12)
        time.sleep(2)
        current_points = read_current_points(ctx, timeout=timeout)

        # 若签到在新窗口完成但状态/余额只同步回主页面，再到主页面补做一次核验。
        if not done_pattern and (current_points is None or start_points is None or current_points <= start_points):
            try:
                if original_window_handle in ctx.driver.window_handles and ctx.driver.current_window_handle != original_window_handle:
                    ctx.driver.switch_to.window(original_window_handle)
                    done_pattern = wait_daily_sign_done_pattern(ctx.driver, timeout=3)
                    current_points = read_current_points(ctx, timeout=3)
            except Exception as e:
                logger.debug(f"主页面签到状态回查失败: {e}")

        if current_points is not None:
            earned_info = ""
            if start_points is not None and current_points >= start_points:
                earned_info = f" (本次获得 +{current_points - start_points} 积分)"
            logger.info(f"当前剩余积分: {current_points}{earned_info} | 约为 {current_points / POINTS_TO_CNY_RATE:.2f} 元")

        if not done_pattern:
            header_text = get_daily_sign_header_text(ctx.driver)
            if start_points is not None and current_points is not None and current_points > start_points:
                logger.info("检测到积分已增加，确认签到成功！")
            else:
                logger.error(f"签到后状态校验未通过，每日签到模块文案仍为: [{header_text}]")
                raise Exception(f"验证码处理后未检测到每日签到完成状态 (当前模块文案: {header_text})")
        else:
            logger.info(f"每日签到完成确认（模块状态：{done_pattern}）")

        logger.info("任务执行成功！")
        success = True
        
    except Exception as e:
        logger.error(f"脚本执行异常终止: {e}")

    finally:
        # === 清理资源 ===
        
        # 1. 关闭浏览器
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

        # 2. 获取所有日志内容
        log_content = log_capture_string.getvalue()

        # 3. 发送通知
        logger.info("正在发送通知...")
        send("雨云签到", log_content)

        # 4. 释放内存
        log_capture_string.close()
        if temp_dir and not debug:
            shutil.rmtree(temp_dir, ignore_errors=True)

        # 5. 设置退出码：成功=0，失败=1（让 GitHub Actions 真实反映结果）
        if success:
            sys.exit(0)
        else:
            sys.exit(1)


if __name__ == "__main__":
    run()
