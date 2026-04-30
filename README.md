# 飞书外部签到系统 (H5)

基于飞书开放平台 API 的外部签到系统，支持在浏览器中直接打开，无需登录飞书账号。

## 核心特性

- **多表格联动**：支持多个多维表格，每个表格对应独立的签到页
- **外部访问**：用户扫码即可在外部浏览器完成签到，无需登录飞书
- **API 限流保护**：内置请求限流，避免触发飞书 API 限制
- **Block 插件联动**：飞书多维表格 Block 插件一键生成签到页

---

## 架构说明

### 完整数据流

```
┌─────────────────────────────────────────────────────────────────────┐
│                   飞书客户端环境（自带认证）                            │
│                                                                     │
│  Block 插件                                                         │
│    ├─ bitable SDK◀──── 已认证（无需 App ID/Secret）                   │
│    ├─ 本地字段识别 ──── table.getFieldMetaList()                     │
│    ├─ 本地二维码生成 ── qrcode 库                                    │
│    ├─ 构造签到 URL ──── H5_BASE_URL/?app={bitable_token}            │
│    │                                                               │
│    └─ [可选] 验证后端连通性 ── POST /api/plugin/register ──┐        │
└───────────────────────────────────────────────────────────│────────┘
                                                            │
                                                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    外部服务器 - H5 后端                                │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │  需要飞书 App ID + App Secret（必须配置）                   │      │
│  │                                                           │      │
│  │  接口列表：                                                 │      │
│  │  • POST /api/plugin/register  ── 可选验证接口               │      │
│  │  • POST /api/config           ── 获取表格配置               │      │
│  │  • POST /api/signin           ── ★ 核心签到接口              │      │
│  │  • GET  /health              ── 健康检查                    │      │
│  │                                                           │      │
│  │  认证方式：app_access_token（App ID + Secret → 飞书 Open API） │      │
│  └──────────────────────────────────────────────────────────┘      │
└──────────────────────────────────────────────────────────┬──────────┘
                                                           │
                                                           ▼
                          ┌───────────────────────────────────┐
                          │   飞书 Open API                   │
                          │   open.feishu.cn/open-apis        │
                          │   Authorization: Bearer {token}   │
                          │   需要 bitable_token 指定表格      │
                          └───────────────────────────────────┘
```

### 为什么 H5 后端需要飞书 App 配置？

这是一个常见的架构疑问，明确解释如下：

| 概念 | bitable_token | app_access_token（飞书 App 凭证） |
|------|--------------|--------------------------------|
| 是什么 | 多维表格标识符 | API 认证令牌 |
| 用途 | 告诉 API "操作哪个表格" | 告诉 API "谁在操作" |
| 来源 | Block SDK 获取的 baseId | App ID + App Secret 换取 |
| Block 是否可以提供 | ✅ 可以（`bitable.base.getSelection()`） | ❌ 不可以（Block 运行在沙箱中，无法提取原始 token） |
| H5 是否可以独立获取 | ✅ 从 URL 参数 `?app=xxx` | ✅ 需要自己配置 App ID / Secret |

**一句话结论**：`bitable_token` 是数据标识（操作哪个表格），`app_access_token` 是认证凭证（谁有权限操作）。两者缺一不可。H5 后端必须有自己的飞书 App 配置才能独立调用飞书 API。

### Block 插件的本地优先策略

从 v2.0.0 开始，Block 插件在飞书客户端内使用 `bitable` SDK 直接完成：
- 字段自动识别（读取表格字段元数据）
- 二维码生成（使用 `qrcode` 库）
- 签到 URL 构造

这意味着 **H5 后端的 /api/plugin/register 不再是必需的**——Block 插件可以不依赖 H5 后端就生成完整的签到页。H5 后端主要负责承载实际的签到操作（用户扫码后输入手机号 → 查询/更新飞书表格）。

---

## 快速开始

### 1. 配置飞书应用

1. 打开 [飞书开发者后台](https://open.feishu.cn/app)
2. 创建或选择一个应用
3. 在「凭证与基础信息」中获取 `App ID` 和 `App Secret`
4. 开通「多维表格」权限：
   - `bitable:app` - 访问多维表格
   - `bitable:app:readonly` - 读取权限

### 2. 部署后端

```bash
# 克隆项目
cd feishu-signin-h5

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入飞书 App ID 和 Secret

# 启动服务
python api/main.py
```

### 3. Block 插件生成签到页

在多维表格中添加「H5签到生成器」Block 插件，点击「一键生成签到页」：
- 自动获取当前多维表格的 Token
- 本地完成字段识别和二维码生成
- 生成专属签到 URL 和二维码

---

## 配置说明

### 环境变量 (.env)

| 变量 | 必填 | 说明 | 示例 |
|------|------|------|------|
| `FEISHU_APP_ID` | ✅ | 飞书应用 App ID | `cli_a97ae1cdd3e2dbb4` |
| `FEISHU_APP_SECRET` | ✅ | 飞书应用 Secret | `xxx` |
| `SIGNIN_BASE_URL` | ✅ | 签到页基础 URL（必须 HTTPS） | `https://signin.example.com` |

### 可选配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HOST` | `0.0.0.0` | 服务监听地址 |
| `PORT` | `8000` | 服务端口 |
| `DEBUG` | `false` | 调试模式 |

> ⚠️ `SIGNIN_BASE_URL` 在生产环境必须使用 HTTPS，否则二维码扫描可能失败。

---

## API 接口

### 1. 插件验证（可选）

Block 插件调用，验证 H5 后端到飞书 API 的连通性。

```
POST /api/plugin/register
Content-Type: application/json

{
  "bitable_token": "bascnxxx"    // 多维表格 Token（必填）
}
```

响应：
```json
{
  "success": true,
  "signin_url": "https://signin.example.com/?app=bascnxxx"
}
```

> ⚠️ 此接口为可选。Block 插件可在本地完成所有注册逻辑。此接口仅用于验证 H5 后端是否可以连通飞书 Open API。

### 2. 获取表格配置

H5 签到页面调用，获取多维表格的名称等信息。

```
POST /api/config
Content-Type: application/json

{
  "bitable_token": "bascnxxx"
}
```

响应：
```json
{
  "success": true,
  "table_name": "活动报名表",
  "bitable_token": "bascnxxx",
  "table_id": "tblxxx",
  "fields": {
    "手机号": "fldxxx",
    "姓名": "fldyyy",
    "签到状态": "fldzzz"
  }
}
```

### 3. 签到（核心接口）

```
POST /api/signin
Content-Type: application/json

{
  "phone": "13800138000",
  "bitable_token": "bascnxxx"
}
```

响应：
```json
{
  "status": "success",
  "message": "签到成功，欢迎参会！",
  "name": "张三",
  "seat": "A区-01",
  "record_id": "recxxx"
}
```

可能的 status 值：
| status | 含义 |
|--------|------|
| `success` | 签到成功 |
| `already` | 已签到（请勿重复提交） |
| `not_found` | 未找到报名信息 |
| `error` | 系统错误 |

### 4. 健康检查

```
GET /health
```

响应：
```json
{
  "status": "ok",
  "feishu_connected": true
}
```

---

## 多表格联动

同一个 H5 后端 + 同一个 Block 插件可用于多个多维表格：

| 多维表格 | Token | 对应签到页 | 签到数据写入 |
|---------|-------|-----------|-------------|
| 活动A报名表 | `bascAxxx` | `/?app=bascAxxx` | 表格A |
| 活动B报名表 | `bascBxxx` | `/?app=bascBxxx` | 表格B |
| 活动C报名表 | `bascCxxx` | `/?app=bascCxxx` | 表格C |

### 工作原理

- Block 插件从 `bitable.base.getSelection()` 获取当前表格的 `baseId`（即 `bitable_token`）
- 签到 URL 格式：`SIGNIN_BASE_URL/?app={bitable_token}`
- H5 后端根据 URL 中的 `app` 参数，定位到对应多维表格
- 每次签到操作的目标表格由 URL 参数决定

---

## 频率限制与优化

### 飞书 API 限制

- 每秒最多 60 次请求（同应用同接口）
- 超出限制会返回错误

### 我们的优化措施

1. **Token 缓存**：App Access Token 缓存在内存中，2小时内复用
2. **搜索 API**：优先使用搜索接口而非全量获取记录
3. **请求限流**：每 IP 每分钟最多 300 次请求
4. **降级策略**：搜索 API 失败自动降级到全量获取

---

## 部署方式

### 方式一：自有服务器

```bash
# 开发模式
python api/main.py

# 生产模式（使用 gunicorn）
gunicorn -w 4 -b 0.0.0.0:8000 "api.main:app"
```

### 方式二：Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["python", "api/main.py"]
```

```bash
docker build -t feishu-signin-h5 .
docker run -d -p 8000:8000 --env-file .env feishu-signin-h5
```

### 方式三：阿里云函数计算

1. 将代码打包为 ZIP
2. 创建函数计算服务
3. 配置环境变量
4. 设置触发器为 HTTP 触发

---

## 项目结构

```
feishu-signin-h5/
├── api/
│   └── main.py          # Flask 后端（包含完整架构注释）
├── public/
│   └── index.html       # H5 签到页面
├── .env.example         # 环境变量模板
├── requirements.txt     # Python 依赖
└── README.md            # 本文档
```

---

## 字段自动识别

H5 后端在签到时会自动识别以下字段（不区分大小写），识别逻辑与 Block 插件保持一致：

| 字段关键词 | 说明 | 在签到中的作用 |
|-----------|------|---------------|
| `手机` / `phone` / `tel` | 手机号字段 | ⭐ 核心：用于查找报名记录 |
| `签到状态` / `status` | 签到状态字段 | 更新为"已签到" |
| `签到时间` / `time` | 签到时间字段 | 记录签到时间戳 |
| `姓名` / `name` | 姓名字段 | 显示签到人姓名 |
| `坐席` / `seat` / `座位` | 座位字段 | 显示座位号 |

---

## 与 Block 插件的配合

| 能力 | Block 插件（飞书客户端内） | H5 后端（外部服务器） |
|------|------------------------|---------------------|
| 字段识别 | ✅ `bitable` SDK 本地完成 | ✅ 独立完成（不依赖 Block） |
| 二维码生成 | ✅ `qrcode` 库本地生成 | - |
| 签到 URL 生成 | ✅ 本地拼接 | ✅ 返回正式版本 |
| 签到处理 | - | ✅ 核心功能 |
| 飞书认证 | ✅ 利用飞书环境 | ✅ 独立 App ID/Secret |
| 可选验证 | → 调用 `POST /api/plugin/register` | ✅ 验证连通性 |

---

## 注意事项

1. **App 配置**：H5 后端必须配置 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`，这是调用飞书 Open API 的前置条件
2. **多维表格权限**：确保应用在开发者后台开通了 `bitable:app` 权限
3. **HTTPS**：生产环境必须使用 HTTPS，否则二维码扫描可能失败
4. **字段名称**：字段名称需包含关键词（如"手机"），否则可能无法自动识别
5. **Block 不存在不影响签到**：H5 后端独立运行，即使 Block 插件未安装，已生成的签到链接仍然可用

---

## License

MIT
