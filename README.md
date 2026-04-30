# 飞书多维表格签到 H5

这是一个基于飞书多维表格的外部签到 H5。参会人扫码打开签到页，输入报名手机号；后端验证该手机号是否存在于指定多维表格，验证成功后把对应记录更新为“已签到”，并写入签到时间。未报名时，页面返回“去报名”按钮和报名链接。

## 功能

- 支持多个多维表格复用同一套 H5，签到链接通过 `?app={bitable_token}` 区分数据源。
- 支持可选 `table` 参数精确指定数据表：`/?app={bitable_token}&table={table_id}`。
- 自动识别手机号、姓名、座位、签到状态、签到时间等字段。
- 手机号记录使用内存索引缓存，降低飞书 Open API 调用量。
- 同一手机号并发签到加锁，避免重复提交。
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

生产环境启动时，如果未配置 `FEISHU_APP_ID` 或 `FEISHU_APP_SECRET`，服务会直接失败退出，避免以错误配置上线。

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

## 生产部署

推荐使用 gunicorn，并放在 HTTPS 反向代理后面：

```bash
gunicorn -w 4 -b 0.0.0.0:8000 "api.main:app"
```

Nginx 反向代理示例：

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

生产检查项：

- `DEBUG=false`。
- `SIGNIN_BASE_URL` 使用 HTTPS 公网域名。
- `.env` 不提交到仓库。
- 反向代理或平台侧配置 HTTPS 证书。
- 日志接入平台日志系统，便于排查飞书 API 写入失败。
- 多实例部署时，当前内存缓存和手机号锁不会跨进程共享；高并发场景建议替换为 Redis。

## API

### `GET /health`

健康检查，并尝试获取飞书 `app_access_token`。

响应：

```json
{
  "status": "ok",
  "feishu_connected": true
}
```

### `POST /api/config`

H5 初始化时获取表格配置。

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

Block 插件可选调用，用于验证后端可访问目标多维表格，并缓存插件传入的配置。

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
- 表格字段配置缓存 1 小时。
- 手机号到记录列表的索引缓存 10 分钟。
- 同一进程内，同一个 `bitable_token + phone` 会加锁处理，避免同一手机号重复并发签到。
- 当前缓存和锁都是进程内实现。多进程或多节点部署时，每个 worker 有自己的缓存和锁；如活动规模较大，建议把缓存、限流和幂等控制迁移到 Redis。

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
- 大型活动不建议只依赖内存缓存；使用 Redis 后可以统一限流、缓存和幂等锁。

## License

MIT
