# FreeGPT-Manager

<div align="center">

**现代化、全自动化的 ChatGPT / OpenAI 账号全生命周期管理与自动化运营平台**

[![Release](https://img.shields.io/badge/release-v1.2.0-emerald.svg)](https://github.com/SeiShonagon520/FreeGPT-Manager)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-brightgreen.svg)](https://www.python.org/)
[![React](https://img.shields.io/badge/frontend-React%2018%20%7C%20Vite-61dafb.svg)](frontend/)

</div>

---

## 📖 简介

**FreeGPT-Manager** 是一个专为本地私有化部署打造的高性能 OpenAI / ChatGPT 账号矩阵管理、注册裂变、存活监测与自动化复活平台。项目采用 **FastAPI 后端 + React 18 / Vite 现代化前端** 架构，将协议注册、浏览器自动化、密码与 2FA 管理、401 故障自愈、微软母体邮箱池以及动态代理调度深度串联，打造出一套可观察、可停止、可复用的自动化流水线。

> 本项目基于开源项目 [aBaiFreeGPT](https://github.com/asz798838958/aBaiFreeGPT) 进行了大量生产级扩展与体验重构，致力于提供更稳定、更高效的账号资产池化与分发能力。

---

## ✨ 核心特性

### 1. ⚡ 401 深度验活与自动故障自愈 (Auto Revive)
- **多维度探活**：通过 Camoufox 浏览器或官方 API 接口并发检测 Access Token (AT) 与 Refresh Token (RT) 存活状态。
- **自动收信复活**：针对 401 失活账号，系统自动调度关联的微软母体邮箱，异步收取最新邮箱验证码，通过协议重登并刷回全新的有效 AT/RT，全自动化救号。
- **专属 SUB 导出**：支持对本次 401 恢复成功的账号进行一键专属导出，标准 Sub2API 格式直接对接聚合分发客户端，无需人工繁琐筛选。

### 2. 🗂️ 账号多选与批量运维操作
- **灵活多选**：支持列表表头一键全选及跨行单选 Checkbox。
- **浮动操作栏**：选中账号后自动唤起批量工具栏，支持一键批量删除失效账号，或按选定范围批量导出为 JSON、CSV、Sub2API、CPA、Any2API 等多种标准格式。

### 3. ⏳ AT 访问令牌实时到期倒计时
- 状态栏实时计算并直观展示 Access Token 剩余存活时长。
- 多级色彩视觉预警：`绿色（充裕）` $\rightarrow$ `黄色/橙色（即将到期）` $\rightarrow$ `红色（已过期）`，方便及时预警并安排刷新。

### 4. 📬 微软母体邮箱池与裂变用量监控
- **⚡ 一键批量测活**：支持对邮箱池内所有微软母体邮箱（Outlook / Hotmail）发起高并发 OAuth 探活，自动检测凭据与 Refresh Token 有效性。
- **失效邮箱自动隔离**：测活失败或鉴权失效的邮箱自动置为「已隔离」并禁用，彻底防止裂变注册任务因死邮箱中断。
- **📊 裂变用量与耗尽率仪表盘**：实时统计母体总数、剩余可用裂变次数、已用/总限额比例与耗尽率，并在配额告急时醒目预警。
- **一键清理**：支持一键剔除并清理已失效或已耗尽的废弃邮箱。

### 5. 🤖 全能注册与安全凭据体系
- **多种注册模式**：支持纯 HTTP 协议极速注册、Camoufox 有头浏览器注册（支持 VNC 调试）以及无头并发注册。
- **密码 + TOTP 2FA**：自动设置强密码并绑定激活 TOTP 2FA，账号凭据、密码及 TOTP Secret 全加密保存。
- **验证码集成**：支持远程打码服务、本地验证码 Solver 或人工介入。

### 6. 🌐 动态代理池与脉冲调度
- 内置 Mihomo (Clash) 订阅节点同步管理，实时监控节点延迟与存活状态。
- 支持动态 IP 提取 API 与本机直连。
- 支持健康节点分波并发脉冲调度，异常节点自动熔断并延时探测恢复。

### 7. 🛡️ 前端抗强缓存与版本追踪
- 前端静态构建资源全面引入指纹哈希，SPA 路由严密配置 `Cache-Control: no-cache, no-store, must-revalidate`，杜绝更新后浏览器磁盘强缓存加载旧脚本的问题。
- 侧边栏与系统设置页常驻版本状态与构建信息。

### 8. 🔐 账号数据库一键加密快照与跨机迁移
- **一键打包**：使用 PBKDF2 (100,000次散列) 与 XSalsa20-Poly1305 认证加密，打包 SQLite 事务快照、元数据清单以及**微软邮箱解密主密钥（.microsoft_mailbox.key）**生成 `.fgmbak` 文件。
- **免写预检与跨机恢复**：支持恢复前在内存中预检账号与邮箱数量分布，确认后一键覆盖恢复；新设备直接无损解密全部敏感凭据。
- **本地快照防误删**：支持即时生成本地快照及恢复前自动快照备份，提供一键回退与下载能力。

---

## 🛠️ 便捷脚本支持

根目录内置了多套一键运维脚本，开箱即用：

| 脚本文件 | 说明 |
| :--- | :--- |
| `start.bat` | **本地极速启动**：自动激活虚拟环境并启动 Uvicorn 后端服务 |
| `start_docker.bat` | **Docker 启动**：一键启动 Docker Compose 容器集群 |
| `backup.bat` | **安全备份**：一键将本地最新代码与提交同步推送至 GitHub 个人仓库 |
| `rollback.bat` | **安全回退**：发生异常时交互式快速回退至稳定历史版本 |

---

## 🚀 快速上手

### 环境要求
- **Python** 3.11+
- **Node.js** 18+ 与 npm
- **Chromium / Camoufox** 依赖（使用浏览器模式注册或验活时需要）
- 可选本地代理软件（如 Clash / FlClash / Mihomo 等）

### 1. 本地部署运行

```bash
# 1. 克隆代码仓库
git clone https://github.com/SeiShonagon520/FreeGPT-Manager.git
cd FreeGPT-Manager

# 2. 创建并激活 Python 虚拟环境
python -m venv .venv
# Windows:
.\.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

# 3. 安装后端依赖
pip install -r requirements.txt

# 4. 构建前端产物
cd frontend
npm ci
npm run build
cd ..

# 5. 配置环境变量
cp .env.example .env
# 编辑 .env 设置 APP_PASSWORD

# 6. 启动后端服务
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

启动后在浏览器打开 `http://127.0.0.1:8000` 即可进入管理面板。

---

### 2. Docker 部署

```bash
# 启动所有容器服务
docker compose up -d --build
```

---

## 📁 目录结构

```text
FreeGPT-Manager/
├── api/                    # FastAPI 路由层（账号、任务、系统配置、邮箱、代理池）
├── application/            # 核心业务应用编排与服务
├── core/                   # 数据库 ORM、加密组件、通用工具类
├── infrastructure/         # 数据持久化 Repository 与 Provider 实现
├── platforms/chatgpt/      # 协议注册、Camoufox 浏览器注册、401 恢复与 2FA
├── frontend/               # React 18 + Vite + Tailwind CSS 前端工程
│   ├── src/pages/          # 账号管理、微软邮箱池、任务中心、设置等页面
│   └── src/components/     # 批量操作栏、状态徽标、倒计时组件等
├── static/                 # 前端编译生成的静态资源目录
├── tests/                  # 自动化端到端测试与单元测试套件
├── docs/                   # 深度配置与架构文档
├── start.bat               # 本地快速启动脚本
├── backup.bat              # 代码一键备份脚本
└── docker-compose.yml      # Docker 编排文件
```

---

## 🧪 自动化测试

项目内置完善的自动化测试，涵盖账号生命周期、邮箱池并发测活、401 恢复流及导出等核心场景：

```bash
# 运行全部后端测试
pytest -q
```

---

## 🤝 鸣谢与致敬 (Credits)

- 感谢上游项目 [aBaiFreeGPT](https://github.com/asz798838958/aBaiFreeGPT) 及其作者所做出的开创性工作与开源贡献。
- 感谢 [Camoufox](https://github.com/daijro/camoufox) 提供的强反指纹反检测浏览器内核。

---

## 📄 开源许可证

本项目遵循 [GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE) 协议开源。
