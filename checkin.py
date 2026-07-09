#!/usr/bin/env python3
"""
雨云纯协议签到 — 替代原 Selenium 方案
仅依赖 requests，无需浏览器/验证码/ddddocr
"""
import io
import json
import logging
import os
import sys
import time

import requests

# ── 日志 ────────────────────────────────────────────────────────
log_capture_string = io.StringIO()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

_string_handler = logging.StreamHandler(log_capture_string)
_string_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(_string_handler)

# ── 通知 ────────────────────────────────────────────────────────
try:
    from notify import send as notify_send
except Exception:
    def notify_send(title, content):
        pass

# ── 常量 ────────────────────────────────────────────────────────
API_BASE = os.environ.get("API_BASE_URL", "https://api.v2.rainyun.com").rstrip("/")
APP_BASE = os.environ.get("APP_BASE_URL", "https://app.rainyun.com").rstrip("/")
POINTS_TO_CNY = int(os.environ.get("POINTS_TO_CNY_RATE", "2000"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "15"))


# ── API 封装 ────────────────────────────────────────────────────
class RainyunClient:
    """极简雨云 API 客户端"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "X-Api-Key": api_key,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })

    def _get(self, path: str) -> dict:
        r = self.session.get(f"{API_BASE}{path}", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 200:
            raise RuntimeError(f"API 错误 [{data.get('code')}]: {data.get('message', '未知')}")
        return data.get("data", {})

    def _post(self, path: str, body: dict) -> dict:
        r = self.session.post(f"{API_BASE}{path}", json=body, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 200:
            raise RuntimeError(f"API 错误 [{data.get('code')}]: {data.get('message', '未知')}")
        return data.get("data", {})

    # ── 用户 ──
    def get_user(self) -> dict:
        """返回 {ID, Name, Points, ...}"""
        return self._get("/user/")

    # ── 签到 ──
    def checkin(self) -> str:
        """执行每日签到，返回 data 字段 (通常 'ok')"""
        return self._post("/user/reward/tasks", {
            "task_name": "每日签到",
            "verifyCode": "",
        })

    # ── 任务列表 ──
    def get_tasks(self) -> list:
        """获取任务列表（可选，用于检测签到状态）"""
        return self._get("/user/reward/tasks")


# ── 主流程 ──────────────────────────────────────────────────────
def run():
    success = False
    try:
        # 读取配置
        api_key = os.environ.get("RAINYUN_API_KEY", "").strip()
        user_env = os.environ.get("RAINYUN_USER", "").strip()
        pwd_env = os.environ.get("RAINYUN_PWD", "").strip()

        # 兼容旧配置：如果没配 RAINYUN_API_KEY 但有 USER/PWD，提示迁移
        if not api_key:
            if user_env:
                logger.error(
                    "⚠️ 当前纯协议方案需要 RAINYUN_API_KEY（从雨云后台获取），"
                    "不再依赖 RAINYUN_USER/RAINYUN_PWD。"
                    "请前往 雨云控制台 → 用户中心 → API密钥 获取。"
                )
            else:
                logger.error("请设置 RAINYUN_API_KEY 环境变量（雨云后台 → 用户中心 → API密钥）")
            return

        logger.info("━━━━━━ 雨云签到 (纯协议) ━━━━━━")

        client = RainyunClient(api_key)

        # 1. 获取用户信息
        logger.info("获取用户信息...")
        user = client.get_user()
        uid = user.get("ID", "?")
        name = user.get("Name", "?")
        points_before = user.get("Points", 0)
        logger.info(f"用户: {name} (ID: {uid})")
        logger.info(f"签到前积分: {points_before} (≈ {points_before / POINTS_TO_CNY:.2f} 元)")

        # 2. 执行签到
        logger.info("执行每日签到...")
        try:
            result = client.checkin()
            if result == "ok":
                logger.info("✅ 签到成功！")
            else:
                logger.info(f"签到返回: {result}")
        except RuntimeError as e:
            err_msg = str(e)
            # 已签到 / 重复签到
            if "30011" in err_msg or "已签到" in err_msg or "already" in err_msg.lower():
                logger.info("ℹ️ 今日已签到，跳过")
            else:
                logger.error(f"签到失败: {e}")
                raise

        # 3. 查询签到后积分
        time.sleep(1)
        user_after = client.get_user()
        points_after = user_after.get("Points", 0)
        gained = points_after - points_before
        logger.info(f"签到后积分: {points_after} (≈ {points_after / POINTS_TO_CNY:.2f} 元)")
        if gained > 0:
            logger.info(f"本次获得: +{gained} 积分")

        success = True

    except Exception as e:
        logger.error(f"脚本异常: {e}")

    finally:
        # 获取日志
        log_content = log_capture_string.getvalue()
        log_capture_string.close()

        # 发送通知
        logger.info("发送通知...")
        notify_send("雨云签到", log_content)

        sys.exit(0 if success else 1)


if __name__ == "__main__":
    run()
