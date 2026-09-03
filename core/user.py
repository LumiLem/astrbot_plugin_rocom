"""
用户绑定本地持久化存储

使用 StarTools.get_data_dir() 确保数据存储在 AstrBot data 目录下，
插件更新/重装不会丢失用户数据。
"""

import os
import json
import copy
import asyncio
from typing import List, Dict, Optional, Any
from astrbot.api import logger


class AsyncDataManager:
    """通用异步 JSON 数据管理器"""

    def __init__(self, data_dir: str, filename: str, default_data: Any):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, filename)
        self.default_data = default_data
        self.lock = asyncio.Lock()
        if not os.path.exists(data_dir):
            os.makedirs(data_dir, exist_ok=True)
        self.data = self._load()

    def _load(self) -> Any:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"[Rocom] 加载 {self.path} 失败: {e}")
        return copy.deepcopy(self.default_data)

    async def _save(self):
        try:
            temp_path = self.path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.path)
        except Exception as e:
            logger.error(f"[Rocom] 保存 {self.path} 失败: {e}")


class UserManager(AsyncDataManager):
    """用户绑定管理"""

    def __init__(self, data_dir: str):
        super().__init__(data_dir, "rocom_bindings.json", {})

    async def get_user_bindings(self, user_id: Any) -> List[Dict]:
        user_id = str(user_id)
        async with self.lock:
            return copy.deepcopy(self.data.get(user_id, []))

    async def get_primary_binding(self, user_id: Any) -> Optional[Dict]:
        bindings = await self.get_user_bindings(user_id)
        for b in bindings:
            if b.get("is_primary"):
                return b
        return bindings[0] if bindings else None

    async def save_user_bindings(self, user_id: Any, bindings: List[Dict]):
        user_id = str(user_id)
        async with self.lock:
            # 去重（按 binding_id）
            cleaned = []
            seen = set()
            for b in bindings:
                bid = b.get("binding_id") or b.get("framework_token", "")
                if bid not in seen:
                    cleaned.append(b)
                    seen.add(bid)

            # 确保有且只有一个 is_primary
            if cleaned:
                has_primary = False
                for b in cleaned:
                    if b.get("is_primary"):
                        if has_primary:
                            b["is_primary"] = False
                        else:
                            has_primary = True
                if not has_primary:
                    cleaned[0]["is_primary"] = True

            self.data[user_id] = cleaned
            await self._save()

    async def add_binding(self, user_id: Any, binding: Dict):
        """添加一个绑定，自动设为主账号"""
        user_id = str(user_id)
        existing = await self.get_user_bindings(user_id)
        # 先取消其他的 primary
        for b in existing:
            b["is_primary"] = False
        binding["is_primary"] = True
        existing.append(binding)
        await self.save_user_bindings(user_id, existing)

    async def replace_binding_for_role(self, user_id: Any, binding: Dict) -> Dict[str, Any]:
        """
        用新绑定覆盖同一用户下相同 role_id 的旧绑定。

        安全顺序：
        1. 读取当前用户全部绑定
        2. 清理相同 role_id 的旧绑定（包括旧 token / 旧 binding_id）
        3. 写入新绑定并设为主账号
        """
        user_id = str(user_id)
        role_id = str(binding.get("role_id", "") or "")
        new_binding_id = binding.get("binding_id") or binding.get("framework_token", "")
        removed_items = []

        existing = await self.get_user_bindings(user_id)
        kept = []
        for item in existing:
            item_role_id = str(item.get("role_id", "") or "")
            item_binding_id = item.get("binding_id") or item.get("framework_token", "")
            if role_id and item_role_id == role_id:
                removed_items.append(item)
                continue
            if new_binding_id and item_binding_id == new_binding_id:
                removed_items.append(item)
                continue
            item["is_primary"] = False
            kept.append(item)

        binding["is_primary"] = True
        kept.append(binding)
        await self.save_user_bindings(user_id, kept)

        return {
            "removed_count": len(removed_items),
            "removed_items": removed_items,
        }

    async def delete_user_binding(self, user_id: Any, index: int) -> Optional[Dict]:
        """按序号(1-based)删除绑定，返回被删除的绑定"""
        user_id = str(user_id)
        bindings = await self.get_user_bindings(user_id)
        if not (1 <= index <= len(bindings)):
            return None
        removed = bindings.pop(index - 1)
        await self.save_user_bindings(user_id, bindings)
        return removed

    async def switch_primary(self, user_id: Any, index: int) -> bool:
        """按序号 (1-based) 切换主账号"""
        user_id = str(user_id)
        bindings = await self.get_user_bindings(user_id)
        if not (1 <= index <= len(bindings)):
            return False
        for i, b in enumerate(bindings):
            b["is_primary"] = (i + 1 == index)
        await self.save_user_bindings(user_id, bindings)
        return True

    async def remove_binding_by_id(self, user_id: Any, binding_id: str) -> bool:
        """按 binding_id 删除指定绑定，返回是否删除成功"""
        user_id = str(user_id)
        async with self.lock:
            bindings = self.data.get(user_id, [])
            original_len = len(bindings)
            bindings = [b for b in bindings if b.get("binding_id") != binding_id]
            if len(bindings) < original_len:
                self.data[user_id] = bindings
                await self._save()
                return True
            return False

    async def get_all_users_bindings(self) -> Dict[str, List[Dict]]:
        """获取所有用户的绑定数据（深拷贝）"""
        async with self.lock:
            result = {}
            for user_id, bindings in self.data.items():
                result[user_id] = copy.deepcopy(bindings)
            return result


class MerchantSubscriptionManager(AsyncDataManager):
    """Per-group merchant subscription storage."""

    def __init__(self, data_dir: str):
        super().__init__(data_dir, "rocom_merchant_subscriptions.json", {})

    async def upsert_subscription(self, group_key: str, subscription: Dict[str, Any]):
        async with self.lock:
            self.data[str(group_key)] = copy.deepcopy(subscription)
            await self._save()

    async def get_subscription(self, group_key: str) -> Optional[Dict[str, Any]]:
        async with self.lock:
            item = self.data.get(str(group_key))
            return copy.deepcopy(item) if item else None

    async def delete_subscription(self, group_key: str) -> bool:
        async with self.lock:
            key = str(group_key)
            if key not in self.data:
                return False
            del self.data[key]
            await self._save()
            return True

    async def get_all_subscriptions(self) -> Dict[str, Dict[str, Any]]:
        async with self.lock:
            return copy.deepcopy(self.data)


class HomeSubscriptionManager(AsyncDataManager):
    """Home garden and pet inspiration subscription storage."""

    def __init__(self, data_dir: str):
        super().__init__(data_dir, "rocom_home_subscriptions.json", {})

    async def upsert_subscription(self, key: str, subscription: Dict[str, Any]):
        async with self.lock:
            self.data[str(key)] = copy.deepcopy(subscription)
            await self._save()

    async def get_subscription(self, key: str) -> Optional[Dict[str, Any]]:
        async with self.lock:
            item = self.data.get(str(key))
            return copy.deepcopy(item) if item else None

    async def delete_subscription(self, key: str) -> bool:
        async with self.lock:
            key = str(key)
            if key not in self.data:
                return False
            del self.data[key]
            await self._save()
            return True

    async def delete_matching(self, session_id: str, kind: str = "", uid: str = "") -> int:
        async with self.lock:
            session_id = str(session_id)
            kind = str(kind or "")
            uid = str(uid or "")
            keys = []
            for key, item in self.data.items():
                if str(item.get("umo", "")) != session_id:
                    continue
                if kind and str(item.get("kind", "")) != kind:
                    continue
                if uid and str(item.get("uid", "")) != uid:
                    continue
                keys.append(key)
            for key in keys:
                del self.data[key]
            if keys:
                await self._save()
            return len(keys)

    async def get_all_subscriptions(self) -> Dict[str, Dict[str, Any]]:
        async with self.lock:
            return copy.deepcopy(self.data)


class AnnouncementSubscriptionManager(AsyncDataManager):
    """RoCom announcement subscription storage."""

    def __init__(self, data_dir: str):
        super().__init__(data_dir, "rocom_announcement_subscriptions.json", {})

    async def upsert_subscription(self, key: str, subscription: Dict[str, Any]):
        async with self.lock:
            self.data[str(key)] = copy.deepcopy(subscription)
            await self._save()

    async def get_subscription(self, key: str) -> Optional[Dict[str, Any]]:
        async with self.lock:
            item = self.data.get(str(key))
            return copy.deepcopy(item) if item else None

    async def delete_subscription(self, key: str) -> bool:
        async with self.lock:
            key = str(key)
            if key not in self.data:
                return False
            del self.data[key]
            await self._save()
            return True

    async def get_all_subscriptions(self) -> Dict[str, Dict[str, Any]]:
        async with self.lock:
            return copy.deepcopy(self.data)


class BroadcastTaskManager(AsyncDataManager):
    """Persistent storage for scheduled broadcast tasks."""

    def __init__(self, data_dir: str):
        super().__init__(data_dir, "rocom_broadcast_tasks.json", {})

    async def add_task(self, task_id: str, task_data: Dict[str, Any]):
        async with self.lock:
            self.data[str(task_id)] = copy.deepcopy(task_data)
            await self._save()

    async def get_all_tasks(self) -> Dict[str, Dict[str, Any]]:
        async with self.lock:
            return copy.deepcopy(self.data)

    async def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        async with self.lock:
            task = self.data.get(str(task_id))
            return copy.deepcopy(task) if task else None

    async def update_task(self, task_id: str, updates: Dict[str, Any]) -> bool:
        async with self.lock:
            key = str(task_id)
            if key not in self.data:
                return False
            self.data[key].update(copy.deepcopy(updates))
            await self._save()
            return True

    async def delete_task(self, task_id: str) -> bool:
        async with self.lock:
            key = str(task_id)
            if key not in self.data:
                return False
            del self.data[key]
            await self._save()
            return True


class ActiveUserManager(AsyncDataManager):
    """Silent tracking of active users for global broadcasts."""

    def __init__(self, data_dir: str):
        super().__init__(data_dir, "rocom_active_users.json", {})

    async def record_user(self, umo: str):
        import time
        async with self.lock:
            self.data[str(umo)] = {"last_active": int(time.time())}
            await self._save()

    async def get_active_users(self, days: int = 0) -> List[str]:
        import time
        now = int(time.time())
        threshold = (now - (days * 24 * 3600)) if days > 0 else 0
        async with self.lock:
            active_umos = []
            for umo, info in self.data.items():
                if days <= 0 or info.get("last_active", 0) >= threshold:
                    active_umos.append(umo)
            return active_umos
