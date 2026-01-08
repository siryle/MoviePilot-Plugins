import re
import time
import traceback
import threading
import os
import urllib.parse
from typing import Any, List, Dict, Tuple, Optional

from app.core.cache import cached
from app.core.event import eventmanager, Event
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.modules.themoviedb import CategoryHelper
from app.plugins import _PluginBase
from app.schemas import WebhookEventInfo, ServiceInfo, MediaServerItem
from app.schemas.types import EventType, MediaType, MediaImageType, NotificationType
from app.utils.web import WebUtils


class mediaservermsgai(_PluginBase):
    """
    媒体服务器通知插件 AI增强版

    功能：
    1. 监听Emby/Jellyfin/Plex等媒体服务器的Webhook事件
    2. 根据配置发送播放、入库等通知消息
    3. 对TV剧集入库事件进行智能聚合，避免消息轰炸
    4. 支持多种媒体服务器和丰富的消息类型配置
    5. 基于TMDB元数据增强消息内容（评分、分类、演员等）
    6. 支持音乐专辑和单曲入库通知
    7. 自动过滤TMDB找不到的资源（可配置）
    """

    # ==================== 常量定义 ====================
    DEFAULT_EXPIRATION_TIME = 600              # 默认过期时间（秒）
    DEFAULT_AGGREGATE_TIME = 15                # 默认聚合时间（秒）
    DEFAULT_OVERVIEW_MAX_LENGTH = 150          # 默认简介最大长度
    IMAGE_CACHE_MAX_SIZE = 100                 # 图片缓存最大数量

    # ==================== 插件基本信息 ====================
    plugin_name = "媒体库服务器通知AI版"
    plugin_desc = "基于Emby识别结果+TMDB元数据+微信清爽版(全消息类型+剧集聚合)"
    plugin_icon = "mediaplay.png"
    plugin_version = "1.8.0"
    plugin_author = "jxxghp"
    author_url = "https://github.com/jxxghp"
    plugin_config_prefix = "mediaservermsgai_"
    plugin_order = 14
    auth_level = 1

    # ==================== 插件运行时状态配置 ====================
    _enabled = False                           # 插件是否启用
    _add_play_link = False                     # 是否添加播放链接
    _mediaservers = None                       # 媒体服务器列表
    _types = []                                # 启用的消息类型
    _webhook_msg_keys = {}                     # Webhook消息去重缓存
    _lock = threading.Lock()                   # 线程锁
    _last_event_cache: Tuple[Optional[Event], float] = (None, 0.0)  # 事件去重缓存
    _image_cache = {}                          # 图片URL缓存
    _overview_max_length = DEFAULT_OVERVIEW_MAX_LENGTH  # 简介最大长度
    _filter_no_tmdb = False                    # 是否过滤TMDB未识别视频
    _filter_play_events = False                # 是否过滤播放事件中的TMDB未识别视频

    # ==================== TV剧集消息聚合配置 ====================
    _aggregate_enabled = False                 # 是否启用TV剧集聚合功能
    _aggregate_time = DEFAULT_AGGREGATE_TIME   # 聚合时间窗口（秒）
    _pending_messages = {}                     # 待聚合的消息 {series_key: [(event_info, event), ...]}
    _aggregate_timers = {}                     # 聚合定时器 {series_key: timer}
    _smart_category_enabled = True             # 是否启用智能分类（CategoryHelper）

    # ==================== Webhook事件映射配置 ====================
    _webhook_actions = {
        "library.new": "已入库",
        "system.webhooktest": "测试",
        "system.notificationtest": "测试",
        "playback.start": "开始播放",
        "playback.stop": "停止播放",
        "playback.pause": "暂停播放",
        "playback.unpause": "继续播放",
        "user.authenticated": "登录成功",
        "user.authenticationfailed": "登录失败",
        "media.play": "开始播放",
        "media.stop": "停止播放",
        "media.pause": "暂停播放",
        "media.resume": "继续播放",
        "item.rate": "标记了",
        "item.markplayed": "标记已播放",
        "item.markunplayed": "标记未播放",
        "PlaybackStart": "开始播放",
        "PlaybackStop": "停止播放"
    }
    
    # ==================== 媒体服务器默认图标（优化后的官方高清图标）====================
    _webhook_images = {
        "emby": "https://raw.githubusercontent.com/qqcomeup/MoviePilot-Plugins/bb3ca257f74cf000640f9ebadab257bb0850baac/icons/11-11.jpg",
        "plex": "https://raw.githubusercontent.com/qqcomeup/MoviePilot-Plugins/bb3ca257f74cf000640f9ebadab257bb0850baac/icons/11-11.jpg",
        "jellyfin": "https://raw.githubusercontent.com/qqcomeup/MoviePilot-Plugins/bb3ca257f74cf000640f9ebadab257bb0850baac/icons/11-11.jpg"
    }

    # ==================== 国家/地区中文映射 ====================
    _country_cn_map = {
        'CN': '中国大陆', 'US': '美国', 'JP': '日本', 'KR': '韩国',
        'HK': '中国香港', 'TW': '中国台湾', 'GB': '英国', 'FR': '法国',
        'DE': '德国', 'IT': '意大利', 'ES': '西班牙', 'IN': '印度',
        'TH': '泰国', 'RU': '俄罗斯', 'CA': '加拿大', 'AU': '澳大利亚',
        'SG': '新加坡', 'MY': '马来西亚', 'VN': '越南', 'PH': '菲律宾',
        'ID': '印度尼西亚', 'BR': '巴西', 'MX': '墨西哥', 'AR': '阿根廷',
        'NL': '荷兰', 'BE': '比利时', 'SE': '瑞典', 'DK': '丹麦',
        'NO': '挪威', 'FI': '芬兰', 'PL': '波兰', 'TR': '土耳其'
    }

    def __init__(self):
        """
        初始化插件实例
        """
        super().__init__()
        self.category = CategoryHelper()
        logger.debug("媒体服务器消息插件AI版初始化完成")

    def init_plugin(self, config: dict = None):
        """
        初始化插件配置

        Args:
            config (dict, optional): 插件配置参数
        """
        if config:
            self._enabled = config.get("enabled")
            self._types = config.get("types") or []
            self._mediaservers = config.get("mediaservers") or []
            self._add_play_link = config.get("add_play_link", False)
            self._overview_max_length = int(config.get("overview_max_length", self.DEFAULT_OVERVIEW_MAX_LENGTH))
            self._aggregate_enabled = config.get("aggregate_enabled", False)
            self._aggregate_time = int(config.get("aggregate_time", self.DEFAULT_AGGREGATE_TIME))
            self._smart_category_enabled = config.get("smart_category_enabled", True)
            self._filter_no_tmdb = config.get("filter_no_tmdb", False)
            self._filter_play_events = config.get("filter_play_events", False)

    def service_infos(self, type_filter: Optional[str] = None) -> Optional[Dict[str, ServiceInfo]]:
        """
        获取媒体服务器信息服务信息

        Args:
            type_filter (str, optional): 媒体服务器类型过滤器

        Returns:
            Dict[str, ServiceInfo]: 活跃的媒体服务器服务信息字典
        """
        if not self._mediaservers:
            logger.debug("尚未配置媒体服务器")
            return None
        services = MediaServerHelper().get_services(type_filter=type_filter, name_filters=self._mediaservers)
        if not services:
            logger.debug("获取媒体服务器实例失败")
            return None
        
        active_services = {}
        for service_name, service_info in services.items():
            if service_info.instance.is_inactive():
                logger.warning(f"媒体服务器 {service_name} 未连接")
            else:
                active_services[service_name] = service_info
        
        return active_services if active_services else None

    def service_info(self, name: str) -> Optional[ServiceInfo]:
        """
        根据名称获取特定媒体服务器服务信息

        Args:
            name (str): 媒体服务器名称

        Returns:
            ServiceInfo: 媒体服务器服务信息
        """
        return (self.service_infos() or {}).get(name)

    def get_state(self) -> bool:
        """
        获取插件状态

        Returns:
            bool: 插件是否启用
        """
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        获取插件命令（当前未实现）

        Returns:
            List[Dict[str, Any]]: 空列表
        """
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API（当前未实现）

        Returns:
            List[Dict[str, Any]]: 空列表
        """
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        
        Returns:
            Tuple[List[dict], Dict[str, Any]]: 页面配置和默认数据
        """
        types_options = [
            {"title": "新入库", "value": "library.new"},
            {"title": "开始播放", "value": "playback.start|media.play|PlaybackStart"},
            {"title": "停止播放", "value": "playback.stop|media.stop|PlaybackStop"},
            {"title": "暂停/继续", "value": "playback.pause|playback.unpause|media.pause|media.resume"},
            {"title": "用户标记", "value": "item.rate|item.markplayed|item.markunplayed"},
            {"title": "登录提醒", "value": "user.authenticated|user.authenticationfailed"},
            {"title": "系统测试", "value": "system.webhooktest|system.notificationtest"},
        ]
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow', 
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'add_play_link', 'label': '添加播放链接'}}]}
                        ]
                    },
                    {
                        'component': 'VRow', 
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VSelect', 'props': {'multiple': True, 'chips': True, 'clearable': True, 'model': 'mediaservers', 'label': '媒体服务器', 'items': [{"title": config.name, "value": config.name} for config in MediaServerHelper().get_configs().values()]}}]}
                        ]
                    },
                    {
                        'component': 'VRow', 
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VSelect', 'props': {'chips': True, 'multiple': True, 'model': 'types', 'label': '消息类型', 'items': types_options}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'aggregate_enabled', 'label': '启用TV剧集入库聚合'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'smart_category_enabled', 'label': '启用智能分类（关闭则使用路径解析）'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'filter_no_tmdb', 'label': '过滤TMDB未识别视频（入库事件）'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'filter_play_events', 'label': '过滤TMDB未识别视频（播放事件）'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'props': {'show': '{{aggregate_enabled}}'},
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'aggregate_time', 'label': '聚合等待时间（秒）', 'placeholder': '15', 'type': 'number'}}]}
                        ]
                    }
                ]
            }
        ], {
            "enabled": False, 
            "types": [], 
            "aggregate_enabled": False, 
            "aggregate_time": self.DEFAULT_AGGREGATE_TIME,
            "smart_category_enabled": True,
            "filter_no_tmdb": False,
            "filter_play_events": False
        }
    
    def get_page(self) -> List[dict]:
        """
        获取插件页面（当前未实现）

        Returns:
            List[dict]: 空列表
        """
        pass

    @eventmanager.register(EventType.WebhookMessage)
    def send(self, event: Event):
        """
        发送通知消息主入口函数
        处理来自媒体服务器的Webhook事件，并根据配置决定是否发送通知消息

        处理流程：
        1. 检查插件是否启用
        2. 验证事件数据有效性
        3. 检查事件类型是否在支持范围内
        4. 检查事件类型是否在用户配置的允许范围内
        5. 验证媒体服务器配置
        6. 根据事件类型分发到对应处理函数

        Args:
            event (Event): Webhook事件对象
        """
        try:
            if not self._enabled:
                logger.debug("插件未启用")
                return
            
            event_info: WebhookEventInfo = event.event_data
            if not event_info:
                logger.debug("事件数据为空")
                return
            
            # 打印event_info用于调试
            logger.info(f"收到Webhook事件: {event_info}")
            
            # 兼容性处理：如果没有映射的动作，尝试使用原始事件名
            if not self._webhook_actions.get(event_info.event):
                logger.debug(f"未知的Webhook事件类型: {event_info.event}")
                return

            # 类型过滤 - 将配置的类型预处理为一个扁平集合，提高查找效率
            allowed_types = set()
            for _type in self._types:
                allowed_types.update(_type.split("|"))
            
            if event_info.event not in allowed_types:
                logger.debug(f"未开启 {event_info.event} 类型的消息通知")
                return

            # 验证媒体服务器配置
            if event_info.server_name and not self.service_info(name=event_info.server_name):
                logger.debug(f"未开启媒体服务器 {event_info.server_name} 的消息通知")
                return

            event_type = str(event_info.event).lower()

            # === 1. 系统测试消息 ===
            if "test" in event_type:
                self._handle_test_event(event_info)
                return

            # === 2. 用户登录消息 ===
            if "user.authentic" in event_type:
                self._handle_login_event(event_info)
                return

            # === 3. 评分/标记消息 ===
            if "item." in event_type and ("rate" in event_type or "mark" in event_type):
                self._handle_rate_event(event_info)
                return

            # === 4. 音乐专辑处理 (仅入库时) ===
            if event_info.json_object and event_info.json_object.get('Item', {}).get('Type') == 'MusicAlbum' and event_type == 'library.new':
                self._handle_music_album(event_info, event_info.json_object.get('Item', {}))
                return

            # === 5. 检查TMDB资源过滤 ===
            # 检查是否应该过滤此事件
            if self._should_filter_event(event_info, event_type):
                logger.info(f"过滤TMDB未识别视频: {event_info.item_name} (事件类型: {event_type})")
                return

            # === 6. 剧集聚合处理 (仅TV入库时) ===
            if (self._aggregate_enabled and 
                event_type == "library.new" and 
                event_info.item_type in ["TV", "SHOW"]):
                
                series_id = self._get_series_id(event_info)
                if series_id:
                    logger.debug(f"满足TV剧集聚合条件，series_id={series_id}")
                    self._aggregate_tv_episodes(series_id, event_info, event)
                    return

            # === 7. 常规媒体消息 (电影入库、播放开始/停止、单集入库、单曲入库等) ===
            self._process_media_event(event, event_info)

        except Exception as e:
            logger.error(f"Webhook分发异常: {str(e)}")
            logger.error(traceback.format_exc())

    def _should_filter_event(self, event_info: WebhookEventInfo, event_type: str) -> bool:
        """
        检查是否应该过滤此事件（基于TMDB资源查找）
        
        Args:
            event_info: Webhook事件信息
            event_type: 事件类型
            
        Returns:
            bool: True表示应该过滤，False表示不应该过滤
        """
        # 跳过不需要TMDB查找的事件类型
        skip_filter_events = [
            "system.webhooktest",
            "system.notificationtest",
            "user.authenticated",
            "user.authenticationfailed",
            "item.rate",
            "item.markplayed",
            "item.markunplayed"
        ]
        
        if event_type in skip_filter_events:
            return False
        
        # 检查是否是音频文件（音乐）
        if event_info.item_type == "AUD":
            logger.debug(f"音频文件跳过TMDB过滤: {event_info.item_name}")
            return False
        
        # 检查是否是播放事件且未开启播放事件过滤
        is_play_event = any(play_key in event_type for play_key in ["playback", "media.play", "media.stop", "Playback"])
        if is_play_event and not self._filter_play_events:
            logger.debug(f"播放事件跳过TMDB过滤: {event_info.item_name}")
            return False
        
        # 检查是否是入库事件且未开启入库事件过滤
        is_library_event = "library.new" in event_type
        if is_library_event and not self._filter_no_tmdb:
            logger.debug(f"入库事件跳过TMDB过滤: {event_info.item_name}")
            return False
        
        # 尝试获取TMDB ID
        tmdb_id = self._extract_tmdb_id(event_info)
        if not tmdb_id:
            logger.debug(f"未找到TMDB ID: {event_info.item_name}")
            return True  # 没有TMDB ID，过滤掉
        
        # 根据媒体类型尝试从TMDB获取信息
        try:
            if event_info.item_type == "MOV":
                tmdb_info = self.chain.recognize_media(tmdbid=int(tmdb_id), mtype=MediaType.MOVIE)
            elif event_info.item_type in ["TV", "SHOW"]:
                tmdb_info = self.chain.recognize_media(tmdbid=int(tmdb_id), mtype=MediaType.TV)
            else:
                logger.debug(f"未知的媒体类型: {event_info.item_type}")
                return False
            
            # 检查TMDB信息是否有效
            if not tmdb_info or not hasattr(tmdb_info, 'id') or not tmdb_info.id:
                logger.debug(f"TMDB找不到此资源: {event_info.item_name} (TMDB ID: {tmdb_id})")
                return True
                
            logger.debug(f"TMDB找到资源: {event_info.item_name} (TMDB ID: {tmdb_id})")
            return False
            
        except Exception as e:
            logger.debug(f"查询TMDB信息失败: {str(e)}")
            return True  # 查询失败，过滤掉

    def _handle_test_event(self, event_info: WebhookEventInfo):
        """
        处理测试消息

        Args:
            event_info (WebhookEventInfo): Webhook事件信息
        """
        title = f"🔔 媒体服务器通知测试"
        server_name = self._get_server_name_cn(event_info)
        texts = [
            f"来自：{server_name}",
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"状态：连接正常"
        ]
        if event_info.user_name:
            texts.append(f"用户：{event_info.user_name}")
            
        self.post_message(
            mtype=NotificationType.MediaServer,
            title=title,
            text="\n".join(texts),
            image=self._webhook_images.get(event_info.channel)
        )

    def _handle_login_event(self, event_info: WebhookEventInfo):
        """
        处理登录消息

        Args:
            event_info (WebhookEventInfo): Webhook事件信息
        """
        action = "登录成功" if "authenticated" in event_info.event and "failed" not in event_info.event else "登录失败"
        title = f"🔐 {action}提醒"
        
        texts = []
        texts.append(f"👤 用户：{event_info.user_name}")
        texts.append(f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        if event_info.device_name:
            texts.append(f"📱 设备：{event_info.client} {event_info.device_name}")
        if event_info.ip:
            try:
                location = WebUtils.get_location(event_info.ip)
                texts.append(f"🌐 IP：{event_info.ip} {location}")
            except Exception as e:
                logger.debug(f"获取IP位置信息时出错: {str(e)}")
                texts.append(f"🌐 IP：{event_info.ip}")
            
        server_name = self._get_server_name_cn(event_info)
        texts.append(f"🖥️ 服务器：{server_name}")

        self.post_message(
            mtype=NotificationType.MediaServer,
            title=title,
            text="\n".join(texts),
            image=self._webhook_images.get(event_info.channel)
        )

    def _handle_rate_event(self, event_info: WebhookEventInfo):
        """
        处理评分/标记消息

        Args:
            event_info (WebhookEventInfo): Webhook事件信息
        """
        # 检查是否应该过滤（评分事件也需要检查TMDB）
        if self._filter_no_tmdb and event_info.item_type in ["MOV", "TV", "SHOW"]:
            tmdb_id = self._extract_tmdb_id(event_info)
            if not tmdb_id:
                logger.info(f"过滤评分事件（无TMDB ID）: {event_info.item_name}")
                return
            
            try:
                if event_info.item_type == "MOV":
                    tmdb_info = self.chain.recognize_media(tmdbid=int(tmdb_id), mtype=MediaType.MOVIE)
                else:
                    tmdb_info = self.chain.recognize_media(tmdbid=int(tmdb_id), mtype=MediaType.TV)
                
                if not tmdb_info or not hasattr(tmdb_info, 'id') or not tmdb_info.id:
                    logger.info(f"过滤评分事件（TMDB找不到资源）: {event_info.item_name}")
                    return
            except Exception:
                logger.info(f"过滤评分事件（TMDB查询失败）: {event_info.item_name}")
                return
        
        item_name = event_info.item_name
            
        title = f"⭐ 用户评分：{item_name}"
        texts = []
        texts.append(f"👤 用户：{event_info.user_name}")
        texts.append(f"🏷️ 标记：{self._webhook_actions.get(event_info.event, '已标记')}")
        texts.append(f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 尝试获取图片
        tmdb_id = self._extract_tmdb_id(event_info)
        image_url = event_info.image_url
        if not image_url and tmdb_id:
            mtype = MediaType.MOVIE if event_info.item_type == "MOV" else MediaType.TV
            image_url = self._get_tmdb_image(event_info, mtype)

        self.post_message(
            mtype=NotificationType.MediaServer,
            title=title,
            text="\n".join(texts),
            image=image_url or self._webhook_images.get(event_info.channel)
        )

    def _process_media_event(self, event: Event, event_info: WebhookEventInfo):
        """处理常规媒体消息（入库/播放）"""
        # 0. 清理过期缓存
        self._clean_expired_cache()
        
        # 1. 防重复与防抖
        expiring_key = f"{event_info.item_id}-{event_info.client}-{event_info.user_name}-{event_info.event}"
        if str(event_info.event) == "playback.stop" and expiring_key in self._webhook_msg_keys:
            self._add_key_cache(expiring_key)
            return
        
        with self._lock:
            current_time = time.time()
            last_event, last_time = self._last_event_cache
            if last_event and (current_time - last_time < 2):
                if last_event.event_id == event.event_id or last_event.event_data == event_info: return
            self._last_event_cache = (event, current_time)

        # 2. 元数据识别
        tmdb_id = self._extract_tmdb_id(event_info)
        event_info.tmdb_id = tmdb_id
        
        # 3. 双重验证TMDB信息（确保之前过滤检查通过）
        tmdb_info = None
        if tmdb_id:
            mtype = MediaType.MOVIE if event_info.item_type == "MOV" else MediaType.TV
            try:
                tmdb_info = self.chain.recognize_media(tmdbid=int(tmdb_id), mtype=mtype)
                if not tmdb_info or not hasattr(tmdb_info, 'id') or not tmdb_info.id:
                    logger.debug(f"TMDB信息无效，跳过发送消息: {event_info.item_name}")
                    return
            except Exception as e:
                logger.debug(f"获取TMDB信息失败，跳过发送消息: {str(e)}")
                return
        
        message_texts = []
        message_title = ""
        image_url = event_info.image_url
        
        # 4. 音频单曲特殊处理
        if event_info.item_type == "AUD":
            self._build_audio_message(event_info, message_texts)
            # 标题构造
            action_base = self._webhook_actions.get(event_info.event, "通知")
            server_name = self._get_server_name_cn(event_info)
            song_name = event_info.item_name
            if event_info.json_object:
                song_name = event_info.json_object.get('Item', {}).get('Name') or song_name
            message_title = f"{song_name} {action_base} {server_name}"
            # 图片
            img = self._get_audio_image_url(event_info.server_name, event_info.json_object.get('Item', {}))
            if img: image_url = img

        # 5. 视频处理 (TV/MOV)
        else:
            # 标题构造
            title_name = event_info.item_name
            
            year = tmdb_info.year if (tmdb_info and tmdb_info.year) else event_info.json_object.get('Item', {}).get('ProductionYear')
            if year and str(year) not in title_name:
                title_name += f" ({year})"
            
            action_base = self._webhook_actions.get(event_info.event, "通知")
            type_cn = "剧集" if event_info.item_type in ["TV", "SHOW"] else "电影"
            action_text = f"{type_cn}{action_base}"
            server_name = self._get_server_name_cn(event_info)

            message_title = f"🆕 {title_name} {action_base}"

            # 内容构造
            message_texts.append(f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}")
            
            # 智能分类（优先使用CategoryHelper，fallback到路径解析）
            category = None
            if self._smart_category_enabled and tmdb_info:
                try:
                    if event_info.item_type == "MOV":
                        category = self.category.get_movie_category(tmdb_info)
                    else:
                        category = self.category.get_tv_category(tmdb_info)
                except Exception as e:
                    logger.debug(f"获取TMDB分类时出错: {str(e)}")
            
            if not category:
                is_folder = event_info.json_object.get('Item', {}).get('IsFolder', False) if event_info.json_object else False
                category = self._get_category_from_path(event_info.item_path, event_info.item_type, is_folder)
            
            if category:
                message_texts.append(f"📂 分类：{category}")

            self._append_season_episode_info(message_texts, event_info, title_name)
            self._append_meta_info(message_texts, tmdb_info)
            self._append_genres_actors(message_texts, tmdb_info)

            # 简介 (播放事件可能不需要太长的简介，可选优化)
            overview = ""
            if tmdb_info and tmdb_info.overview: overview = tmdb_info.overview
            elif event_info.overview: overview = event_info.overview
            
            if overview:
                if len(overview) > self._overview_max_length:
                    overview = overview[:self._overview_max_length].rstrip() + "..."
                message_texts.append(f"📖 简介：\n{overview}")

            # 图片
            if not image_url:
                if event_info.item_type in ["TV", "SHOW"] and tmdb_id:
                    image_url = self._get_tmdb_image(event_info, MediaType.TV)
                elif event_info.item_type == "MOV" and tmdb_id:
                    image_url = self._get_tmdb_image(event_info, MediaType.MOVIE)

        # 6. 附加信息（用户、进度等）
        self._append_extra_info(message_texts, event_info)
        
        # 7. 播放链接
        play_link = self._get_play_link(event_info)

        # 8. 兜底图片
        if not image_url:
            image_url = self._webhook_images.get(event_info.channel)

        # 9. 缓存管理（用于过滤重复停止事件）
        if str(event_info.event) == "playback.stop":
            self._add_key_cache(expiring_key)
        if str(event_info.event) == "playback.start":
            self._remove_key_cache(expiring_key)

        # 10. 发送
        self.post_message(
            mtype=NotificationType.MediaServer,
            title=message_title,
            text="\n" + "\n".join(message_texts),
            image=image_url,
            link=play_link
        )

    # === 辅助构建函数 ===
    def _build_audio_message(self, event_info, texts):
        item_data = event_info.json_object.get('Item', {})
        artist = (item_data.get('Artists') or ['未知歌手'])[0]
        album = item_data.get('Album', '')
        duration = self._format_ticks(item_data.get('RunTimeTicks', 0))
        container = item_data.get('Container', '').upper()
        size = self._format_size(item_data.get('Size', 0))

        texts.append(f"⏰ 时间：{time.strftime('%H:%M:%S', time.localtime())}")
        texts.append(f"👤 歌手：{artist}")
        if album: texts.append(f"💿 专辑：{album}")
        texts.append(f"⏱️ 时长：{duration}")
        texts.append(f"📦 格式：{container} · {size}")

    def _get_series_id(self, event_info: WebhookEventInfo) -> Optional[str]:
        if event_info.json_object and isinstance(event_info.json_object, dict):
            item = event_info.json_object.get("Item", {})
            return item.get("SeriesId") or item.get("SeriesName")
        return getattr(event_info, "series_id", None)

    # === 剧集聚合逻辑 ===
    def _aggregate_tv_episodes(self, series_id: str, event_info: WebhookEventInfo, event: Event):
        with self._lock:
            if series_id not in self._pending_messages:
                self._pending_messages[series_id] = []
            
            self._pending_messages[series_id].append((event_info, event))
            
            if series_id in self._aggregate_timers:
                self._aggregate_timers[series_id].cancel()
            
            timer = threading.Timer(self._aggregate_time, self._send_aggregated_message, [series_id])
            self._aggregate_timers[series_id] = timer
            timer.start()

    def _send_aggregated_message(self, series_id: str):
        with self._lock:
            if series_id not in self._pending_messages or not self._pending_messages[series_id]:
                if series_id in self._aggregate_timers: del self._aggregate_timers[series_id]
                return
            msg_list = self._pending_messages.pop(series_id)
            if series_id in self._aggregate_timers: del self._aggregate_timers[series_id]

        if not msg_list: return
        
        # 单条直接回退到常规处理（但需要重新检查TMDB过滤）
        if len(msg_list) == 1:
            event_info, event_obj = msg_list[0]
            # 检查TMDB过滤
            if not self._should_filter_event(event_info, "library.new"):
                self._process_media_event(event_obj, event_info)
            else:
                logger.info(f"过滤单条剧集聚合消息（TMDB找不到资源）: {event_info.item_name}")
            return

        # 多条聚合
        first_info = msg_list[0][0]
        events_info = [x[0] for x in msg_list]
        count = len(events_info)

        # 检查TMDB信息
        tmdb_id = self._extract_tmdb_id(first_info)
        if not tmdb_id and self._filter_no_tmdb:
            logger.info(f"过滤剧集聚合消息（无TMDB ID）: {first_info.item_name}")
            return
        
        first_info.tmdb_id = tmdb_id
        
        tmdb_info = None
        if tmdb_id:
            try:
                tmdb_info = self.chain.recognize_media(tmdbid=int(tmdb_id), mtype=MediaType.TV)
                if not tmdb_info or not hasattr(tmdb_info, 'id') or not tmdb_info.id:
                    logger.info(f"过滤剧集聚合消息（TMDB找不到资源）: {first_info.item_name}")
                    return
            except Exception:
                logger.info(f"过滤剧集聚合消息（TMDB查询失败）: {first_info.item_name}")
                return

        title_name = first_info.item_name
        if first_info.json_object:
            title_name = first_info.json_object.get('Item', {}).get('SeriesName') or title_name
        
        year = tmdb_info.year if (tmdb_info and tmdb_info.year) else first_info.json_object.get('Item', {}).get('ProductionYear')
        if year and str(year) not in title_name:
            title_name += f" ({year})"
        
        server_name = self._get_server_name_cn(first_info)
        message_title = f"🆕 {title_name} 已入库 (含{count}个文件)"

        message_texts = []
        message_texts.append(f"⏰ {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}")
        
        # 智能分类（优先使用CategoryHelper）
        category = None
        if self._smart_category_enabled and tmdb_info:
            try:
                category = self.category.get_tv_category(tmdb_info)
            except Exception as e:
                logger.debug(f"获取TMDB分类时出错: {str(e)}")
        
        if not category:
            category = self._get_category_from_path(first_info.item_path, "TV", False)
        
        if category:
            message_texts.append(f"📂 分类：{category}")

        episodes_str = self._merge_continuous_episodes(events_info)
        message_texts.append(f"📺 季集：{episodes_str}")

        self._append_meta_info(message_texts, tmdb_info)
        self._append_genres_actors(message_texts, tmdb_info)

        overview = ""
        if tmdb_info and tmdb_info.overview: overview = tmdb_info.overview
        elif first_info.overview: overview = first_info.overview
        
        if overview:
            if len(overview) > self._overview_max_length:
                overview = overview[:self._overview_max_length].rstrip() + "..."
            message_texts.append("\n━━━━━━━━━━━━━━━━━━\n") 
            message_texts.append(f"📖 剧情简介\n{overview}")

        image_url = first_info.image_url
        if not image_url and tmdb_id:
            image_url = self._get_tmdb_image(first_info, MediaType.TV)
        if not image_url:
            image_url = self._webhook_images.get(first_info.channel)
        
        play_link = self._get_play_link(first_info)

        self.post_message(
            mtype=NotificationType.MediaServer,
            title=message_title,
            text="\n" + "\n".join(message_texts),
            image=image_url,
            link=play_link
        )

    # === 集数合并逻辑 ===
    def _merge_continuous_episodes(self, events: List[WebhookEventInfo]) -> str:
        season_episodes = {}
        for event in events:
            season, episode = None, None
            episode_name = ""
            if event.json_object and isinstance(event.json_object, dict):
                item = event.json_object.get("Item", {})
                season = item.get("ParentIndexNumber")
                episode = item.get("IndexNumber")
                episode_name = item.get("Name", "")
            
            if season is None: season = getattr(event, "season_id", None)
            if episode is None: episode = getattr(event, "episode_id", None)
            if not episode_name: episode_name = getattr(event, "item_name", "")

            if season is not None and episode is not None:
                if season not in season_episodes: season_episodes[season] = []
                season_episodes[season].append({"episode": int(episode), "name": episode_name})

        merged_details = []
        for season in sorted(season_episodes.keys()):
            episodes = season_episodes[season]
            episodes.sort(key=lambda x: x["episode"])
            if not episodes: continue

            start = episodes[0]["episode"]
            end = episodes[0]["episode"]
            
            for i in range(1, len(episodes)):
                current = episodes[i]["episode"]
                if current == end + 1:
                    end = current
                else:
                    merged_details.append(f"S{str(season).zfill(2)}E{str(start).zfill(2)}-E{str(end).zfill(2)}" if start != end else f"S{str(season).zfill(2)}E{str(start).zfill(2)}")
                    start = end = current
            
            merged_details.append(f"S{str(season).zfill(2)}E{str(start).zfill(2)}-E{str(end).zfill(2)}" if start != end else f"S{str(season).zfill(2)}E{str(start).zfill(2)}")
        
        return ", ".join(merged_details)

    def _extract_tmdb_id(self, event_info: WebhookEventInfo) -> Optional[str]:
        tmdb_id = event_info.tmdb_id
        if not tmdb_id and event_info.json_object:
            provider_ids = event_info.json_object.get('Item', {}).get('ProviderIds', {})
            tmdb_id = provider_ids.get('Tmdb')
        
        if not tmdb_id and event_info.item_path:
            if match := re.search(r'[\[{](?:tmdbid|tmdb)[=-](\d+)[\]}]', event_info.item_path, re.IGNORECASE):
                tmdb_id = match.group(1)

        if not tmdb_id and event_info.json_object:
            item_data = event_info.json_object.get('Item', {})
            series_id = item_data.get('SeriesId')
            if series_id and item_data.get('Type') == 'Episode':
                try:
                    service = self.service_info(event_info.server_name)
                    if service:
                        host = service.config.config.get('host')
                        apikey = service.config.config.get('apikey')
                        if host and apikey:
                            import requests
                            api_url = f"{host}/emby/Items?Ids={series_id}&Fields=ProviderIds&api_key={apikey}"
                            res = requests.get(api_url, timeout=5)
                            if res.status_code == 200:
                                data = res.json()
                                if data and data.get('Items'):
                                    parent_ids = data['Items'][0].get('ProviderIds', {})
                                    tmdb_id = parent_ids.get('Tmdb')
                except Exception: pass
        return tmdb_id

    def _get_server_name_cn(self, event_info):
        server_name = ""
        if event_info.json_object and isinstance(event_info.json_object.get('Server'), dict):
            server_name = event_info.json_object.get('Server', {}).get('Name')
        if not server_name:
            server_name = event_info.server_name or "Emby"
        if not server_name.lower().endswith("emby"):
            server_name += "Emby"
        return server_name

    def _get_audio_image_url(self, server_name: str, item_data: dict) -> Optional[str]:
        if not server_name: return None
        try:
            service = self.service_info(server_name)
            if not service or not service.instance: return None
            play_url = service.instance.get_play_url("dummy")
            if not play_url: return None
            parsed = urllib.parse.urlparse(play_url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            item_id = item_data.get('Id')
            primary_tag = item_data.get('ImageTags', {}).get('Primary')
            if not primary_tag:
                item_id = item_data.get('PrimaryImageItemId')
                primary_tag = item_data.get('PrimaryImageTag')
            if item_id and primary_tag:
                return f"{base_url}/emby/Items/{item_id}/Images/Primary?maxHeight=450&maxWidth=450&tag={primary_tag}&quality=90"
        except: pass
        return None

    def _get_tmdb_image(self, event_info: WebhookEventInfo, mtype: MediaType) -> Optional[str]:
        key = f"{event_info.tmdb_id}_{event_info.season_id}_{event_info.episode_id}"
        if key in self._image_cache: return self._image_cache[key]
        try:
            img = self.chain.obtain_specific_image(
                mediaid=event_info.tmdb_id, mtype=mtype, 
                image_type=MediaImageType.Backdrop, 
                season=event_info.season_id, episode=event_info.episode_id
            )
            if not img:
                img = self.chain.obtain_specific_image(
                    mediaid=event_info.tmdb_id, mtype=mtype, 
                    image_type=MediaImageType.Poster, 
                    season=event_info.season_id, episode=event_info.episode_id
                )
            if img:
                if len(self._image_cache) > 100: self._image_cache.pop(next(iter(self._image_cache)))
                self._image_cache[key] = img
                return img
        except: pass
        return None

    def _get_category_from_path(self, path: str, item_type: str, is_folder: bool = False) -> str:
        if not path: return ""
        try:
            path = os.path.normpath(path)
            if is_folder and item_type in ["TV", "SHOW"]:
                return os.path.basename(os.path.dirname(path))
            current_dir = os.path.dirname(path)
            dir_name = os.path.basename(current_dir)
            if re.search(r'^(Season|季|S\d)', dir_name, re.IGNORECASE):
                current_dir = os.path.dirname(current_dir)
            category_dir = os.path.dirname(current_dir)
            category = os.path.basename(category_dir)
            if not category or category == os.path.sep: return ""
            return category
        except: return ""

    def _handle_music_album(self, event_info: WebhookEventInfo, item_data: dict):
        try:
            album_name = item_data.get('Name', '')
            album_id = item_data.get('Id', '')
            album_artist = (item_data.get('Artists') or ['未知艺术家'])[0]
            primary_image_item_id = item_data.get('PrimaryImageItemId') or album_id
            primary_image_tag = item_data.get('PrimaryImageTag') or item_data.get('ImageTags', {}).get('Primary')

            service = self.service_info(event_info.server_name)
            if not service or not service.instance: return
            base_url = service.config.config.get('host', '')
            api_key = service.config.config.get('apikey', '')

            import requests
            fields = "Path,MediaStreams,Container,Size,RunTimeTicks,ImageTags,ProviderIds"
            api_url = f"{base_url}/emby/Items?ParentId={album_id}&Fields={fields}&api_key={api_key}"
            
            res = requests.get(api_url, timeout=10)
            if res.status_code == 200:
                items = res.json().get('Items', [])
                logger.info(f"专辑 [{album_name}] 包含 {len(items)} 首歌曲")
                for song in items:
                    self._send_single_audio_notify(
                        song, album_name, album_artist, 
                        primary_image_item_id, primary_image_tag, 
                        base_url
                    )
        except Exception as e:
            logger.error(f"处理音乐专辑失败: {e}")

    def _send_single_audio_notify(self, song: dict, album_name, album_artist, 
                                  cover_item_id, cover_tag, base_url):
        try:
            song_name = song.get('Name', '未知歌曲')
            song_id = song.get('Id')
            artist = (song.get('Artists') or [album_artist])[0]
            duration = self._format_ticks(song.get('RunTimeTicks', 0))
            container = song.get('Container', '').upper()
            size = self._format_size(song.get('Size', 0))

            title = f"🎵 新入库媒体：{song_name}"
            texts = []
            
            texts.append(f"⏰ 入库：{time.strftime('%H:%M:%S', time.localtime())}")
            texts.append(f"👤 歌手：{artist}")
            if album_name: texts.append(f"💿 专辑：{album_name}")
            texts.append(f"⏱️ 时长：{duration}")
            texts.append(f"📦 格式：{container} · {size}")

            image_url = None
            if cover_item_id and cover_tag:
                 image_url = f"{base_url}/emby/Items/{cover_item_id}/Images/Primary?maxHeight=450&maxWidth=450&tag={cover_tag}&quality=90"

            link = None
            if self._add_play_link:
                link = f"{base_url}/web/index.html#!/item?id={song_id}&serverId={song.get('ServerId', '')}"

            self.post_message(
                mtype=NotificationType.MediaServer,
                title=title,
                text="\n" + "\n".join(texts),
                image=image_url,
                link=link
            )
        except Exception as e:
            logger.error(f"发送单曲通知失败: {e}")

    def _append_meta_info(self, texts: List[str], tmdb_info):
        if not tmdb_info: return
        if hasattr(tmdb_info, 'vote_average') and tmdb_info.vote_average:
            texts.append(f"⭐️ 评分：{round(float(tmdb_info.vote_average), 1)}/10")
        
        region = self._get_region_text_cn(tmdb_info)

    def _get_region_text_cn(self, tmdb_info) -> str:
        if not tmdb_info: return ""
        try:
            codes = []
            if hasattr(tmdb_info, 'origin_country') and tmdb_info.origin_country:
                codes = tmdb_info.origin_country[:2]
            elif hasattr(tmdb_info, 'production_countries') and tmdb_info.production_countries:
                for c in tmdb_info.production_countries[:2]:
                    if isinstance(c, dict): code = c.get('iso_3166_1')
                    else: code = getattr(c, 'iso_3166_1', str(c))
                    if code: codes.append(code)
            if not codes: return ""
            cn_names = [self._country_cn_map.get(code.upper(), code) for code in codes]
            return "、".join(cn_names)
        except: return ""

    def _append_genres_actors(self, texts: List[str], tmdb_info):
        if not tmdb_info: return
        if hasattr(tmdb_info, 'genres') and tmdb_info.genres:
            genres = [g.get('name') if isinstance(g, dict) else str(g) for g in tmdb_info.genres[:3]]
        
        if hasattr(tmdb_info, 'actors') and tmdb_info.actors:
            actors = [a.get('name') if isinstance(a, dict) else str(a) for a in tmdb_info.actors[:3]]
            if actors: texts.append(f"🎬 演员：{'、'.join(actors)}")

    def _append_season_episode_info(self, texts: List[str], event_info: WebhookEventInfo, series_name: str):
        if event_info.season_id is not None and event_info.episode_id is not None:
            s_str, e_str = str(event_info.season_id).zfill(2), str(event_info.episode_id).zfill(2)
            info = f"📺 季集：S{s_str}E{e_str}"
            ep_name = event_info.json_object.get('Item', {}).get('Name')
            if ep_name and ep_name != series_name: info += f" - {ep_name}"
            texts.append(info)
        elif description := event_info.json_object.get('Description'):
            first_line = description.split('\n\n')[0].strip()
            if re.search(r'S\d+\s+E\d+', first_line):
                 texts.append(f"📺 季集：{first_line}")

    def _append_extra_info(self, texts: List[str], event_info: WebhookEventInfo):
        extras = []
        if event_info.user_name: extras.append(f"👤 用户：{event_info.user_name}")
        if event_info.device_name: extras.append(f"📱 设备：{event_info.client} {event_info.device_name}")
        if event_info.ip: extras.append(f"🌐 IP：{event_info.ip} {WebUtils.get_location(event_info.ip)}")
        if event_info.percentage: extras.append(f"📊 进度：{round(float(event_info.percentage), 2)}%")
        if extras: texts.extend(extras)

    def _get_play_link(self, event_info: WebhookEventInfo) -> Optional[str]:
        if not self._add_play_link or not event_info.server_name: return None
        service = self.service_info(event_info.server_name)
        return service.instance.get_play_url(event_info.item_id) if service else None

    def _format_ticks(self, ticks) -> str:
        if not ticks: return "00:00"
        s = ticks / 10000000
        return f"{int(s // 60)}:{int(s % 60):02d}"

    def _format_size(self, size) -> str:
        if not size: return "0MB"
        return f"{round(size / 1024 / 1024, 1)} MB"

    def _add_key_cache(self, key):
        """添加元素到过期字典中"""
        self._webhook_msg_keys[key] = time.time() + self.DEFAULT_EXPIRATION_TIME

    def _remove_key_cache(self, key):
        """从过期字典中移除指定元素"""
        if key in self._webhook_msg_keys: 
            del self._webhook_msg_keys[key]

    def _clean_expired_cache(self):
        """清理过期的缓存元素"""
        current_time = time.time()
        expired_keys = [k for k, v in self._webhook_msg_keys.items() if v <= current_time]
        for key in expired_keys:
            self._webhook_msg_keys.pop(key, None)

    @cached(
        region="MediaServerMsgAI",
        maxsize=128,
        ttl=600,
        skip_none=True,
        skip_empty=False
    )
    def _get_tmdb_info(self, tmdb_id: str, mtype: MediaType, season: Optional[int] = None):
        """
        获取TMDB信息（带缓存）

        Args:
            tmdb_id: TMDB ID
            mtype: 媒体类型
            season: 季数（仅电视剧需要）

        Returns:
            dict: TMDB信息
        """
        if mtype == MediaType.MOVIE:
            return self.chain.tmdb_info(tmdbid=tmdb_id, mtype=mtype)
        else:
            tmdb_info = self.chain.tmdb_info(tmdbid=tmdb_id, mtype=mtype, season=season)
            tmdb_info2 = self.chain.tmdb_info(tmdbid=tmdb_id, mtype=mtype)
            if tmdb_info and tmdb_info2:
                return {**tmdb_info2, **tmdb_info}
            return tmdb_info or tmdb_info2

    def stop_service(self):
        """
        退出插件时的清理工作

        确保：
        1. 所有待处理的聚合消息被立即发送
        2. 所有定时器被取消
        3. 清空所有内部缓存数据
        """
        try:
            # 发送所有待处理的聚合消息
            for series_id in list(self._pending_messages.keys()):
                try:
                    self._send_aggregated_message(series_id)
                except Exception as e:
                    logger.error(f"发送聚合消息时出错: {str(e)}")
            
            # 取消所有定时器
            for timer in self._aggregate_timers.values():
                try:
                    timer.cancel()
                except Exception as e:
                    logger.debug(f"取消定时器时出错: {str(e)}")
            
            self._aggregate_timers.clear()
            self._pending_messages.clear()
            self._webhook_msg_keys.clear()
            self._image_cache.clear()

            # 清理TMDB缓存
            try:
                self._get_tmdb_info.cache_clear()
            except Exception as e:
                logger.debug(f"清理TMDB缓存时出错: {str(e)}")
        except Exception as e:
            logger.error(f"插件停止时发生错误: {str(e)}")