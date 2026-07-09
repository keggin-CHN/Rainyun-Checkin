# 🌧️ 雨云自动签到 (纯协议版)

> **v3.0** — 纯 HTTP 协议，无浏览器依赖，秒级完成

雨云（Rainyun）每日自动签到工具，支持 **GitHub Actions 一键部署**，无需服务器。

## ✨ 特性

- ✅ **纯协议签到** — 直接调用雨云 API，无需 Selenium/验证码
- ✅ **极速完成** — 3 个 HTTP 请求搞定（查积分 → 签到 → 再查积分）
- ✅ **多平台通知** — Server酱、Bark、Telegram 等 20+ 渠道
- ✅ **积分续费** — 可选自动续费游戏云服务器
- ✅ **零依赖** — 仅需 `requests`，无需 Chrome/ddddocr/opencv

## 🚀 快速开始

### 1. 获取 API 密钥

前往 [雨云控制台](https://app.rainyun.com) → **用户中心** → **API 密钥**，复制你的密钥。

### 2. Fork 本仓库

点击右上角 **Fork** 按钮。

### 3. 配置 Secrets

进入你 Fork 的仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| 变量名 | 说明 | 必填 |
|--------|------|------|
| `RAINYUN_API_KEY` | 雨云 API 密钥 | ✅ |
| `TG_BOT_TOKEN` | Telegram Bot Token | ❌ |
| `TG_USER_ID` | Telegram 用户 ID | ❌ |

### 4. 手动测试

**Actions** → **Checkin** → **Run workflow** → 等待完成

## ⚙️ 环境变量

### 必填
| 变量名 | 说明 |
|--------|------|
| `RAINYUN_API_KEY` | 雨云 API 密钥（后台获取） |

### 可选
| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `API_BASE_URL` | 雨云 API 地址 | `https://api.v2.rainyun.com` |
| `POINTS_TO_CNY_RATE` | 积分兑人民币比例 | `2000` |
| `REQUEST_TIMEOUT` | 请求超时（秒） | `15` |

### 通知渠道（按需配置）
详见 `.env.example`，支持：Bark、钉钉、飞书、Telegram、Server酱、PushPlus、企业微信 等。

## 📁 项目结构

```
├── .github/workflows/
│   ├── checkin.yml       # 签到工作流（每天）
│   ├── point_renew.yml   # 积分续费（每 7 天）
│   └── keepalive.yml     # 仓库保活（每 14 天）
├── checkin.py            # 签到主脚本（纯协议）
├── api_client.py         # 雨云 API 客户端（续费用）
├── server_manager.py     # 服务器管理（续费用）
├── notify.py             # 通知推送模块
├── config.py             # 配置读取
└── requirements.txt      # 依赖（仅 requests）
```

## 🔄 从旧版迁移

旧版使用 `RAINYUN_USER` + `RAINYUN_PWD` + Selenium + 验证码识别。新版改用 `RAINYUN_API_KEY`：

1. 获取 API 密钥：雨云后台 → 用户中心 → API 密钥
2. 在 GitHub Secrets 中添加 `RAINYUN_API_KEY`
3. 可以删除 `RAINYUN_USER` 和 `RAINYUN_PWD`

## ⏰ 自动执行时间

| 工作流 | 频率 | 时间 |
|--------|------|------|
| 签到 | 每天 | UTC 00:00（北京 08:00） |
| 积分续费 | 每 7 天 | UTC 00:00 |
| 仓库保活 | 每 14 天 | UTC 00:00 |

## 📄 许可证

MIT License

## 🙏 致谢

- 原版: [SerendipityR-2022/Rainyun-Qiandao](https://github.com/SerendipityR-2022/Rainyun-Qiandao)
- 改进: [fatekey](https://github.com/fatekey/Rainyun-Qiandao) · [Jielumoon](https://github.com/Jielumoon/Rainyun-Qiandao) · [0x6768](https://github.com/0x6768/Rainyun-Checkin)
- API 参考: [xingkongmcqwq/rainyun-api](https://github.com/xingkongmcqwq/rainyun-api)
