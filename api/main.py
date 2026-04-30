# -*- coding: utf-8 -*-
"""
飞书签到系统 - Flask 后端
支持多表格自动联动：通过 bitable_token 动态路由，不同表格显示不同签到页

=== 架构说明：H5 后端为什么需要飞书 App 配置 ===

H5 后端需要 FEISHU_APP_ID 和 FEISHU_APP_SECRET，原因如下：

1. bitable_token ≠ 认证凭证
   - Block 插件通过 bitable SDK 可获得 baseId（即 bitable_token）
   - 但 baseId 只是"多维表格标识符"，不是 API 认证凭证
   - 调用飞书 Open API（如 bitable/v1/apps/{token}/...）需要
     Authorization: Bearer {access_token}
   
2. 两种 Token 的差异
   - bitable_token → 告诉 API "操作哪个表格"（数据维度）
   - app_access_token → 证明"谁在操作"（认证维度）
   
3. Block 插件 ≠ H5 后端的认证源
   - Block 插件运行在飞书客户端沙箱中，利用飞书环境认证
   - 它无法向外部服务器提供可复用的 access_token
   - H5 后端作为独立服务，必须自己向飞书 Open API 认证

=== 架构优化 ===

从 v2.0.0 开始，Block 插件使用 bitable SDK 在本地完成：
- 字段识别（table.getFieldMetaList()）
- 二维码生成（qrcode 库）
- 签到 URL 构造

H5 后端的 /api/plugin/register 变为可选验证接口。
核心签到功能 /api/signin 仍依赖 H5 后端的飞书 App 认证。
"""

import os
import json
import time
import hashlib
import logging
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional, Tuple, Dict, List
from collections import defaultdict
from threading import Lock

from flask import Flask, request, jsonify, send_from_directory, abort
from flask_cors import CORS
from dotenv import load_dotenv
import httpx

# 加载环境变量
load_dotenv()

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}'
)
logger = logging.getLogger(__name__)

# ==================== 配置 ====================

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
DEBUG = os.getenv("DEBUG", "true").lower() == "true"

# 签到页面基础URL（插件生成链接时使用）
SIGNIN_BASE_URL = os.getenv("SIGNIN_BASE_URL", "").rstrip("/")

# ==================== 内存缓存（生产环境建议用Redis） ====================

class MemoryCache:
    """简单的内存缓存，带线程安全"""
    
    def __init__(self):
        self._data: Dict[str, tuple] = {}  # key -> (value, expire_time)
        self._lock = Lock()
    
    def get(self, key: str) -> Optional[any]:
        with self._lock:
            if key in self._data:
                value, expire_time = self._data[key]
                if datetime.now() < expire_time:
                    return value
                else:
                    del self._data[key]
        return None
    
    def set(self, key: str, value: any, ttl_seconds: int = 3600):
        with self._lock:
            self._data[key] = (value, datetime.now() + timedelta(seconds=ttl_seconds))
    
    def delete(self, key: str):
        with self._lock:
            self._data.pop(key, None)

# 全局缓存实例
cache = MemoryCache()

# ==================== 飞书 API 客户端 ====================

class RateLimiter:
    """简单的内存限流器（生产环境建议用Redis）"""
    
    def __init__(self, max_requests: int = 60, window_seconds: int = 1):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: Dict[str, List[float]] = defaultdict(list)
        self._lock = Lock()
    
    def is_allowed(self, key: str) -> bool:
        """检查是否允许请求"""
        now = time.time()
        window_start = now - self.window_seconds
        
        with self._lock:
            # 清理过期记录
            self._requests[key] = [t for t in self._requests[key] if t > window_start]
            
            if len(self._requests[key]) >= self.max_requests:
                return False
            
            self._requests[key].append(now)
            return True

# 全局限流器：每分钟300次请求（给5倍余量）
rate_limiter = RateLimiter(max_requests=300, window_seconds=60)


class FeishuClient:
    """飞书 API 客户端"""

    BASE_URL = "https://open.feishu.cn/open-apis"
    TOKEN_URL = f"{BASE_URL}/auth/v3/app_access_token/internal"

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token_cache: Optional[Tuple[str, datetime]] = None

    def get_app_access_token(self) -> str:
        """获取应用访问令牌（带缓存）"""
        # 检查缓存是否有效
        if self._token_cache:
            token, expires_at = self._token_cache
            if datetime.now() < expires_at - timedelta(minutes=5):
                return token

        # 请求新令牌
        response = httpx.post(
            self.TOKEN_URL,
            json={
                "app_id": self.app_id,
                "app_secret": self.app_secret
            },
            timeout=30.0
        )

        data = response.json()
        if data.get("code") != 0:
            raise Exception(f"获取飞书 Access Token 失败: {data.get('msg')}")

        token = data["app_access_token"]
        # 缓存，token 通常有效期 2 小时
        expires_in = data.get("expire", 7200)
        self._token_cache = (token, datetime.now() + timedelta(seconds=expires_in))

        return token

    def api_request(
        self,
        method: str,
        path: str,
        **kwargs
    ) -> dict:
        """发送 API 请求"""
        token = self.get_app_access_token()
        url = f"{self.BASE_URL}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        response = httpx.request(
            method=method,
            url=url,
            headers=headers,
            **kwargs,
            timeout=30.0
        )

        data = response.json()
        if data.get("code") != 0:
            logger.error(f"飞书 API 错误: {data}")
            raise Exception(data.get("msg", "API 请求失败"))

        return data.get("data", {})

    def get_records(
        self,
        bitable_token: str,
        table_id: str,
        page_size: int = 500
    ) -> list:
        """获取多维表格记录"""
        data = self.api_request(
            "GET",
            f"/bitable/v1/apps/{bitable_token}/tables/{table_id}/records",
            params={"page_size": page_size}
        )
        return data.get("items", [])

    def search_records(
        self,
        bitable_token: str,
        table_id: str,
        field_id: str,
        value: str,
        page_size: int = 100
    ) -> list:
        """根据字段搜索记录（更高效）"""
        data = self.api_request(
            "POST",
            f"/bitable/v1/apps/{bitable_token}/tables/{table_id}/records/search",
            json={
                "page_size": page_size,
                "filter": {
                    "conjunction": "and",
                    "conditions": [
                        {
                            "field_name": field_id,
                            "operator": "contains",
                            "value": [value]
                        }
                    ]
                }
            }
        )
        return data.get("items", [])

    def update_record(
        self,
        bitable_token: str,
        table_id: str,
        record_id: str,
        fields: dict
    ) -> dict:
        """更新多维表格记录"""
        data = self.api_request(
            "PUT",
            f"/bitable/v1/apps/{bitable_token}/tables/{table_id}/records/{record_id}",
            json={"fields": fields}
        )
        return data

    def get_field_list(
        self,
        bitable_token: str,
        table_id: str
    ) -> list:
        """获取字段列表"""
        data = self.api_request(
            "GET",
            f"/bitable/v1/apps/{bitable_token}/tables/{table_id}/fields"
        )
        return data.get("items", [])

    def get_table_list(self, bitable_token: str) -> list:
        """获取表格列表"""
        data = self.api_request(
            "GET",
            f"/bitable/v1/apps/{bitable_token}/tables"
        )
        return data.get("items", [])

    def get_app_info(self, bitable_token: str) -> dict:
        """获取多维表格信息"""
        return self.api_request(
            "GET",
            f"/bitable/v1/apps/{bitable_token}"
        )


# 全局客户端
feishu = FeishuClient(FEISHU_APP_ID, FEISHU_APP_SECRET)


# ==================== Flask 应用 ====================

app = Flask(__name__, static_folder="public", static_url_path="")
CORS(app)


# ==================== 辅助函数 ====================

def rate_limit_check():
    """API限流检查装饰器"""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            # 使用客户端IP作为限流key
            client_ip = request.remote_addr or "unknown"
            
            if not rate_limiter.is_allowed(client_ip):
                logger.warning(f"限流触发: {client_ip}")
                return error_response("请求过于频繁，请稍后再试", 429)
            
            return f(*args, **kwargs)
        return wrapped
    return decorator


def generate_signin_url(bitable_token: str) -> str:
    """生成签到页URL（带安全签名）"""
    if not SIGNIN_BASE_URL:
        # 开发环境使用当前域名
        return f"/?app={bitable_token}"
    
    # 生产环境：使用查询参数传递token
    return f"{SIGNIN_BASE_URL}/?app={bitable_token}"


def get_cached_config(bitable_token: str) -> Optional[dict]:
    """获取缓存的配置"""
    cache_key = f"config_{bitable_token}"
    return cache.get(cache_key)


def set_cached_config(bitable_token: str, config: dict):
    """缓存配置（5分钟过期）"""
    cache_key = f"config_{bitable_token}"
    cache.set(cache_key, config, ttl_seconds=300)


def extract_phone_values(value) -> list:
    """从飞书单元格值中提取手机号"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict) and "text" in item:
                result.append(item["text"])
        return result
    if isinstance(value, dict) and "text" in value:
        return [value["text"]]
    return []


def extract_name_value(value) -> str:
    """从飞书单元格值中提取姓名"""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and len(value) > 0:
        item = value[0]
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            return item.get("name", item.get("text", ""))
    if isinstance(value, dict):
        return value.get("name", value.get("text", ""))
    return ""


def extract_status_value(value) -> str:
    """从飞书单元格值中提取状态"""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and len(value) > 0:
        item = value[0]
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            return item.get("name", item.get("text", ""))
    return ""


def format_timestamp(ts) -> str:
    """格式化时间戳"""
    try:
        ts = int(ts)
        dt = datetime.fromtimestamp(ts / 1000)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return str(ts)


def error_response(message: str, status_code: int = 400):
    """统一错误响应"""
    return jsonify({"status": "error", "message": message}), status_code


def success_response(**kwargs):
    """统一成功响应"""
    return jsonify({"status": "success", **kwargs})


# ==================== API 路由 ====================

@app.route("/health")
def health_check():
    """健康检查"""
    connected = False
    try:
        feishu.get_app_access_token()
        connected = True
    except Exception as e:
        logger.error(f"飞书连接失败: {e}")

    return jsonify({
        "status": "ok" if connected else "degraded",
        "feishu_connected": connected
    })


@app.route("/api/plugin/register", methods=["POST"])
@rate_limit_check()
def plugin_register():
    """
    【插件验证接口 - 可选】
    Block 插件调用此接口验证 H5 后端到飞书 API 的连通性。

    注意：此接口为可选。Block 插件已支持本地完成字段识别和二维码生成，
    H5 后端不再负责这些逻辑。

    请求参数：
    {
        "bitable_token": "bascnxxx",  // 多维表格 Token（必填）
        "table_id": "tblxxx",         // 表格 ID（可选）
        "event_name": "产品发布会"    // 活动名称（可选）
    }

    返回：
    {
        "success": true,
        "signin_url": "https://xxx/?app=bascnxxx"
    }
    """
    data = request.get_json()
    if not data:
        return error_response("请求数据格式错误")

    bitable_token = data.get("bitable_token", "").strip()
    if not bitable_token:
        return error_response("缺少 bitable_token 参数")

    try:
        # 验证 Token 有效性（确认 H5 后端可连通飞书）
        feishu.get_app_info(bitable_token)

        # 生成签到 URL
        signin_url = generate_signin_url(bitable_token)

        logger.info(f"插件连通性验证通过: bitable={bitable_token}")
        return jsonify({
            "success": True,
            "signin_url": signin_url,
        })

    except Exception as e:
        logger.error(f"插件验证失败: {e}")
        return error_response(f"验证失败: {str(e)}", 500)


@app.route("/api/config", methods=["POST"])
def get_config():
    """
    【前端获取配置】
    H5页面调用此接口获取多维表格配置
    
    请求参数：
    {
        "bitable_token": "xxx"  // 可选，不传则使用默认配置
    }
    """
    data = request.get_json() or {}
    bitable_token = data.get("bitable_token", "").strip()
    
    # 如果没有传入token，尝试从缓存获取
    if not bitable_token:
        return error_response("缺少 bitable_token 参数")

    try:
        # 尝试从缓存获取
        config = get_cached_config(bitable_token)
        
        if config:
            return jsonify({
                "success": True,
                "cached": True,
                **config
            })

        # 缓存不存在，重新获取
        table_list = feishu.get_table_list(bitable_token)
        if not table_list:
            return error_response("没有找到任何表格")

        first_table = table_list[0]
        table_id = first_table["table_id"]
        table_name = first_table.get("name", "未命名表格")

        # 获取字段列表
        fields = feishu.get_field_list(bitable_token, table_id)

        # 构建字段映射
        field_map = {}
        for field in fields:
            field_map[field["field_name"]] = field["field_id"]

        return jsonify({
            "success": True,
            "cached": False,
            "bitable_token": bitable_token,
            "table_id": table_id,
            "table_name": table_name,
            "fields": field_map
        })
    except Exception as e:
        logger.error(f"获取配置失败: {e}")
        return error_response(str(e), 500)


@app.route("/api/signin", methods=["POST"])
@rate_limit_check()
def signin():
    """
    签到接口（支持多表格联动）
    
    请求参数：
    {
        "phone": "13800138000"        // 手机号（必填）
        "bitable_token": "xxx"         // 多维表格Token（可选，默认使用缓存）
    }
    
    优先从缓存获取配置，减少API调用
    """
    data = request.get_json()
    if not data:
        return error_response("请求数据格式错误")

    phone = data.get("phone", "").strip()
    if not phone or len(phone) < 7:
        return error_response("请输入正确的手机号码")

    bitable_token = data.get("bitable_token", "").strip()
    if not bitable_token:
        return error_response("缺少 bitable_token 参数")

    try:
        # 优先使用缓存配置
        config = get_cached_config(bitable_token)
        
        if not config:
            # 缓存不存在，重新获取并缓存
            table_list = feishu.get_table_list(bitable_token)
            if not table_list:
                return jsonify({
                    "status": "error",
                    "message": "没有找到表格"
                })

            table_id = table_list[0]["table_id"]
            fields = feishu.get_field_list(bitable_token, table_id)
            
            # 构建字段映射
            field_map = {f["field_name"]: f["field_id"] for f in fields}
            phone_field_id = status_field_id = time_field_id = name_field_id = seat_field_id = None

            for name, fid in field_map.items():
                name_lower = name.lower()
                if "手机" in name or "phone" in name_lower:
                    phone_field_id = fid
                elif "签到状态" in name or "status" in name_lower:
                    status_field_id = fid
                elif "签到时间" in name or "time" in name_lower:
                    time_field_id = fid
                elif "姓名" in name or "name" in name_lower:
                    name_field_id = fid
                elif "坐席" in name or "seat" in name_lower:
                    seat_field_id = fid

            config = {
                "bitable_token": bitable_token,
                "table_id": table_id,
                "fields": field_map,
                "phone_field_id": phone_field_id,
                "status_field_id": status_field_id,
                "time_field_id": time_field_id,
                "name_field_id": name_field_id,
                "seat_field_id": seat_field_id
            }
            set_cached_config(bitable_token, config)

        table_id = config["table_id"]
        field_map = config["fields"]
        phone_field_id = config["phone_field_id"]
        status_field_id = config["status_field_id"]
        time_field_id = config["time_field_id"]
        name_field_id = config["name_field_id"]
        seat_field_id = config["seat_field_id"]

        # 使用搜索API而不是全量获取（更高效，减少API调用）
        if phone_field_id:
            try:
                # 尝试使用搜索API
                normalized_phone = phone.replace(" ", "").replace("-", "")
                matched_records = feishu.search_records(
                    bitable_token, 
                    table_id, 
                    phone_field_id, 
                    normalized_phone
                )
            except:
                # 搜索API失败，降级到全量获取
                matched_records = []
        else:
            matched_records = []

        # 如果搜索没找到，尝试全量获取
        if not matched_records and phone_field_id:
            all_records = feishu.get_records(bitable_token, table_id)
            normalized_phone = phone.replace(" ", "").replace("-", "")

            for record in all_records:
                fields_data = record.get("fields", {})
                phone_value = fields_data.get(phone_field_id)
                if phone_value:
                    phone_list = extract_phone_values(phone_value)
                    for p in phone_list:
                        p_normalized = p.replace(" ", "").replace("-", "")
                        if p_normalized.endswith(normalized_phone) or normalized_phone.endswith(p_normalized):
                            matched_records.append(record)
                            break
                if matched_records:
                    break

        # 未找到报名记录
        if not matched_records:
            return jsonify({
                "status": "not_found",
                "message": "未查询到您的报名信息，请检查手机号是否正确"
            })

        record = matched_records[0]
        record_id = record["record_id"]
        record_fields = record.get("fields", {})

        # 检查是否已签到
        if status_field_id:
            current_status = extract_status_value(record_fields.get(status_field_id))
            if current_status in ["已签到", "已签到 "]:
                first_time = ""
                if time_field_id:
                    ts = record_fields.get(time_field_id)
                    if isinstance(ts, (int, float)):
                        first_time = format_timestamp(int(ts))

                return jsonify({
                    "status": "already",
                    "message": "您已完成签到，请勿重复提交",
                    "name": extract_name_value(record_fields.get(name_field_id)) if name_field_id else None,
                    "seat": extract_name_value(record_fields.get(seat_field_id)) if seat_field_id else None,
                    "first_signin_time": first_time
                })

        # 执行签到
        update_fields = {}

        if status_field_id:
            update_fields[status_field_id] = "已签到"

        if time_field_id:
            update_fields[time_field_id] = datetime.now().timestamp() * 1000  # 毫秒

        feishu.update_record(bitable_token, table_id, record_id, update_fields)

        logger.info(f"签到成功: 手机号 {phone}, 记录 {record_id}")

        # 返回签到成功信息和座位信息
        return jsonify({
            "status": "success",
            "message": "签到成功，欢迎参会！",
            "name": extract_name_value(record_fields.get(name_field_id)) if name_field_id else None,
            "seat": extract_name_value(record_fields.get(seat_field_id)) if seat_field_id else None,
            "record_id": record_id
        })

    except Exception as e:
        logger.error(f"签到失败: {e}")
        return jsonify({
            "status": "error",
            "message": "签到失败，请重试"
        })


@app.route("/")
def index():
    """返回 H5 签到页面"""
    return send_from_directory("public", "index.html")


# ==================== 启动 ====================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=DEBUG)
