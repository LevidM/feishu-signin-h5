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
import time
import logging
import re
import json
import uuid
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional, Tuple, Dict, List
from collections import defaultdict
from threading import Lock
import threading

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
import httpx
import redis

load_dotenv()

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ==================== 配置 ====================

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
SIGNIN_BASE_URL = os.getenv("SIGNIN_BASE_URL", "").rstrip("/")
REDIS_URL = os.getenv("REDIS_URL", "").strip()
CACHE_BACKEND = os.getenv("CACHE_BACKEND", "auto").strip().lower()
CONFIG_CACHE_TTL = int(os.getenv("CONFIG_CACHE_TTL", "21600"))
RECORD_CACHE_TTL = int(os.getenv("RECORD_CACHE_TTL", "21600"))
MISS_REFRESH_COOLDOWN = int(os.getenv("MISS_REFRESH_COOLDOWN", "60"))
FEISHU_API_MAX_RETRIES = int(os.getenv("FEISHU_API_MAX_RETRIES", "2"))
FEISHU_API_RETRY_BASE_DELAY = float(os.getenv("FEISHU_API_RETRY_BASE_DELAY", "0.4"))
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

if not DEBUG and (not FEISHU_APP_ID or not FEISHU_APP_SECRET):
    raise RuntimeError("生产环境必须配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")

# ==================== 缓存与分布式锁 ====================

class MemoryCache:
    """线程安全的内存缓存（未配置 Redis 时的回退实现）"""

    def __init__(self):
        self._data: Dict[str, tuple] = {}  # key -> (value, expire_time)
        self._lock = Lock()

    def get(self, key: str):
        with self._lock:
            if key in self._data:
                value, expire_time = self._data[key]
                if datetime.now() < expire_time:
                    return value
                del self._data[key]
        return None

    def set(self, key: str, value, ttl_seconds: int = 3600):
        with self._lock:
            self._data[key] = (value, datetime.now() + timedelta(seconds=ttl_seconds))

    def delete(self, key: str):
        with self._lock:
            self._data.pop(key, None)


class RedisCache:
    """Redis 缓存，供多 worker / 多进程共享配置和手机号索引。"""

    KEY_PREFIX = "feishu_signin:"

    def __init__(self, redis_url: str):
        self.client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=10,
            health_check_interval=30,
        )
        self.client.ping()

    def _key(self, key: str) -> str:
        return f"{self.KEY_PREFIX}{key}"

    def get(self, key: str):
        raw = self.client.get(self._key(key))
        if raw is None:
            return None
        return json.loads(raw)

    def set(self, key: str, value, ttl_seconds: int = 3600):
        self.client.setex(
            self._key(key),
            ttl_seconds,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        )

    def delete(self, key: str):
        self.client.delete(self._key(key))


def create_cache():
    if CACHE_BACKEND not in ("auto", "memory", "redis"):
        raise RuntimeError("CACHE_BACKEND 只能是 auto、memory 或 redis")
    if CACHE_BACKEND in ("auto", "redis") and REDIS_URL:
        try:
            redis_cache = RedisCache(REDIS_URL)
            logger.info("缓存后端: Redis")
            return redis_cache
        except Exception as e:
            if CACHE_BACKEND == "redis":
                raise RuntimeError(f"Redis 连接失败: {e}") from e
            logger.warning(f"Redis 连接失败，回退到内存缓存: {e}")
    logger.info("缓存后端: Memory")
    return MemoryCache()


cache = create_cache()


class LockManager:
    """提供 Redis 分布式锁；未配置 Redis 时回退到进程内锁。"""

    KEY_PREFIX = "feishu_signin:lock:"

    def __init__(self, cache_backend):
        self.redis_client = cache_backend.client if isinstance(cache_backend, RedisCache) else None
        self._locks: Dict[str, Lock] = {}
        self._locks_guard = Lock()

    def _redis_key(self, key: str) -> str:
        return f"{self.KEY_PREFIX}{key}"

    def acquire(self, key: str, ttl_seconds: int = 30, wait_timeout: float = 0) -> Optional[str]:
        if self.redis_client:
            token = uuid.uuid4().hex
            deadline = time.time() + wait_timeout
            while True:
                if self.redis_client.set(self._redis_key(key), token, nx=True, ex=ttl_seconds):
                    return token
                if time.time() >= deadline:
                    return None
                time.sleep(0.05)

        lock = self._get_memory_lock(key)
        if wait_timeout > 0:
            acquired = lock.acquire(timeout=wait_timeout)
        else:
            acquired = lock.acquire(blocking=False)
        return "memory" if acquired else None

    def release(self, key: str, token: str):
        if not token:
            return
        if self.redis_client:
            script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            end
            return 0
            """
            self.redis_client.eval(script, 1, self._redis_key(key), token)
            return
        self._get_memory_lock(key).release()

    def _get_memory_lock(self, key: str) -> Lock:
        with self._locks_guard:
            if key not in self._locks:
                self._locks[key] = Lock()
            return self._locks[key]


lock_manager = LockManager(cache)


class RecordCache:
    """
    签到记录索引缓存：手机号 → 记录列表，O(1) 查找。

    并发安全设计：
    - 每个 bitable_token 持有独立的锁
    - refresh() 内部 double-check，防止多线程同时触发全量拉取
    - update_record_fields() 持同一把锁修改索引，保证可见性
    """

    CACHE_KEY_PREFIX = "idx_"
    CACHE_TTL = RECORD_CACHE_TTL

    def _index_key(self, bitable_token: str, table_id: str = "") -> str:
        suffix = f"{bitable_token}:{table_id}" if table_id else bitable_token
        return f"{self.CACHE_KEY_PREFIX}{suffix}"

    def _lock_key(self, bitable_token: str, table_id: str = "") -> str:
        suffix = f"{bitable_token}:{table_id}" if table_id else bitable_token
        return f"record_index:{suffix}"

    def _miss_refresh_key(self, bitable_token: str, table_id: str) -> str:
        return f"miss_refresh_{bitable_token}:{table_id}"

    def _build_index(self, records: list, phone_field_name: str) -> dict:
        index = {}
        for record in records:
            phone_value = record.get("fields", {}).get(phone_field_name)
            if not phone_value:
                continue
            for p in extract_phone_values(phone_value):
                normalized = normalize_phone(p)
                if normalized:
                    index.setdefault(normalized, []).append(record)
        return index

    def _find_all_in_index(self, index: dict, phone: str) -> list:
        normalized = normalize_phone(phone)
        records = index.get(normalized)
        if records:
            return records
        # 容错：后缀匹配（手机号前缀可能不同）
        matches = []
        seen_record_ids = set()
        for key, rec in index.items():
            if key.endswith(normalized) or normalized.endswith(key):
                for item in rec:
                    record_id = item.get("record_id")
                    if record_id not in seen_record_ids:
                        seen_record_ids.add(record_id)
                        matches.append(item)
        return matches

    def refresh(self, bitable_token: str, table_id: str, phone_field_name: str, force: bool = False):
        """
        全量拉取并重建索引。
        持锁期间若其他线程已刷新完成，直接返回，避免重复拉取（雷劈效应）。
        """
        lock_token = lock_manager.acquire(self._lock_key(bitable_token, table_id), ttl_seconds=300, wait_timeout=30)
        if not lock_token:
            logger.warning(f"缓存刷新锁获取失败: token={bitable_token}")
            return
        try:
            # double-check：等锁成功后再确认是否仍需刷新
            if not force and cache.get(self._index_key(bitable_token, table_id)):
                return
            all_records = feishu.get_records(bitable_token, table_id)
            index = self._build_index(all_records, phone_field_name)
            cache.set(self._index_key(bitable_token, table_id), index, ttl_seconds=self.CACHE_TTL)
            logger.info(
                f"缓存刷新完成: token={bitable_token}, "
                f"记录={len(all_records)}, 手机号={len(index)}"
            )
        except Exception as e:
            logger.warning(f"缓存刷新失败: token={bitable_token}, error={e}")
        finally:
            lock_manager.release(self._lock_key(bitable_token, table_id), lock_token)

    def refresh_after_miss(self, bitable_token: str, table_id: str, phone_field_name: str) -> bool:
        """
        缓存已有但手机号未命中时，最多每 MISS_REFRESH_COOLDOWN 秒刷新一次。
        避免输错手机号或未报名手机号反复触发全量拉取。
        """
        if not self.has_index(bitable_token, table_id):
            self.refresh(bitable_token, table_id, phone_field_name)
            return True

        cooldown_key = self._miss_refresh_key(bitable_token, table_id)
        if cache.get(cooldown_key):
            return False
        cache.set(cooldown_key, {"at": int(time.time())}, ttl_seconds=MISS_REFRESH_COOLDOWN)
        self.refresh(bitable_token, table_id, phone_field_name, force=True)
        return True

    def has_index(self, bitable_token: str, table_id: str = "") -> bool:
        return cache.get(self._index_key(bitable_token, table_id)) is not None

    def records_count(self, bitable_token: str, table_id: str = "") -> int:
        index = cache.get(self._index_key(bitable_token, table_id))
        if not index:
            return 0
        return sum(len(records) for records in index.values())

    def find_by_phone(self, bitable_token: str, table_id: str, phone: str) -> Optional[dict]:
        matches = self.find_all_by_phone(bitable_token, table_id, phone)
        return matches[0] if matches else None

    def find_all_by_phone(self, bitable_token: str, table_id: str, phone: str) -> list:
        index = cache.get(self._index_key(bitable_token, table_id))
        if not index:
            return []
        return self._find_all_in_index(index, phone)

    def find_by_record_id(self, bitable_token: str, table_id: str, record_id: str) -> Optional[dict]:
        index = cache.get(self._index_key(bitable_token, table_id))
        if not index:
            return None
        for records in index.values():
            for record in records:
                if record.get("record_id") == record_id:
                    return record
        return None

    def update_record_fields(self, bitable_token: str, table_id: str, phone: str, new_fields: dict):
        """
        签到成功后立即更新缓存中对应记录的字段。
        持锁保证与 refresh() 之间的可见性，防止并发覆盖索引。
        """
        lock_token = lock_manager.acquire(self._lock_key(bitable_token, table_id), ttl_seconds=30, wait_timeout=5)
        if not lock_token:
            logger.warning(f"缓存更新锁获取失败: token={bitable_token}")
            return
        try:
            index = cache.get(self._index_key(bitable_token, table_id))
            if not index:
                return
            for record in self._find_all_in_index(index, phone):
                if record and "fields" in record:
                    record["fields"].update(new_fields)
            cache.set(self._index_key(bitable_token, table_id), index, ttl_seconds=self.CACHE_TTL)
        finally:
            lock_manager.release(self._lock_key(bitable_token, table_id), lock_token)

    def upsert_records(self, bitable_token: str, table_id: str, phone_field_name: str, records: list):
        if not records:
            return
        lock_token = lock_manager.acquire(self._lock_key(bitable_token, table_id), ttl_seconds=30, wait_timeout=5)
        if not lock_token:
            logger.warning(f"缓存合并锁获取失败: token={bitable_token}, table={table_id}")
            return
        try:
            index = cache.get(self._index_key(bitable_token, table_id)) or {}
            incoming = self._build_index(records, phone_field_name)
            for phone_key, new_records in incoming.items():
                by_id = {record.get("record_id"): record for record in index.get(phone_key, [])}
                for record in new_records:
                    by_id[record.get("record_id")] = record
                index[phone_key] = list(by_id.values())
            cache.set(self._index_key(bitable_token, table_id), index, ttl_seconds=self.CACHE_TTL)
        finally:
            lock_manager.release(self._lock_key(bitable_token, table_id), lock_token)

    def update_record_fields_by_id(self, bitable_token: str, table_id: str, record_id: str, new_fields: dict):
        lock_token = lock_manager.acquire(self._lock_key(bitable_token, table_id), ttl_seconds=30, wait_timeout=5)
        if not lock_token:
            logger.warning(f"缓存更新锁获取失败: token={bitable_token}")
            return
        try:
            index = cache.get(self._index_key(bitable_token, table_id))
            if not index:
                return
            updated = False
            for records in index.values():
                for record in records:
                    if record.get("record_id") == record_id and "fields" in record:
                        record["fields"].update(new_fields)
                        updated = True
            if updated:
                cache.set(self._index_key(bitable_token, table_id), index, ttl_seconds=self.CACHE_TTL)
        finally:
            lock_manager.release(self._lock_key(bitable_token, table_id), lock_token)


record_cache = RecordCache()

# ==================== 限流器 ====================

class RateLimiter:
    """滑动窗口限流（生产环境可替换为 Redis + Lua 脚本）"""

    def __init__(self, max_requests: int = 300, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: Dict[str, List[float]] = defaultdict(list)
        self._lock = Lock()

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        window_start = now - self.window_seconds
        with self._lock:
            self._requests[key] = [t for t in self._requests[key] if t > window_start]
            if len(self._requests[key]) >= self.max_requests:
                return False
            self._requests[key].append(now)
            return True


rate_limiter = RateLimiter(max_requests=300, window_seconds=60)

# ==================== 飞书 API 客户端 ====================

class FeishuClient:
    """飞书 Open API 客户端，线程安全"""

    BASE_URL = "https://open.feishu.cn/open-apis"
    TOKEN_URL = f"{BASE_URL}/auth/v3/app_access_token/internal"

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token_cache: Optional[Tuple[str, datetime]] = None
        self._token_lock = Lock()  # 防止并发时重复刷新 token

    def get_app_access_token(self) -> str:
        with self._token_lock:
            if self._token_cache:
                token, expires_at = self._token_cache
                if datetime.now() < expires_at - timedelta(minutes=5):
                    return token

            response = httpx.post(
                self.TOKEN_URL,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 0:
                raise Exception(f"获取飞书 Access Token 失败: {data.get('msg')}")

            token = data["app_access_token"]
            expires_in = data.get("expire", 7200)
            self._token_cache = (token, datetime.now() + timedelta(seconds=expires_in))
            return token

    def api_request(self, method: str, path: str, timeout: float = 10.0, **kwargs) -> dict:
        token = self.get_app_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        for attempt in range(FEISHU_API_MAX_RETRIES + 1):
            response = httpx.request(
                method=method,
                url=f"{self.BASE_URL}{path}",
                headers=headers,
                timeout=timeout,
                **kwargs,
            )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < FEISHU_API_MAX_RETRIES:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else FEISHU_API_RETRY_BASE_DELAY * (2 ** attempt)
                    except ValueError:
                        delay = FEISHU_API_RETRY_BASE_DELAY * (2 ** attempt)
                    time.sleep(delay)
                    continue
            response.raise_for_status()
            data = response.json()
            if data.get("code") == 0:
                return data.get("data", {})
            logger.error(f"飞书 API 错误: path={path}, code={data.get('code')}, msg={data.get('msg')}")
            raise Exception(data.get("msg", "API 请求失败"))
        raise Exception("飞书 API 请求失败")

    def get_records(self, bitable_token: str, table_id: str, page_size: int = 500) -> list:
        """分页拉取全量记录，支持超过 500 条"""
        all_items = []
        page_token = None
        while True:
            params: dict = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            data = self.api_request(
                "GET",
                f"/bitable/v1/apps/{bitable_token}/tables/{table_id}/records",
                timeout=30.0,
                params=params,
            )
            all_items.extend(data.get("items", []))
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
        return all_items

    def search_records_by_phone(
        self, bitable_token: str, table_id: str, phone_field_name: str, phone: str, page_size: int = 100
    ) -> list:
        """按手机号精确补查，避免新报名用户因全量缓存未刷新而不可见。"""
        all_items = []
        page_token = None
        normalized_phone = normalize_phone(phone)
        payload = {
            "filter": {
                "conjunction": "and",
                "conditions": [
                    {
                        "field_name": phone_field_name,
                        "operator": "contains",
                        "value": [normalized_phone],
                    }
                ],
            }
        }
        while True:
            params: dict = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            data = self.api_request(
                "POST",
                f"/bitable/v1/apps/{bitable_token}/tables/{table_id}/records/search",
                timeout=15.0,
                params=params,
                json=payload,
            )
            all_items.extend(data.get("items", []))
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
        return all_items

    def update_record(
        self, bitable_token: str, table_id: str, record_id: str, fields: dict
    ) -> dict:
        return self.api_request(
            "PUT",
            f"/bitable/v1/apps/{bitable_token}/tables/{table_id}/records/{record_id}",
            json={"fields": fields},
        )

    def get_field_list(self, bitable_token: str, table_id: str) -> list:
        data = self.api_request(
            "GET",
            f"/bitable/v1/apps/{bitable_token}/tables/{table_id}/fields",
        )
        return data.get("items", [])

    def get_view_list(self, bitable_token: str, table_id: str) -> list:
        data = self.api_request(
            "GET",
            f"/bitable/v1/apps/{bitable_token}/tables/{table_id}/views",
            params={"page_size": 50},
        )
        return data.get("items", [])

    def get_table_list(self, bitable_token: str) -> list:
        data = self.api_request("GET", f"/bitable/v1/apps/{bitable_token}/tables")
        return data.get("items", [])

    def get_app_info(self, bitable_token: str) -> dict:
        return self.api_request("GET", f"/bitable/v1/apps/{bitable_token}")


feishu = FeishuClient(FEISHU_APP_ID, FEISHU_APP_SECRET)

# ==================== Flask 应用 ====================

app = Flask(__name__, static_folder="public", static_url_path="")
if ALLOWED_ORIGINS:
    CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}, r"/health": {"origins": ALLOWED_ORIGINS}})
elif DEBUG:
    CORS(app)

# ==================== 辅助函数 ====================

def rate_limit_check():
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            client_ip = request.remote_addr or "unknown"
            if not rate_limiter.is_allowed(client_ip):
                logger.warning(f"限流触发: ip={client_ip}")
                return error_response("请求过于频繁，请稍后再试", 429)
            return f(*args, **kwargs)
        return wrapped
    return decorator


def normalize_phone(phone: str) -> str:
    return re.sub(r"[\s\-()]+", "", (phone or "").strip())


def is_valid_phone(phone: str) -> bool:
    normalized = normalize_phone(phone)
    return bool(re.fullmatch(r"\+?\d{7,15}", normalized))


def signin_lock_key(bitable_token: str, phone: str) -> str:
    return f"signin:{bitable_token}:{normalize_phone(phone)}"


def generate_signin_url(bitable_token: str) -> str:
    if not SIGNIN_BASE_URL:
        return f"/?app={bitable_token}"
    return f"{SIGNIN_BASE_URL}/?app={bitable_token}"


def get_cached_config(bitable_token: str) -> Optional[dict]:
    return cache.get(f"config_{bitable_token}")


def set_cached_config(bitable_token: str, config: dict):
    cache.set(f"config_{bitable_token}", config, ttl_seconds=CONFIG_CACHE_TTL)


def extract_phone_values(value) -> list:
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
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        item = value[0]
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            return item.get("name", item.get("text", ""))
    if isinstance(value, dict):
        return value.get("name", value.get("text", ""))
    return ""


def extract_status_value(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        item = value[0]
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            return item.get("name", item.get("text", ""))
    return ""


def detect_phone_field_name(fields: list) -> str:
    return next(
        (
            f["field_name"]
            for f in fields
            if "手机" in f["field_name"] or "phone" in f["field_name"].lower()
        ),
        "",
    )


def format_timestamp(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def error_response(message: str, status_code: int = 400):
    return jsonify({"status": "error", "message": message}), status_code


def build_candidate(record: dict, name_field_name: str, seat_field_name: str, status_field_name: str) -> dict:
    fields = record.get("fields", {})
    return {
        "record_id": record.get("record_id", ""),
        "name": extract_name_value(fields.get(name_field_name)) if name_field_name else "",
        "seat": extract_name_value(fields.get(seat_field_name)) if seat_field_name else "",
        "signin_status": extract_status_value(fields.get(status_field_name)) if status_field_name else "",
    }


def start_record_cache_preload(
    bitable_token: str,
    table_id: str,
    phone_field_name: str = "",
) -> bool:
    if not bitable_token or not table_id or record_cache.has_index(bitable_token, table_id):
        return False

    def _preload():
        try:
            resolved_phone_field = phone_field_name
            if not resolved_phone_field:
                fields = feishu.get_field_list(bitable_token, table_id)
                resolved_phone_field = detect_phone_field_name(fields)
            if not resolved_phone_field:
                logger.warning(f"缓存预热跳过，未找到手机号字段: token={bitable_token}, table={table_id}")
                return
            record_cache.refresh(bitable_token, table_id, resolved_phone_field)
        except Exception as e:
            logger.warning(f"缓存预热失败: token={bitable_token}, table={table_id}, error={e}")

    threading.Thread(target=_preload, daemon=True).start()
    return True


def find_signin_table(bitable_token: str) -> Tuple[str, str]:
    """智能查找签到表：按名称 → 按字段 → 回退到第一个表"""
    table_list = feishu.get_table_list(bitable_token)
    if not table_list:
        raise Exception("该多维表格没有任何数据表")

    for t in table_list:
        tbl_name = t.get("name", "")
        if "报名" in tbl_name or "签到" in tbl_name:
            logger.info(f"按名称匹配到签到表: {tbl_name}")
            return t["table_id"], tbl_name

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
            continue

    first = table_list[0]
    logger.warning(f"未找到签到表，回退到: {first.get('name', '未命名')}")
    return first["table_id"], first.get("name", "未命名表格")


def detect_form_url(bitable_token: str, table_id: str) -> str:
    """检测表格中的表单视图，返回外部可访问的报名链接"""
    try:
        views = feishu.get_view_list(bitable_token, table_id)
        for view in views:
            vt = view.get("view_type")
            if vt == "form" or vt == 3 or view.get("type") == 3:
                view_id = view["view_id"]
                try:
                    form_data = feishu.api_request(
                        "GET",
                        f"/bitable/v1/apps/{bitable_token}/tables/{table_id}/forms/{view_id}",
                    )
                    form_info = form_data.get("form", {})
                    if form_info.get("shared") and form_info.get("shared_url"):
                        return form_info["shared_url"]
                except Exception:
                    pass
                return (
                    f"https://fszi-org.feishu.cn/base/{bitable_token}"
                    f"?table={table_id}&view={view_id}"
                )
            if vt == "grid" and "报名" in view.get("view_name", ""):
                view_id = view["view_id"]
                logger.info(f"按名称匹配到报名视图: {view.get('view_name')}")
                return (
                    f"https://fszi-org.feishu.cn/base/{bitable_token}"
                    f"?table={table_id}&view={view_id}"
                )
    except Exception as e:
        logger.warning(f"检测表单视图失败: {e}")
    return ""


# ==================== API 路由 ====================

@app.route("/health")
def health_check():
    connected = False
    try:
        feishu.get_app_access_token()
        connected = True
    except Exception as e:
        logger.error(f"飞书连接失败: {e}")
    return jsonify({
        "status": "ok" if connected else "degraded",
        "feishu_connected": connected,
        "cache_backend": "redis" if isinstance(cache, RedisCache) else "memory",
    })


@app.route("/api/cache/status", methods=["GET"])
def cache_status():
    if not DEBUG:
        return error_response("Not found", 404)
    bitable_token = request.args.get("token", "").strip()
    if not bitable_token:
        return jsonify({"error": "请提供 ?token=xxx 参数"})
    table_id = request.args.get("table", "").strip()
    config = get_cached_config(bitable_token)
    index = cache.get(record_cache._index_key(bitable_token, table_id))
    return jsonify({
        "ok": True,
        "cache_backend": "redis" if isinstance(cache, RedisCache) else "memory",
        "config_cached": bool(config and "fields" in config),
        "records_cached": record_cache.has_index(bitable_token, table_id),
        "records_count": record_cache.records_count(bitable_token, table_id),
        "phone_keys_count": len(index) if index else 0,
        "config_keys": list(config.keys()) if config else [],
    })


@app.route("/api/cache/preload", methods=["POST"])
@rate_limit_check()
def cache_preload():
    """
    主动预热手机号索引缓存。活动开始前调用一次，可避免首个签到用户承担全量拉取耗时。
    """
    data = request.get_json() or {}
    bitable_token = data.get("bitable_token", "").strip()
    if not bitable_token:
        return error_response("缺少 bitable_token 参数")

    try:
        request_table_id = data.get("table_id", "").strip()
        config = get_cached_config(bitable_token) or {}
        table_id = request_table_id or config.get("table_id", "")
        if not table_id:
            table_id, _ = find_signin_table(bitable_token)

        phone_field_name = config.get("phone_field_name", "")
        if not phone_field_name:
            fields = feishu.get_field_list(bitable_token, table_id)
            phone_field_name = detect_phone_field_name(fields)
        if not phone_field_name:
            return error_response("未找到手机号字段，无法预热缓存", 400)

        started = start_record_cache_preload(bitable_token, table_id, phone_field_name)
        return jsonify({
            "success": True,
            "started": started,
            "already_cached": not started and record_cache.has_index(bitable_token, table_id),
            "table_id": table_id,
        })
    except Exception as e:
        logger.error(f"缓存预热接口失败: {e}")
        return error_response("缓存预热失败，请检查表格权限和字段配置", 500)


@app.route("/api/plugin/register", methods=["POST"])
@rate_limit_check()
def plugin_register():
    """
    【插件验证接口 - 可选】
    Block 插件调用此接口验证 H5 后端到飞书 API 的连通性，并预热签到记录缓存。

    请求参数：
    {
        "bitable_token": "bascnxxx",    // 多维表格 Token（必填）
        "table_id": "tblxxx",           // 表格 ID（可选）
        "event_name": "产品发布会",     // 活动名称（可选）
        "register_form_url": "https://" // 报名表单 URL（可选）
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
        feishu.get_app_info(bitable_token)

        table_id = data.get("table_id", "").strip()
        register_form_url = data.get("register_form_url", "").strip()
        signin_config = data.get("config") or {}

        cache_data: dict = {}
        if table_id:
            cache_data["table_id"] = table_id
        if register_form_url:
            cache_data["register_form_url"] = register_form_url
        if signin_config:
            cache_data["signin_config"] = signin_config
        if cache_data:
            set_cached_config(bitable_token, cache_data)
            logger.info(f"缓存签到配置: token={bitable_token}")

        if table_id:
            start_record_cache_preload(bitable_token, table_id)

        signin_url = generate_signin_url(bitable_token)
        if table_id:
            signin_url += f"&table={table_id}"

        logger.info(f"插件验证通过: bitable={bitable_token}")
        return jsonify({"success": True, "signin_url": signin_url})

    except Exception as e:
        logger.error(f"插件验证失败: {e}")
        return error_response(f"验证失败: {str(e)}", 500)


@app.route("/api/config", methods=["POST"])
def get_config():
    """
    【前端获取配置】
    H5 页面调用此接口获取多维表格配置

    请求参数：
    {
        "bitable_token": "xxx",   // 多维表格 Token
        "table_id": "tblxxx"      // 表格 ID（可选，自动检测）
    }
    """
    data = request.get_json() or {}
    bitable_token = data.get("bitable_token", "").strip()
    if not bitable_token:
        return error_response("缺少 bitable_token 参数")

    try:
        request_table_id = data.get("table_id", "").strip()
        config = get_cached_config(bitable_token)

        if config and "fields" in config:
            start_record_cache_preload(
                bitable_token,
                config.get("table_id", ""),
                config.get("phone_field_name", ""),
            )
            return jsonify({
                "success": True,
                "cached": True,
                "bitable_token": bitable_token,
                "table_id": config.get("table_id", ""),
                "table_name": config.get("table_name", ""),
                "fields": config.get("fields", {}),
                "register_form_url": config.get("register_form_url", ""),
            })

        if request_table_id:
            table_id = request_table_id
        elif config and config.get("table_id"):
            table_id = config["table_id"]
        else:
            table_id, _ = find_signin_table(bitable_token)

        register_form_url = (config.get("register_form_url") or "") if config else ""
        if not register_form_url:
            register_form_url = detect_form_url(bitable_token, table_id)

        fields = feishu.get_field_list(bitable_token, table_id)
        field_map = {f["field_name"]: f["field_id"] for f in fields}
        phone_field_name = status_field_name = time_field_name = None
        name_field_name = seat_field_name = None
        phone_field_id = status_field_id = time_field_id = None
        name_field_id = seat_field_id = None

        for f in fields:
            fname = f["field_name"]
            fid = f["field_id"]
            fname_lower = fname.lower()
            if "手机" in fname or "phone" in fname_lower:
                phone_field_id, phone_field_name = fid, fname
            elif "签到状态" in fname or "status" in fname_lower:
                status_field_id, status_field_name = fid, fname
            elif "签到时间" in fname or "time" in fname_lower:
                time_field_id, time_field_name = fid, fname
            elif "姓名" in fname or "name" in fname_lower:
                name_field_id, name_field_name = fid, fname
            elif "坐席" in fname or "seat" in fname_lower or "座位" in fname:
                seat_field_id, seat_field_name = fid, fname

        try:
            table_list = feishu.get_table_list(bitable_token)
            table_name = next(
                (t.get("name") for t in table_list if t["table_id"] == table_id), ""
            )
        except Exception:
            table_name = ""

        response_data = {
            "success": True,
            "cached": False,
            "bitable_token": bitable_token,
            "table_id": table_id,
            "table_name": table_name,
            "fields": field_map,
            "register_form_url": register_form_url,
        }

        cached_config = dict(config or {})
        cached_config.update(response_data)
        cached_config.update({
            "phone_field_id": phone_field_id,
            "phone_field_name": phone_field_name,
            "status_field_id": status_field_id,
            "status_field_name": status_field_name,
            "time_field_id": time_field_id,
            "time_field_name": time_field_name,
            "name_field_id": name_field_id,
            "name_field_name": name_field_name,
            "seat_field_id": seat_field_id,
            "seat_field_name": seat_field_name,
        })
        set_cached_config(bitable_token, cached_config)
        start_record_cache_preload(bitable_token, table_id, phone_field_name or "")

        return jsonify(response_data)
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
        "phone": "13800138000",    // 手机号（必填）
        "bitable_token": "xxx",    // 多维表格 Token（必填）
        "table_id": "tblxxx",      // 表格 ID（可选，URL 参数传递）
        "record_id": "recxxx"      // 记录 ID（可选，多人共用手机号时二次确认）
    }
    """
    data = request.get_json()
    if not data:
        return error_response("请求数据格式错误")

    phone = normalize_phone(data.get("phone", ""))
    if not is_valid_phone(phone):
        return error_response("请输入正确的手机号码")

    bitable_token = data.get("bitable_token", "").strip()
    if not bitable_token:
        return error_response("缺少 bitable_token 参数")

    request_table_id = data.get("table_id", "").strip()
    selected_record_id = data.get("record_id", "").strip()

    # 同一手机号并发签到防重：Redis 分布式锁可覆盖多个 gunicorn worker。
    lock_key = signin_lock_key(bitable_token, phone)
    lock_token = lock_manager.acquire(lock_key, ttl_seconds=120, wait_timeout=0)
    if not lock_token:
        return error_response("签到请求处理中，请勿重复提交", 429)
    try:
        return _do_signin(phone, bitable_token, request_table_id, selected_record_id)
    finally:
        lock_manager.release(lock_key, lock_token)


def _do_signin(phone: str, bitable_token: str, request_table_id: str, selected_record_id: str = ""):
    """签到核心逻辑（调用方已持 signin_lock）"""
    try:
        config = get_cached_config(bitable_token)

        if not config or "fields" not in config:
            # 优先级：请求参数 > 插件缓存 > 自动检测
            if request_table_id:
                table_id = request_table_id
            elif config and config.get("table_id"):
                table_id = config["table_id"]
            else:
                table_id, _ = find_signin_table(bitable_token)

            fields = feishu.get_field_list(bitable_token, table_id)
            field_map = {f["field_name"]: f["field_id"] for f in fields}

            phone_field_name = status_field_name = time_field_name = None
            name_field_name = seat_field_name = None
            phone_field_id = status_field_id = time_field_id = None
            name_field_id = seat_field_id = None

            for f in fields:
                fname = f["field_name"]
                fid = f["field_id"]
                fname_lower = fname.lower()
                if "手机" in fname or "phone" in fname_lower:
                    phone_field_id, phone_field_name = fid, fname
                elif "签到状态" in fname or "status" in fname_lower:
                    status_field_id, status_field_name = fid, fname
                elif "签到时间" in fname or "time" in fname_lower:
                    time_field_id, time_field_name = fid, fname
                elif "姓名" in fname or "name" in fname_lower:
                    name_field_id, name_field_name = fid, fname
                elif "坐席" in fname or "seat" in fname_lower or "座位" in fname:
                    seat_field_id, seat_field_name = fid, fname

            # 保留旧缓存中插件提供的报名链接和签到行为配置
            old_form_url = (config.get("register_form_url") or "") if config else ""
            old_signin_config = (config.get("signin_config") or {}) if config else {}

            config = {
                "bitable_token": bitable_token,
                "table_id": table_id,
                "fields": field_map,
                "phone_field_id": phone_field_id,
                "phone_field_name": phone_field_name,
                "status_field_id": status_field_id,
                "status_field_name": status_field_name,
                "time_field_id": time_field_id,
                "time_field_name": time_field_name,
                "name_field_id": name_field_id,
                "name_field_name": name_field_name,
                "seat_field_id": seat_field_id,
                "seat_field_name": seat_field_name,
            }
            if old_signin_config:
                config["signin_config"] = old_signin_config
            if old_form_url:
                config["register_form_url"] = old_form_url
            else:
                form_url = detect_form_url(bitable_token, table_id)
                if form_url:
                    config["register_form_url"] = form_url

            set_cached_config(bitable_token, config)

        table_id = config["table_id"]
        phone_field_name = config.get("phone_field_name")
        status_field_name = config.get("status_field_name")
        time_field_name = config.get("time_field_name")
        name_field_name = config.get("name_field_name")
        seat_field_name = config.get("seat_field_name")

        # 查找报名记录：优先缓存索引；未命中时按手机号精确补查飞书，避免新报名记录等待全量缓存刷新。
        matched_records = []
        if phone_field_name:
            matched_records = record_cache.find_all_by_phone(bitable_token, table_id, phone)
            if not matched_records:
                search_failed = False
                try:
                    fresh_records = feishu.search_records_by_phone(bitable_token, table_id, phone_field_name, phone)
                    if fresh_records:
                        record_cache.upsert_records(bitable_token, table_id, phone_field_name, fresh_records)
                        matched_records = record_cache.find_all_by_phone(bitable_token, table_id, phone)
                except Exception as e:
                    search_failed = True
                    logger.warning(f"手机号精确补查失败，回退缓存刷新: phone={phone}, error={e}")
                if not matched_records and (search_failed or not record_cache.has_index(bitable_token, table_id)):
                    record_cache.refresh_after_miss(bitable_token, table_id, phone_field_name)
                    matched_records = record_cache.find_all_by_phone(bitable_token, table_id, phone)

        if not matched_records:
            result: dict = {
                "status": "not_found",
                "message": "未查询到您的报名信息，请检查手机号是否正确",
            }
            form_url = config.get("register_form_url", "")
            if form_url:
                result["register_form_url"] = form_url
            return jsonify(result)

        matched_record = None
        if selected_record_id:
            matched_record = next(
                (record for record in matched_records if record.get("record_id") == selected_record_id),
                None,
            )
            if not matched_record:
                return error_response("所选报名记录无效，请重新选择", 400)
        elif len(matched_records) > 1:
            candidates = [
                build_candidate(record, name_field_name, seat_field_name, status_field_name)
                for record in matched_records
            ]
            return jsonify({
                "status": "multiple",
                "message": "该手机号关联了多位参会人，请选择本人完成签到",
                "candidates": candidates,
            })
        else:
            matched_record = matched_records[0]

        record_id = matched_record["record_id"]
        record_fields = matched_record.get("fields", {})

        signin_cfg = config.get("signin_config") or {}
        update_status = signin_cfg.get("update_signin_status", True)
        update_time = signin_cfg.get("update_signin_time", True)
        return_name = signin_cfg.get("return_name", True)
        return_seat = signin_cfg.get("return_seat", True)
        success_msg = signin_cfg.get("success_message", "签到成功，欢迎参会！")
        already_msg = signin_cfg.get("already_message", "已签到，无需重复签到")

        # 检查是否已签到
        if status_field_name and update_status:
            current_status = extract_status_value(record_fields.get(status_field_name))
            if current_status in ("已签到", "已签到 "):
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
                    "first_signin_time": first_time,
                })

        # 执行签到
        update_fields: dict = {}
        if status_field_name and update_status:
            update_fields[status_field_name] = "已签到"
        if time_field_name and update_time:
            update_fields[time_field_name] = int(datetime.now().timestamp() * 1000)

        # 生产环境需确认飞书写入成功后再向用户返回签到成功。
        if update_fields:
            feishu.update_record(bitable_token, table_id, record_id, update_fields)
            record_cache.update_record_fields_by_id(bitable_token, table_id, record_id, update_fields)
            logger.info(f"飞书记录已更新: record_id={record_id}")

        logger.info(f"签到成功: phone={phone}, record_id={record_id}")
        return jsonify({
            "status": "success",
            "message": success_msg,
            "name": extract_name_value(record_fields.get(name_field_name)) if name_field_name and return_name else None,
            "seat": extract_name_value(record_fields.get(seat_field_name)) if seat_field_name and return_seat else None,
            "record_id": record_id,
        })

    except Exception as e:
        logger.error(f"签到处理异常: phone={phone}, error={e}")
        return jsonify({"status": "error", "message": "签到失败，请重试"})


@app.route("/")
def index():
    return send_from_directory(os.path.join(_APP_ROOT, "public"), "index.html")


# ==================== 启动 ====================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=DEBUG)
