# ChatGPT-Account-Pool

<div align="center">

# 🚀 ChatGPT-Account-Pool
### 高可用 ChatGPT 账号池与 Token 智能维护系统
**High-Availability ChatGPT Account & Token Pool with Auto-Refresh, Camoufox Turnstile Solver & Mihomo Proxy Integration**

[![GitHub Stars](https://img.shields.io/github/stars/SeiShonagon520/ChatGPT-Account-Pool?style=flat-square&logo=github&color=gold)](https://github.com/SeiShonagon520/ChatGPT-Account-Pool)
[![Release](https://img.shields.io/badge/release-v2.0.0-emerald.svg?style=flat-square)](https://github.com/SeiShonagon520/ChatGPT-Account-Pool/releases)
[![Python](https://img.shields.io/badge/python-3.11+-brightgreen.svg?style=flat-square&logo=python)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-compose-blue.svg?style=flat-square&logo=docker)](docker-compose.yml)
[![FastAPI](https://img.shields.io/badge/backend-FastAPI-009688.svg?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/frontend-React%2018%20%7C%20Vite-61dafb.svg?style=flat-square&logo=react)](frontend/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg?style=flat-square)](LICENSE)

[English](#-english-overview) · [简体中文](#-项目简介) · [快速开始](#-快速开始-docker-compose) · [核心特性](#-核心特性) · [API 文档](#-api-接口与下游集成)

</div>

---

## 📖 项目简介

**ChatGPT-Account-Pool** 是一套专为生产环境与大模型中转站（如 One-API / New-API / chatgpt2api）打造的**高可用 ChatGPT 账号资产池与 Token 智能调度管理系统**。

面对 OpenAI 严格的风控、Cloudflare Turnstile 验证盾、节点黑名单以及频繁的 401 令牌失效，本项目将**协议高速直连**、**Camoufox 真实指纹浏览器**、**Mihomo (Clash) 代理池原生协同**以及**微软母体邮箱池**深度融合，实现账号从**自动注册、存活监测、失效抢救、过盾自愈到下游分发**的全生命周期自动化运作。

---

## ✨ 核心特性

### 1. ⚡ 401 深度验活与自动故障自愈 (Auto Revive)
- **多维度探活**：通过 Camoufox 浏览器或官方 API 并发检测 Access Token (AT) 与 Refresh Token (RT) 存活状态。
- **自动收信抢救**：针对 401 失活账号，系统自动调度关联的微软母体邮箱，异步收取最新邮箱验证码，通过协议重登并刷回全新的有效 AT/RT，全自动化救号。
- **专属 SUB 导出**：支持对本次 401 恢复成功的账号进行一键专属导出，标准 Sub2API 格式直接对接聚合分发客户端。

### 2. 🛡️ Camoufox 真实指纹浏览器与 Cloudflare Turnstile 自动破解
- **内置指纹浏览器**：采用 Camoufox (定制防检测 Firefox 内核)，完美绕过浏览器特征指纹检测。
- **自动化过盾**：内置智能 Cloudflare Turnstile Solver，自动定位 iframe 挑战、计算坐标并模拟人工点击打勾。
- **智能降级与恢复**：日常验活走极速纯协议模式；遇到密码页人机盾阻断时，系统自动无缝唤起 Camoufox 浏览器过盾并签发新 Token。

### 3. 🌐 原生 Mihomo 代理池集成与智能 Slot 轮换
- **双容器协同**：`app` 与 `mihomo` 容器原生互通，一键 Docker Compose 启动即自带专业代理调度能力。
- **订阅一键同步**：支持标准 Clash / Mihomo 订阅链接，自动解析 Hysteria2 / Vless / SS / Trojan 等全协议节点。
- **智能 Slot 分流**：支持多账号分配独立代理 Slot 端口，遇到 Cloudflare 挑战或封禁时自动毫秒级轮换可用节点。

### 4. 🗂️ 全格式账号导入与自动解析
- **多格式兼容**：支持导入 **JSON 文件**、**Sub2API 格式**、**CPA 格式**、**纯文本行格式**。
- **智能字段提取**：自动识别邮箱、密码、Access Token、Refresh Token、Session Token、2FA Secret 并填充入库。

### 5. 🔍 账号详情抽屉与 2FA TOTP 动态码实时生成
- **一键查看详情**：点击列表右侧「编辑」按钮，进入账号细节参数面板。
- **实时 2FA 动态码**：只要账号绑定了 TOTP Secret，前端**实时计算并展示 6 位动态验证码**，提供实时倒计时与一键复制功能。
- **便捷编辑**：支持修改账号密码、添加自定义备注、更新 Token，方便人工介入与凭据维护。

### 6. 📬 微软母体邮箱池与裂变用量监控
- **一键批量测活**：对邮箱池内所有微软母体邮箱（Outlook / Hotmail）发起高并发 OAuth 探活，自动检测凭据有效性。
- **失效邮箱隔离**：测活失败或鉴权失效的邮箱自动置为「已隔离」并禁用，防止裂变注册任务中断。
- **用量仪表盘**：实时统计母体总数、剩余可用裂变次数、已用/总限额比例，并在配额告急时醒目预警。

### 7. 🔐 账号数据库加密快照与跨机迁移
- **加密快照**：使用 PBKDF2 与 XSalsa20-Poly1305 认证加密，打包 SQLite 事务快照、元数据清单以及解密主密钥生成 `.fgmbak` 文件。
- **跨机无损迁移**：新设备一键恢复，直接无损解密全部敏感凭据。

### 8. 📋 任务实时监控与全链路诊断日志
- **多状态回溯**：支持「运行中 / 已完成 / 全部任务」分类筛选，任务结束后历史记录持久化保存，不丢失任何执行痕迹。
- **全景恢复详情**：直观展示 401 验活抢救指标（需恢复数、尝试登录数、成功解救数、失败数），支持一键复制成功账号与专用导出。
- **智能错误定位**：自动提取失败根本原因（频控拦截、节点封禁、挑战超时），内置完整底层诊断日志面板与一键复制。

---

## 🚀 快速开始 (Docker Compose)

推荐使用 Docker Compose 进行一键部署，开箱即用：

### 1. 克隆代码仓库
```bash
git clone https://github.com/SeiShonagon520/ChatGPT-Account-Pool.git chatgpt-account-pool
cd chatgpt-account-pool
```

### 2. 配置环境变量
```bash
cp .env.example .env
# 编辑 .env 文件，设置访问密码（APP_PASSWORD）等参数
```

### 3. 启动容器集群
```bash
docker compose up -d
```

### 4. 初始化 Camoufox 浏览器内核（首次启动需要）
```bash
docker compose exec app python -m camoufox fetch
```

访问 `http://localhost:8000` 即可进入管理面板！

---

## 💻 本地开发部署

### 环境要求
- **Python** 3.11+
- **Node.js** 18+ 与 npm
- **Camoufox** 依赖

```bash
# 1. 安装 Python 依赖
python -m venv .venv
source .venv/bin/activate  # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m camoufox fetch

# 2. 安装并构建前端
cd frontend
npm install
npm run build
cd ..

# 3. 启动后端服务
python main.py
```

---

## 🔌 API 接口与下游集成

本项目提供标准的 RESTful API，可无缝对接 **One-API**、**New-API** 或第三方聚合平台：

| 接口端点 | 方法 | 说明 |
| :--- | :--- | :--- |
| `/api/accounts` | `GET` | 获取账号列表，支持状态筛选与分页 |
| `/api/accounts/export` | `GET` | 批量导出账号（支持 json, sub2api, cpa 等格式） |
| `/api/accounts/check-refresh-tokens` | `POST` | 触发 401 验活与自动抢救任务 |
| `/api/accounts/import` | `POST` | 批量导入账号（支持 JSON / Sub / 纯文本） |
| `/api/mihomo/proxies` | `GET` | 获取当前 Mihomo 代理池节点与延迟状态 |

---

## 🌐 English Overview

**ChatGPT-Account-Pool** is a production-grade, high-availability ChatGPT account pool and token maintenance platform designed for developers and LLM gateways (such as One-API and New-API).

### Key Highlights:
- **Auto 401 Recovery**: Automatically detects expired access tokens, receives email verification codes via Microsoft mailbox integration, and recovers accounts via direct OAuth protocol.
- **Camoufox & Cloudflare Turnstile Bypass**: Built-in anti-detect browser engine capable of automatically detecting and clicking Cloudflare Turnstile checkboxes.
- **Native Mihomo Proxy Pool**: Dual-container architecture with native proxy subscription, slot port mapping, latency testing, and auto-rotation on IP blocks.
- **Universal Import/Export**: Supports JSON, Sub2API, CPA, and plaintext formats.
- **Real-time 2FA**: Live TOTP 6-digit code generator with countdown directly on the web UI.

---

## ⚠️ 免责声明 (Disclaimer)

本项目仅供学术研究、接口测试与自动化运维学习交流使用。请严格遵守 OpenAI 服务条款以及当地法律法规。使用本项目产生的任何后果由使用者自行承担，与项目开发者无关。

---

## 📄 开源协议 (License)

本项目基于 [AGPL-3.0 License](LICENSE) 开源。
