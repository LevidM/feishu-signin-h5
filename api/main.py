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
import threading

# 加载环境变量
load_dotenv()

# 项目根目录（因为 main.py 在 api/ 下，所以往上一级）
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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


class RecordCache:
    """签到记录缓存：手机号→记录 的索引，毫秒级查找"""
    
    CACHE_KEY_PREFIX = "idx_"  # 索引前缀
    CACHE_TTL = 600  # 缓存10分钟
    
    def _index_key(self, bitable_token: str) -> str:
        return f"{self.CACHE_KEY_PREFIX}{bitable_token}"
    
    def _build_index(self, records: list, phone_field_name: str) -> dict:
        """构建手机号→记录的索引"""
        index = {}
        for record in records:
            fields_data = record.get("fields", {})
            phone_value = fields_data.get(phone_field_name)
            if phone_value:
                phones = extract_phone_values(phone_value)
                for p in phones:
                    normalized = p.replace(" ", "").replace("-", "")
                    if normalized and normalized not in index:
                        index[normalized] = record
        return index
    
    def refresh(self, bitable_token: str, table_id: str, phone_field_name: str):
        """拉取全量记录并构建索引缓存"""
        try:
            all_records = feishu.get_records(bitable_token, table_id, page_size=500)
            index = self._build_index(all_records, phone_field_name)
            cache.set(self._index_key(bitable_token), index, ttl_seconds=self.CACHE_TTL)
            logger.info(f"缓存刷新: {bitable_token}, 共 {len(all_records)} 条记录, {len(index)} 个手机号索引")
        except Exception as e:
            logger.warning(f"缓存刷新失败: {e}")
    
    def find_by_phone(self, bitable_token: str, phone: str) -> Optional[dict]:
        """按手机号查找（O(1) 字典查找）"""
        index = cache.get(self._index_key(bitable_token))
        if not index:
            return None
        normalized = phone.replace(" ", "").replace("-", "")
        record = index.get(normalized)
        if record:
            return record
        # 容错：尝试后缀匹配（手机号可能有不同前缀）
        for key, record in index.items():
            if key.endswith(normalized) or normalized.endswith(key):
                return record
        return None

record_cache = RecordCache()

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
            timeout=5.0
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
        field_name: str,
        value: str,
        page_size: int = 1
    ) -> list:
        """根据字段名称搜索记录（更高效）"""
        data = self.api_request(
            "POST",
            f"/bitable/v1/apps/{bitable_token}/tables/{table_id}/records/search",
            json={
                "page_size": page_size,
                "filter": {
                    "conjunction": "and",
                    "conditions": [
                        {
                            "field_name": field_name,  # 传字段名称，非字段 ID
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

    def get_field(
        self,
        bitable_token: str,
        table_id: str,
        field_id: str
    ) -> dict:
        """获取单个字段的详细信息（含单选项的 options）"""
        try:
            data = self.api_request(
                "GET",
                f"/bitable/v1/apps/{bitable_token}/tables/{table_id}/fields/{field_id}"
            )
            return data.get("field", {})
        except Exception as e:
            logger.warning(f"获取字段详情失败: {e}")
            return {}

    def get_view_list(
        self,
        bitable_token: str,
        table_id: str
    ) -> list:
        """获取视图列表"""
        data = self.api_request(
            "GET",
            f"/bitable/v1/apps/{bitable_token}/tables/{table_id}/views",
            params={"page_size": 50}
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
    """缓存配置（1小时后过期，减少飞书 API 调用）"""
    cache_key = f"config_{bitable_token}"
    cache.set(cache_key, config, ttl_seconds=3600)


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


def find_signin_table(bitable_token: str) -> Tuple[str, str]:
    """
    智能查找签到用的正确表格。
    扫描所有表格的字段名，找到包含手机的表格（签到目标表）。
    如果找不到，回退到第一个表格。
    
    返回: (table_id, table_name)
    """
    table_list = feishu.get_table_list(bitable_token)
    if not table_list:
        raise Exception("该多维表格没有任何数据表")

    # 尝试按名称匹配（报名、签到相关）
    for t in table_list:
        tbl_name = t.get("name", "")
        if "报名" in tbl_name or "签到" in tbl_name:
            logger.info(f"按名称匹配到签到表: {tbl_name}")
            return t["table_id"], tbl_name

    # 按字段特征匹配：扫描每个表的字段名，找包含"手机"的表
    candidates = []
    for t in table_list:
        tbl_id = t["table_id"]
        tbl_name = t.get("name", "未命名")
        try:
            fields = feishu.get_field_list(bitable_token, tbl_id)
            for f in fields:
                fname = f.get("field_name", "")
                if "手机" in fname or "phone" in fname.lower():
                    logger.info(f"按字段匹配到签到表: {tbl_name} (字段: {fname})")
                    return tbl_id, tbl_name
        except Exception:
            continue  # 跳过无法读取字段的表

    # 回退到第一个表格
    first = table_list[0]
    logger.warning(f"未找到签到表，回退到: {first.get('name', '未命名')}")
    return first["table_id"], first.get("name", "未命名表格")


def detect_form_url(bitable_token: str, table_id: str, table_name: str = "") -> str:
    """
    检测表格中的表单视图，返回外部可访问的表单 URL。
    优先从飞书表单 API 获取分享链接，失败时用 view_id 构造。
    """
    try:
        views = feishu.get_view_list(bitable_token, table_id)
        for view in views:
            vt = view.get("view_type")
            if vt == "form" or vt == 3 or view.get("type") == 3:
                view_id = view["view_id"]
                # 优先通过表单 API 获取分享链接（包含完整的 shr 分享 ID）
                try:
                    form_data = feishu.api_request(
                        "GET",
                        f"/bitable/v1/apps/{bitable_token}/tables/{table_id}/forms/{view_id}"
                    )
                    form_info = form_data.get("form", {})
                    if form_info.get("shared") and form_info.get("shared_url"):
                        return form_info["shared_url"]
                except Exception:
                    pass
                # 失败时用 view_id 构造链接
                return f"https://fszi-org.feishu.cn/base/{bitable_token}?table={table_id}&view={view_id}"
            # 容错：视图名包含"报名"也视为表单
            if vt == "grid" and "报名" in view.get("view_name", ""):
                view_id = view["view_id"]
                logger.info(f"按名称匹配到报名视图: {view.get('view_name')}")
                return f"https://fszi-org.feishu.cn/base/{bitable_token}?table={table_id}&view={view_id}"
    except Exception as e:
        logger.warning(f"检测表单视图失败: {e}")
    return ""


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


@app.route("/api/cache/status", methods=["GET"])
def cache_status():
    """缓存状态查询（调试用）"""
    bitable_token = request.args.get("token", "").strip()
    if not bitable_token:
        return jsonify({"error": "请提供 ?token=xxx 参数"})
    
    config = get_cached_config(bitable_token)
    index = cache.get(f"idx_{bitable_token}")
    
    return jsonify({
        "ok": True,
        "config_cached": bool(config and "fields" in config),
        "records_cached": bool(index),
        "records_count": len(index) if index else 0,
        "config_keys": list(config.keys()) if config else [],
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
        "bitable_token": "bascnxxx",    // 多维表格 Token（必填）
        "table_id": "tblxxx",           // 表格 ID（可选）
        "event_name": "产品发布会",     // 活动名称（可选）
        "register_form_url": "https://"  // 报名表单 URL（可选）
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

        # 缓存 Block 插件提供的信息（供签到接口使用）
        table_id = data.get("table_id", "").strip()
        register_form_url = data.get("register_form_url", "").strip()
        signin_config = data.get("config") or {}  # 签到行为配置
        
        cache_data = {}
        if table_id:
            cache_data["table_id"] = table_id
        if register_form_url:
            cache_data["register_form_url"] = register_form_url
        if signin_config:
            cache_data["signin_config"] = signin_config
        if cache_data:
            set_cached_config(bitable_token, cache_data)
            logger.info(f"缓存签到配置: {cache_data}")

        # 预加载签到记录到缓存（打开插件即准备，第一个签到者不用等）
        if table_id:
            # 自动检测手机号字段名，并预加载索引缓存
            try:
                fields = feishu.get_field_list(bitable_token, table_id)
                phone_field = None
                for f in fields:
                    fname = f["field_name"]
                    if "手机" in fname or "phone" in fname.lower():
                        phone_field = fname
                        break
                if phone_field:
                    record_cache.refresh(bitable_token, table_id, phone_field)
            except Exception as e:
                logger.warning(f"插件预加载失败: {e}")

        # 生成签到 URL（包含 table_id，便于直接访问时使用）
        signin_url = generate_signin_url(bitable_token)
        if table_id:
            signin_url += f"&table={table_id}"

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
        "bitable_token": "xxx",   // 多维表格 Token
        "table_id": "tblxxx"      // 表格 ID（可选，自动检测）
    }
    """
    data = request.get_json() or {}
    bitable_token = data.get("bitable_token", "").strip()
    
    # 如果没有传入token，尝试从缓存获取
    if not bitable_token:
        return error_response("缺少 bitable_token 参数")

    try:
        # 请求中指定的 table_id（H5 页面从 URL 参数传递）
        request_table_id = data.get("table_id", "").strip()

        # 尝试从缓存获取
        config = get_cached_config(bitable_token)

        # 提取注册表单 URL（可能来自缓存，后续再补充）
        register_form_url = ""
        
        if config and "fields" in config:
            # 完整缓存命中
            return jsonify({
                "success": True,
                "cached": True,
                "bitable_token": bitable_token,
                "table_id": config.get("table_id", ""),
                "table_name": config.get("table_name", ""),
                "fields": config.get("fields", {}),
                "register_form_url": config.get("register_form_url", ""),
            })
        
        # 缓存不存在或不完整，重新获取
        if request_table_id:
            table_id = request_table_id
        elif config and config.get("table_id"):
            table_id = config["table_id"]
            register_form_url = config.get("register_form_url", "")
        else:
            table_id, _ = find_signin_table(bitable_token)

        # 如果还没找到注册表单 URL，自动检测
        if not register_form_url:
            register_form_url = detect_form_url(bitable_token, table_id)

        fields = feishu.get_field_list(bitable_token, table_id)

        # 构建字段映射
        field_map = {}
        for field in fields:
            field_map[field["field_name"]] = field["field_id"]

        # 获取表格名称
        try:
            table_list = feishu.get_table_list(bitable_token)
            table_name = next((t.get("name") for t in table_list if t["table_id"] == table_id), "")
        except:
            table_name = ""

        return jsonify({
            "success": True,
            "cached": False,
            "bitable_token": bitable_token,
            "table_id": table_id,
            "table_name": table_name,
            "fields": field_map,
            "register_form_url": register_form_url,
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
    
    # 优先使用请求中指定的 table_id（H5 页面从 URL 参数传递）
    request_table_id = data.get("table_id", "").strip()

    try:
        # 优先使用缓存配置（必须是完整配置，含字段映射）
        config = get_cached_config(bitable_token)
        
        if not config or "fields" not in config:
            # 缓存不存在或不完整，重新获取并缓存
            # 优先级：请求参数 > 缓存(插件提供) > 自动检测
            if request_table_id:
                table_id = request_table_id
                table_name = ""
            else:
                cached_table_id = config.get("table_id") if config else None
                if cached_table_id:
                    table_id = cached_table_id
                    table_name = ""
                else:
                    table_id, table_name = find_signin_table(bitable_token)
            
            fields = feishu.get_field_list(bitable_token, table_id)
            
            # 构建字段映射（同时存 ID 和名称，飞书 search API 需要传字段名称）
            field_map = {f["field_name"]: f["field_id"] for f in fields}
            phone_field_id = phone_field_name = None
            status_field_id = status_field_name = status_option_id = None
            time_field_id = time_field_name = None
            name_field_id = name_field_name = None
            seat_field_id = seat_field_name = None

            for field in fields:
                name = field["field_name"]
                fid = field["field_id"]
                name_lower = name.lower()
                if "手机" in name or "phone" in name_lower:
                    phone_field_id = fid
                    phone_field_name = name
                elif "签到状态" in name or "status" in name_lower:
                    status_field_id = fid
                    status_field_name = name
                    # 单选项需要传选项 ID，尝试多种方式提取"已签到"选项
                    prop = field.get("property") or {}
                    opts = prop.get("options") or prop.get("option") or []
                    for opt in opts:
                        opt_name = (opt.get("name") or opt.get("text") or "").strip()
                        if "已签到" in opt_name:
                            status_option_id = opt.get("id") or opt.get("option_id")
                            logger.info(f"检测到签到状态选项: name={opt_name}, id={status_option_id}")
                            break
                    if not status_option_id:
                        logger.warning(f"未找到已签到选项，字段属性: {prop}")
                elif "签到时间" in name or "time" in name_lower:
                    time_field_id = fid
                    time_field_name = name
                elif "姓名" in name or "name" in name_lower:
                    name_field_id = fid
                    name_field_name = name
                elif "坐席" in name or "seat" in name_lower:
                    seat_field_id = fid
                    seat_field_name = name

            config = {
                "bitable_token": bitable_token,
                "table_id": table_id,
                "fields": field_map,
                "phone_field_id": phone_field_id,
                "phone_field_name": phone_field_name,
                "status_field_id": status_field_id,
                "status_field_name": status_field_name,
                "status_option_id": status_option_id,
                "time_field_id": time_field_id,
                "time_field_name": time_field_name,
                "name_field_id": name_field_id,
                "name_field_name": name_field_name,
                "seat_field_id": seat_field_id,
                "seat_field_name": seat_field_name,
            }
            # 保留旧缓存中的 register_form_url（Block 插件传入的报名链接）
            old_cache = get_cached_config(bitable_token)
            if old_cache and old_cache.get("register_form_url"):
                config["register_form_url"] = old_cache["register_form_url"]
            else:
                # 没有旧缓存时，自动检测表格中的表单视图
                form_url = detect_form_url(bitable_token, table_id)
                if form_url:
                    config["register_form_url"] = form_url

            set_cached_config(bitable_token, config)

        table_id = config["table_id"]
        field_map = config["fields"]
        phone_field_id = config.get("phone_field_id")
        phone_field_name = config.get("phone_field_name")
        status_field_id = config.get("status_field_id")
        status_field_name = config.get("status_field_name")
        status_option_id = config.get("status_option_id")
        time_field_id = config.get("time_field_id")
        time_field_name = config.get("time_field_name")
        name_field_id = config.get("name_field_id")
        name_field_name = config.get("name_field_name")
        seat_field_id = config.get("seat_field_id")
        seat_field_name = config.get("seat_field_name")

        # 查找报名记录（优先内存缓存，再拉取全量）
        matched_record = None
        if phone_field_name:
            # 1) 先在索引缓存中查找（O(1) 字典，毫秒级）
            matched_record = record_cache.find_by_phone(bitable_token, phone)
            
            # 2) 缓存未命中，拉取全量记录并建索引
            if not matched_record:
                logger.info(f"初始化缓存: {bitable_token}")
                record_cache.refresh(bitable_token, table_id, phone_field_name)
                matched_record = record_cache.find_by_phone(bitable_token, phone)

        # 未找到报名记录（返回报名链接）
        if not matched_record:
            result = {
                "status": "not_found",
                "message": "未查询到您的报名信息，请检查手机号是否正确"
            }
            # register_form_url 可能从 Block 插件缓存的原始 config 来
            original_cache = get_cached_config(bitable_token)
            if original_cache and original_cache.get("register_form_url"):
                result["register_form_url"] = original_cache["register_form_url"]
            return jsonify(result)

        record = matched_record
        record_id = record["record_id"]
        record_fields = record.get("fields", {})

        # 读取签到行为配置
        signin_cfg = (config or {}).get("signin_config") or {}
        update_status = signin_cfg.get("update_signin_status", True)
        update_time = signin_cfg.get("update_signin_time", True)
        return_name = signin_cfg.get("return_name", True)
        return_seat = signin_cfg.get("return_seat", True)
        success_msg = signin_cfg.get("success_message", "签到成功，欢迎参会！")
        already_msg = signin_cfg.get("already_message", "已签到，无需重复签到")

        # 检查是否已签到（用字段名读取 record_fields）
        if status_field_name and update_status:
            current_status = extract_status_value(record_fields.get(status_field_name))
            if current_status in ["已签到", "已签到 "]:
                first_time = ""
                if time_field_name:
                    ts = record_fields.get(time_field_name)
                    if isinstance(ts, (int, float)):
                        first_time = format_timestamp(int(ts))

                return jsonify({
                    "status": "already",
                    "message": already_msg,
                    "name": extract_name_value(record_fields.get(name_field_name)) if name_field_name and return_name else None,
                    "seat": extract_name_value(record_fields.get(seat_field_name)) if seat_field_name and return_seat else None,
                    "first_signin_time": first_time
                })

        # 执行签到（用字段名称作为 key）
        update_fields = {}

        if status_field_name and update_status:
            # 单选项用选项 ID，否则实时查询字段详情获取
            if not status_option_id:
                field_detail = feishu.get_field(bitable_token, table_id, status_field_id)
                for opt in (field_detail.get("property") or {}).get("options", []):
                    opt_name = (opt.get("name") or opt.get("text") or "").strip()
                    if "已签到" in opt_name:
                        status_option_id = opt.get("id") or opt.get("option_id")
                        logger.info(f"查到签到状态选项: {opt_name} -> {status_option_id}")
                        # 缓存查询结果，后续签到不再查字段
                        config["status_option_id"] = status_option_id
                        set_cached_config(bitable_token, config)
                        break
            if status_option_id:
                update_fields[status_field_name] = status_option_id
            else:
                logger.warning("未找到已签到选项 ID，直接使用文本")
                update_fields[status_field_name] = "已签到"

        if time_field_name and update_time:
            update_fields[time_field_name] = datetime.now().timestamp() * 1000  # 毫秒

        # 异步更新飞书表格（不等 API 返回，签到结果先给用户）
        if update_fields:
            def _do_update():
                try:
                    feishu.update_record(bitable_token, table_id, record_id, update_fields)
                    logger.info(f"签到记录已更新: {record_id}")
                except Exception as e:
                    logger.error(f"签到记录更新失败: {e}")
            threading.Thread(target=_do_update, daemon=True).start()

        logger.info(f"签到成功: 手机号 {phone}, 记录 {record_id}")

        # 返回签到成功信息
        return jsonify({
            "status": "success",
            "message": success_msg,
            "name": extract_name_value(record_fields.get(name_field_name)) if name_field_name and return_name else None,
            "seat": extract_name_value(record_fields.get(seat_field_name)) if seat_field_name and return_seat else None,
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
    return send_from_directory(os.path.join(_APP_ROOT, "public"), "index.html")


# ==================== 启动 ====================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=DEBUG)
