# 飞书多维表格签到 H5

这是一个基于飞书多维表格的外部签到 H5。参会人扫码打开签到页，输入报名手机号；后端验证该手机号是否存在于指定多维表格，验证成功后把对应记录更新为“已签到”，并写入签到时间。未报名时，页面返回“去报名”按钮和报名链接。

## 功能

- 支持多个多维表格复用同一套 H5，签到链接通过 `?app={bitable_token}` 区分数据源。
- 支持可选 `table` 参数精确指定数据表：`/?app={bitable_token}&table={table_id}`。
- 自动识别手机号、姓名、座位、签到状态、签到时间等字段。
- 手机号记录使用 Redis 或内存索引缓存，降低飞书 Open API 调用量。
- 支持 Redis 分布式锁，多个 gunicorn worker 下也能避免同一手机号重复并发签到。
- 同一手机号命中多条报名记录时，H5 会让用户选择本人后再签到。
- 未报名时返回报名链接；链接可由 Block 插件传入，也可尝试从表单视图中自动检测。
- H5 可外部浏览器访问，不要求参会人登录飞书。

## 系统架构

```text
参会人浏览器
  |
  |  GET /?app=xxx&table=yyy
  v
H5 静态页面
  |
  |  POST /api/config
  |  POST /api/signin
  v
Flask 后端
  |
  |  App ID + App Secret 换取 app_access_token
  |  读取/更新 bitable 记录
  v
飞书 Open API
```

`bitable_token` 只表示“操作哪个多维表格”，不是认证凭证。后端必须配置飞书应用的 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`，用它们换取 `app_access_token` 后才能调用飞书 Open API。

## 表格要求

目标数据表至少需要一个手机号字段。推荐字段如下：

| 字段 | 推荐名称 | 说明 |
| --- | --- | --- |
| 手机号 | `手机号` / `phone` / `tel` | 必填，用于查找报名记录 |
| 签到状态 | `签到状态` / `status` | 可选，签到成功后写入 `已签到` |
| 签到时间 | `签到时间` / `time` | 可选，签到成功后写入毫秒时间戳 |
| 姓名 | `姓名` / `name` | 可选，成功页展示 |
| 座位 | `坐席` / `座位` / `seat` | 可选，成功页展示 |

如果没有签到状态或签到时间字段，后端仍可返回签到成功，但不会写入缺失字段。生产使用建议显式创建这两个字段。

同一个手机号允许多人报名。签到时如果手机号命中多条记录，接口会返回候选参会人列表，H5 展示姓名和座位供用户选择；用户选择后，前端带 `record_id` 二次提交，后端只更新被选中的那一条记录。

## 环境变量

复制 `.env.example` 为 `.env`，按环境填写：

```bash
cp .env.example .env
```

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `FEISHU_APP_ID` | 是 | 空 | 飞书应用 App ID |
| `FEISHU_APP_SECRET` | 是 | 空 | 飞书应用 App Secret |
| `SIGNIN_BASE_URL` | 生产必填 | 空 | 对外访问域名，例如 `https://signin.example.com` |
| `HOST` | 否 | `0.0.0.0` | Flask 监听地址 |
| `PORT` | 否 | `8000` | 服务端口 |
| `DEBUG` | 否 | `false` | 调试模式；生产必须为 `false` |
| `ALLOWED_ORIGINS` | 否 | 空 | CORS 白名单，H5 与 API 同域部署时留空 |
| `CACHE_BACKEND` | 否 | `auto` | `auto` / `redis` / `memory`；生产建议 `redis` |
| `REDIS_URL` | 生产建议 | 空 | Redis 连接，例如 `redis://:password@127.0.0.1:6379/0` |
| `CONFIG_CACHE_TTL` | 否 | `21600` | 表格配置缓存秒数，默认 6 小时 |
| `RECORD_CACHE_TTL` | 否 | `21600` | 手机号索引缓存秒数，默认 6 小时 |
| `MISS_REFRESH_COOLDOWN` | 否 | `60` | 手机号未命中后的强制刷新冷却秒数 |

生产环境启动时，如果未配置 `FEISHU_APP_ID` 或 `FEISHU_APP_SECRET`，服务会直接失败退出，避免以错误配置上线。

`CACHE_BACKEND=auto` 时，如果配置了可连接的 `REDIS_URL` 就使用 Redis；未配置或连接失败会回退内存缓存。生产环境建议设置 `CACHE_BACKEND=redis`，这样 Redis 连接失败会直接启动失败，避免误以为已经启用共享缓存。

## 飞书应用权限

在飞书开发者后台创建企业自建应用，并确保：

- 应用凭证中的 App ID / App Secret 已配置到后端环境变量。
- 应用拥有多维表格读写权限，例如 `bitable:app`。
- 该应用对目标多维表格有访问权限。

## 本地运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python api/main.py
```

打开：

```text
http://127.0.0.1:8000/?app={bitable_token}&table={table_id}
```

`table` 可省略。省略时后端会按表名、字段名自动查找签到数据表。

## 宝塔面板生产部署

以下步骤假设当前非 Redis 版本已经能在宝塔上正常运行，只升级为“Redis 缓存 + Redis 分布式锁 + 同步飞书写入”。

### 1. 安装 Redis

1. 登录宝塔面板。
2. 打开「软件商店」。
3. 搜索并安装「Redis」。
4. 安装完成后进入 Redis 设置页，确认状态为「运行中」。
5. 建议在 Redis 设置中配置密码。

Redis 只需要作为缓存和锁使用，不需要建数据库、不需要建表。

### 2. 确认 Redis 连接信息

如果应用和 Redis 在同一台服务器，通常使用：

```text
redis://:你的Redis密码@127.0.0.1:6379/0
```

如果 Redis 没有设置密码：

```text
redis://127.0.0.1:6379/0
```

生产建议设置密码，并且不要把 Redis 端口开放到公网。

### 3. 更新项目代码

在宝塔「终端」中进入项目目录：

```bash
cd /www/wwwroot/你的项目目录
```

如果使用 git 管理代码：

```bash
git pull
```

如果是手动上传代码，上传后确认 `api/main.py`、`requirements.txt`、`README.md` 已更新。

### 4. 安装新增依赖

进入项目虚拟环境后安装依赖。路径按你当前宝塔项目实际环境调整：

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

如果你之前没有使用虚拟环境，也可以在当前 Python 环境中执行：

```bash
pip install -r requirements.txt
```

本次新增依赖是 `redis==4.3.6`。

### 5. 修改 `.env`

在项目根目录 `.env` 中增加或修改：

```env
DEBUG=false
CACHE_BACKEND=redis
REDIS_URL=redis://:你的Redis密码@127.0.0.1:6379/0
CONFIG_CACHE_TTL=21600
RECORD_CACHE_TTL=21600
MISS_REFRESH_COOLDOWN=60
```

完整生产示例：

```env
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
SIGNIN_BASE_URL=https://signin.example.com
HOST=0.0.0.0
PORT=8000
DEBUG=false
ALLOWED_ORIGINS=
CACHE_BACKEND=redis
REDIS_URL=redis://:你的Redis密码@127.0.0.1:6379/0
CONFIG_CACHE_TTL=21600
RECORD_CACHE_TTL=21600
MISS_REFRESH_COOLDOWN=60
```

### 6. 重启 Python 项目

如果使用宝塔「Python 项目管理器」：

1. 打开「Python 项目管理器」。
2. 找到该签到项目。
3. 点击「重启」。
4. 查看项目日志，确认没有 Redis 连接错误。

如果你使用 supervisor 或 systemd 管理 gunicorn，则重启对应服务。

gunicorn 推荐命令：

推荐使用 gunicorn，并放在 HTTPS 反向代理后面：

```bash
gunicorn -w 4 -b 0.0.0.0:8000 "api.main:app"
```

### 7. 配置宝塔网站反向代理

在宝塔「网站」中绑定你的域名，例如 `signin.example.com`，并配置反向代理到 Python 服务端口。

Nginx 配置示例：

```nginx
server {
    listen 443 ssl http2;
    server_name signin.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

宝塔可视化操作路径通常是：

1. 网站列表选择域名。
2. 打开「反向代理」。
3. 添加代理，目标 URL 填 `http://127.0.0.1:8000`。
4. 打开「SSL」，申请并启用 HTTPS 证书。
5. 开启强制 HTTPS。

### 8. 验证 Redis 已生效

访问健康检查：

```bash
curl https://signin.example.com/health
```

返回中应包含：

```json
{
  "cache_backend": "redis",
  "feishu_connected": true
}
```

活动开始前主动预热缓存。这个操作不是在飞书里新增或修改记录，而是让 H5 后端提前读取报名表，把「手机号 → 报名记录」索引写入 Redis。这样现场第一个用户签到时不用等待后端全量拉取报名表。

```bash
curl -X POST https://signin.example.com/api/cache/preload \
  -H 'Content-Type: application/json' \
  -d '{"bitable_token":"bascnxxx","table_id":"tblxxx"}'
```

返回 `success: true` 后，后台会开始把报名记录拉到 Redis 索引中。`started: true` 表示本次已启动预热任务；`already_cached: true` 表示缓存已经存在，不需要重复预热。

建议在活动开始前 5-15 分钟执行一次。如果报名表还在持续新增，现场签到时缓存未命中的手机号仍会通过飞书 `records/search` 精确补查，不需要反复手动预热。

生产检查项：

- `DEBUG=false`。
- `CACHE_BACKEND=redis`。
- `/health` 返回 `cache_backend: redis`。
- `SIGNIN_BASE_URL` 使用 HTTPS 公网域名。
- `.env` 不提交到仓库。
- 反向代理或平台侧配置 HTTPS 证书。
- 日志接入平台日志系统，便于排查飞书 API 写入失败。
- Redis 只监听内网或本机，不向公网开放 6379 端口。

## API

### `GET /health`

健康检查，并尝试获取飞书 `app_access_token`。

响应：

```json
{
  "status": "ok",
  "feishu_connected": true,
  "cache_backend": "redis"
}
```

### `POST /api/config`

H5 初始化时获取表格配置。接口返回配置后，会在后台异步预热手机号索引缓存，不阻塞页面加载。

请求：

```json
{
  "bitable_token": "bascnxxx",
  "table_id": "tblxxx"
}
```

响应：

```json
{
  "success": true,
  "cached": false,
  "bitable_token": "bascnxxx",
  "table_id": "tblxxx",
  "table_name": "活动报名表",
  "fields": {
    "手机号": "fldxxx",
    "姓名": "fldyyy",
    "签到状态": "fldzzz"
  },
  "register_form_url": "https://..."
}
```

### `POST /api/cache/preload`

主动预热手机号索引缓存。活动开始前调用一次，可以避免首个签到用户承担全量拉取报名记录的耗时。

具体来说，这是一个给后端调用的 HTTP 接口。它会根据 `bitable_token` 和 `table_id` 读取飞书多维表格中的报名记录，提取手机号字段，构建 Redis/内存缓存索引。接口不会修改飞书表格里的报名、签到状态或签到时间。

请求：

```json
{
  "bitable_token": "bascnxxx",
  "table_id": "tblxxx"
}
```

响应：

```json
{
  "success": true,
  "started": true,
  "already_cached": false,
  "table_id": "tblxxx"
}
```

`table_id` 可省略，省略时后端会自动查找签到数据表。该接口只启动后台预热任务，不等待全部记录拉取完成。

字段说明：

- `bitable_token`：多维表格 token，对应签到链接里的 `?app=...`。
- `table_id`：数据表 ID，对应签到链接里的 `&table=...`；生产建议传入，避免自动识别选错表。
- `started`：是否启动了新的后台预热任务。
- `already_cached`：是否已经存在缓存；为 `true` 时说明无需重复预热。

生产执行示例：

```bash
curl -X POST https://signin.example.com/api/cache/preload \
  -H 'Content-Type: application/json' \
  -d '{"bitable_token":"bascnxxx","table_id":"tblxxx"}'
```

建议在活动开始前 5-15 分钟执行一次。若报名仍在持续新增，不需要频繁调用；签到接口在缓存未命中时会按手机号向飞书精确补查并合并缓存。

### `POST /api/signin`

核心签到接口。

请求：

```json
{
  "phone": "13800138000",
  "bitable_token": "bascnxxx",
  "table_id": "tblxxx",
  "record_id": "recxxx"
}
```

`record_id` 只在同一手机号命中多条记录、用户选择本人后需要传入。

成功响应：

```json
{
  "status": "success",
  "message": "签到成功，欢迎参会！",
  "name": "张三",
  "seat": "A区-01",
  "record_id": "recxxx"
}
```

同一手机号命中多条记录时：

```json
{
  "status": "multiple",
  "message": "该手机号关联了多位参会人，请选择本人完成签到",
  "candidates": [
    {
      "record_id": "recxxx",
      "name": "张三",
      "seat": "A区-01",
      "signin_status": ""
    },
    {
      "record_id": "recyyy",
      "name": "李四",
      "seat": "B区-08",
      "signin_status": "已签到"
    }
  ]
}
```

常见 `status`：

| status | 含义 |
| --- | --- |
| `success` | 签到成功，飞书记录已更新 |
| `already` | 报名记录已是 `已签到` |
| `multiple` | 手机号命中多条报名记录，需要选择本人 |
| `not_found` | 未找到报名手机号 |
| `error` | 系统错误或飞书 API 调用失败 |

### `POST /api/plugin/register`

Block 插件可选调用，用于验证后端可访问目标多维表格，并缓存插件传入的配置。如果传入 `table_id`，接口会异步预热手机号索引缓存。

请求：

```json
{
  "bitable_token": "bascnxxx",
  "table_id": "tblxxx",
  "register_form_url": "https://...",
  "config": {
    "update_signin_status": true,
    "update_signin_time": true,
    "return_name": true,
    "return_seat": true,
    "success_message": "签到成功，欢迎参会！",
    "already_message": "已签到，无需重复签到"
  }
}
```

响应：

```json
{
  "success": true,
  "signin_url": "https://signin.example.com/?app=bascnxxx&table=tblxxx"
}
```

## 缓存与并发

- `app_access_token` 在进程内缓存，过期前自动复用。
- 表格字段配置默认缓存 6 小时；配置 Redis 后多个 worker 共享。
- 手机号到记录列表的索引默认缓存 6 小时；配置 Redis 后多个 worker 共享。
- 打开签到页触发 `/api/config` 后，后端会自动后台预热手机号索引。
- 活动开始前可以主动调用 `/api/cache/preload` 预热，降低首个签到请求耗时。
- 签到时如果手机号命中缓存，直接使用缓存，不再定时每 10 分钟重新拉取。
- 签到时如果手机号未命中缓存，会先通过飞书 `records/search` 按手机号精确补查，并把查到的新报名记录局部合并进缓存。
- 只有缓存为空且精确补查仍未命中时，才会立即全量拉取一次建立索引；后续全量刷新受 `MISS_REFRESH_COOLDOWN` 控制。
- 手机号索引缓存按 `bitable_token + table_id` 隔离，同一个多维表格下多张活动表不会串缓存。
- 飞书 API 出现短暂 `429` 或 `5xx` 时会按 `FEISHU_API_MAX_RETRIES` 和 `FEISHU_API_RETRY_BASE_DELAY` 做有限重试。
- 同一个 `bitable_token + phone` 会加锁处理，配置 Redis 后该锁跨 gunicorn worker 生效。
- 预热手机号索引时也使用锁，配置 Redis 后多个 worker 不会同时全量拉取同一张表。
- 未配置 Redis 时会回退到内存缓存和进程内锁，只适合单 worker 或本地开发。
- 本版本仍然同步写入飞书，用户看到签到成功时，飞书表格已经更新成功。

## 安全说明

- 生产环境默认不启用全域 CORS；同域部署无需配置 `ALLOWED_ORIGINS`。
- `DEBUG=false` 时，`/api/cache/status` 调试接口不可用。
- 前端会转义接口返回的姓名、座位、消息和报名链接，避免把表格内容直接作为 HTML 注入页面。
- 签到成功响应会在飞书记录写入完成后返回，避免用户看到成功但表格未落库。

## 项目结构

```text
feishu-signin-h5/
├── api/
│   └── main.py
├── public/
│   └── index.html
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## 维护建议

- 依赖版本已固定在 `requirements.txt`，升级前先在测试表格验证完整签到流程。
- 当前没有自动化测试；后续建议补充字段识别、手机号匹配、重复签到、飞书 API 失败等用例。
- 大型活动建议使用 Redis，并在活动开始前调用 `/api/cache/preload` 预热缓存。

## License

MIT
