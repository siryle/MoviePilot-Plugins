import re
import time
import traceback
import threading
import os
import urllib.parse
from collections import OrderedDict
from typing import Any, List, Dict, Tuple, Optional
from enum import Enum

from app.core.cache import cached
from app.core.event import eventmanager, Event
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.modules.themoviedb import CategoryHelper
from app.plugins import _PluginBase
from app.schemas import WebhookEventInfo, ServiceInfo
from app.schemas.types import EventType, MediaType, MediaImageType, NotificationType
from app.utils.web import WebUtils


class MessageType(Enum):
    """消息类型枚举"""
    TEST = "test"
    LOGIN = "login"
    RATING = "rating"
    MUSIC = "music"
    TV_AGGREGATE = "tv_aggregate"
    MEDIA_EVENT = "media_event"
    SKIPPED = "skipped"


class MediaServerMsgAI(_PluginBase):
    """
    媒体服务器通知插件 AI增强版
    
    功能特点：
    1. 支持多服务器：Emby/Jellyfin/Plex
    2. TMDB元数据增强（评分、分类、演员等）
    3. TV剧集智能聚合，避免消息轰炸
    4. 可配置跳过TMDB未识别的视频
    5. 支持音乐专辑和单曲通知
    6. 丰富的消息模板和图片缓存
    """

    # ==================== 常量定义 ====================
    DEFAULT_EXPIRATION_TIME = 600              # 消息去重过期时间（秒）
    DEFAULT_AGGREGATE_TIME = 15                # 剧集聚合时间窗口（秒）
    DEFAULT_OVERVIEW_MAX_LENGTH = 150          # 简介最大长度
    IMAGE_CACHE_MAX_SIZE = 100                 # 图片缓存最大数量
    MAX_AGGREGATE_TIME = 300                   # 最大聚合时间
    MIN_OVERVIEW_LENGTH = 50                   # 最小简介长度

    # ==================== 插件基本信息 ====================
    plugin_name = "媒体服务器通知AI版"
    plugin_desc = "智能媒体服务器通知：TMDB元数据增强+剧集聚合+未识别过滤"
    plugin_icon = "mediaplay.png"
    plugin_version = "1.8.0"
    plugin_author = "jxxghp"
    author_url = "https://github.com/jxxghp"
    plugin_config_prefix = "mediaservermsgai_"
    plugin_order = 14
    auth_level = 1

    # ==================== 运行时配置 ====================
    def __init__(self):
        """初始化插件实例"""
        super().__init__()
        self._init_config()
        self._init_state()
        self.category = CategoryHelper()
        logger.info(f"{self.plugin_name} v{self.plugin_version} 初始化完成")

    def _init_config(self):
        """初始化配置相关变量"""
        self._enabled = False
        self._add_play_link = False
        self._skip_unrecognized = True
        self._aggregate_enabled = False
        self._smart_category_enabled = True
        self._mediaservers = []
        self._types = []
        self._aggregate_time = self.DEFAULT_AGGREGATE_TIME
        self._overview_max_length = self.DEFAULT_OVERVIEW_MAX_LENGTH

    def _init_state(self):
        """初始化运行时状态"""
        self._webhook_msg_keys = {}
        self._lock = threading.Lock()
        self._last_event_cache = (None, 0.0)
        self._image_cache = OrderedDict()
        self._pending_messages = {}
        self._aggregate_timers = {}
        
        # 统计信息
        self._metrics = {
            "start_time": time.time(),
            "messages_processed": 0,
            "messages_sent": 0,
            "messages_skipped": 0,
            "messages_aggregated": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "errors": 0,
            "by_type": {msg_type.value: 0 for msg_type in MessageType}
        }

    # ==================== 配置映射 ====================
    _WEBHOOK_ACTIONS = {
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
    
    _SERVER_IMAGES = {
        "emby": "https://raw.githubusercontent.com/qqcomeup/MoviePilot-Plugins/bb3ca257f74cf000640f9ebadab257bb0850baac/icons/11-11.jpg",
        "plex": "https://raw.githubusercontent.com/qqcomeup/MoviePilot-Plugins/bb3ca257f74cf000640f9ebadab257bb0850baac/icons/11-11.jpg",
        "jellyfin": "https://raw.githubusercontent.com/qqcomeup/MoviePilot-Plugins/bb3ca257f74cf000640f9ebadab257bb0850baac/icons/11-11.jpg"
    }

    _COUNTRY_CN_MAP = {
        'CN': '中国大陆', 'US': '美国', 'JP': '日本', 'KR': '韩国',
        'HK': '中国香港', 'TW': '中国台湾', 'GB': '英国', 'FR': '法国',
        'DE': '德国', 'IT': '意大利', 'ES': '西班牙', 'IN': '印度',
        'TH': '泰国', 'RU': '俄罗斯', 'CA': '加拿大', 'AU': '澳大利亚',
        'SG': '新加坡', 'MY': '马来西亚', 'VN': '越南', 'PH': '菲律宾',
        'ID': '印度尼西亚', 'BR': '巴西', 'MX': '墨西哥', 'AR': '阿根廷',
        'NL': '荷兰', 'BE': '比利时', 'SE': '瑞典', 'DK': '丹麦',
        'NO': '挪威', 'FI': '芬兰', 'PL': '波兰', 'TR': '土耳其'
    }

    # ==================== 核心方法 ====================
    def init_plugin(self, config: dict = None):
        """初始化插件配置"""
        if not config:
            return
            
        try:
            # 基础配置
            self._enabled = config.get("enabled", False)
            self._types = config.get("types") or []
            self._mediaservers = config.get("mediaservers") or []
            
            # 功能配置
            self._add_play_link = config.get("add_play_link", False)
            self._skip_unrecognized = config.get("skip_unrecognized", True)
            self._aggregate_enabled = config.get("aggregate_enabled", False)
            self._smart_category_enabled = config.get("smart_category_enabled", True)
            
            # 数值配置（带边界检查）
            self._aggregate_time = self._clamp_value(
                config.get("aggregate_time", self.DEFAULT_AGGREGATE_TIME),
                1, self.MAX_AGGREGATE_TIME
            )
            
            self._overview_max_length = self._clamp_value(
                config.get("overview_max_length", self.DEFAULT_OVERVIEW_MAX_LENGTH),
                self.MIN_OVERVIEW_LENGTH, 500
            )
            
            logger.info(
                f"插件配置加载: 启用={self._enabled}, "
                f"跳过未识别={self._skip_unrecognized}, "
                f"服务器={len(self._mediaservers)}个"
            )
            
        except Exception as e:
            logger.error(f"配置初始化失败: {str(e)}")
            raise

    def get_state(self) -> bool:
        """获取插件启用状态"""
        return self._enabled

    # ==================== 配置页面 ====================
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """生成插件配置页面"""
        # 消息类型选项
        types_options = [
            {"title": "新入库", "value": "library.new"},
            {"title": "开始播放", "value": "playback.start|media.play|PlaybackStart"},
            {"title": "停止播放", "value": "playback.stop|media.stop|PlaybackStop"},
            {"title": "暂停/继续", "value": "playback.pause|playback.unpause|media.pause|media.resume"},
            {"title": "用户标记", "value": "item.rate|item.markplayed|item.markunplayed"},
            {"title": "登录提醒", "value": "user.authenticated|user.authenticationfailed"},
            {"title": "系统测试", "value": "system.webhooktest|system.notificationtest"},
        ]
        
        # 服务器选项
        server_configs = MediaServerHelper().get_configs()
        server_items = [
            {"title": f"{config.name} ({config.server})", "value": config.name} 
            for config in server_configs.values()
        ]
        
        form_config = [
            {
                'component': 'VForm',
                'content': [
                    # 基础开关
                    {
                        'component': 'VRow', 
                        'content': [
                            self._create_switch("enabled", "启用插件", 4, "启用后开始接收媒体服务器通知"),
                            self._create_switch("add_play_link", "添加播放链接", 4, "在消息中添加媒体播放链接"),
                            self._create_switch("skip_unrecognized", "跳过未识别视频", 4, "TMDB未识别的电影/剧集不发送通知"),
                        ]
                    },
                    
                    # 服务器选择
                    {
                        'component': 'VRow', 
                        'content': [
                            self._create_select("mediaservers", "媒体服务器", server_items, True)
                        ]
                    },
                    
                    # 消息类型选择
                    {
                        'component': 'VRow', 
                        'content': [
                            self._create_select("types", "消息类型", types_options, True)
                        ]
                    },
                    
                    # 高级功能
                    {
                        'component': 'VRow',
                        'content': [
                            self._create_switch("aggregate_enabled", "TV剧集聚合", 6, "启用后会将短时间内入库的同一剧集合并通知"),
                            self._create_switch("smart_category_enabled", "智能分类", 6, "使用TMDB数据进行智能分类"),
                        ]
                    },
                    
                    # 聚合设置（条件显示）
                    {
                        'component': 'VRow',
                        'props': {'show': '{{aggregate_enabled}}'},
                        'content': [
                            self._create_text_field("aggregate_time", "聚合等待时间（秒）", 6, "15", "等待多少秒内入库的剧集进行聚合"),
                            self._create_text_field("overview_max_length", "简介最大长度", 6, "150", "简介文本的最大显示长度"),
                        ]
                    }
                ]
            }
        ]
        
        default_values = {
            "enabled": False, 
            "types": [], 
            "mediaservers": [],
            "aggregate_enabled": False, 
            "aggregate_time": self.DEFAULT_AGGREGATE_TIME,
            "smart_category_enabled": True,
            "overview_max_length": self.DEFAULT_OVERVIEW_MAX_LENGTH,
            "skip_unrecognized": True
        }
        
        return form_config, default_values

    # ==================== 事件处理主入口 ====================
    @eventmanager.register(EventType.WebhookMessage)
    def send(self, event: Event):
        """处理Webhook事件主入口"""
        self._metrics["messages_processed"] += 1
        
        try:
            # 前置检查
            if not self._should_process_event(event):
                return
                
            event_info = event.event_data
            event_type = str(event_info.event).lower()
            
            # 记录事件详情
            logger.debug(f"处理事件: {event_type}, 项目: {event_info.item_name}")
            
            # 路由到对应的处理器
            handler_result = self._route_event(event_info, event_type, event)
            
            if handler_result == MessageType.SKIPPED:
                self._metrics["by_type"][MessageType.SKIPPED.value] += 1
                self._metrics["messages_skipped"] += 1
                
        except Exception as e:
            logger.error(f"事件处理异常: {str(e)}")
            logger.error(traceback.format_exc())
            self._metrics["errors"] += 1

    def _should_process_event(self, event: Event) -> bool:
        """检查是否应该处理事件"""
        if not self._enabled:
            return False
            
        event_info = event.event_data
        if not event_info:
            logger.debug("事件数据为空")
            return False
            
        # 检查事件类型
        if not self._WEBHOOK_ACTIONS.get(event_info.event):
            logger.debug(f"未知事件类型: {event_info.event}")
            return False
            
        # 检查是否启用该类型
        if not self._is_event_type_enabled(event_info.event):
            logger.debug(f"未启用 {event_info.event} 类型的通知")
            return False
            
        # 检查服务器配置
        if event_info.server_name and not self.service_info(event_info.server_name):
            logger.debug(f"未配置服务器: {event_info.server_name}")
            return False
            
        return True

    def _route_event(self, event_info, event_type: str, event: Event) -> MessageType:
        """路由事件到对应的处理器"""
        # 1. 系统测试
        if "test" in event_type:
            self._handle_test_event(event_info)
            return MessageType.TEST
            
        # 2. 用户登录
        if "user.authentic" in event_type:
            self._handle_login_event(event_info)
            return MessageType.LOGIN
            
        # 3. 评分标记
        if "item." in event_type and ("rate" in event_type or "mark" in event_type):
            self._handle_rate_event(event_info)
            return MessageType.RATING
            
        # 4. 音乐专辑
        if (event_info.json_object and 
            event_info.json_object.get('Item', {}).get('Type') == 'MusicAlbum' and 
            event_type == 'library.new'):
            self._handle_music_album(event_info, event_info.json_object.get('Item', {}))
            return MessageType.MUSIC
            
        # 5. TV剧集聚合处理
        if (self._aggregate_enabled and 
            event_type == "library.new" and 
            event_info.item_type in ["TV", "SHOW"]):
            
            series_id = self._get_series_id(event_info)
            if series_id:
                self._aggregate_tv_episodes(series_id, event_info, event)
                return MessageType.TV_AGGREGATE
                
        # 6. 常规媒体事件（包含未识别检查）
        return self._process_media_event_with_check(event, event_info)

    # ==================== 事件处理器 ====================
    def _handle_test_event(self, event_info):
        """处理测试事件"""
        server_name = self._get_server_display_name(event_info)
        
        texts = [
            f"来自：{server_name}",
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"状态：连接正常"
        ]
        
        if event_info.user_name:
            texts.append(f"用户：{event_info.user_name}")
            
        self._send_message(
            title="🔔 媒体服务器测试",
            texts=texts,
            image=self._SERVER_IMAGES.get(event_info.channel),
            message_type=MessageType.TEST
        )

    def _handle_login_event(self, event_info):
        """处理登录事件"""
        is_success = "authenticated" in event_info.event and "failed" not in event_info.event
        action = "登录成功" if is_success else "登录失败"
        
        texts = [
            f"👤 用户：{event_info.user_name}",
            f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        
        if event_info.device_name:
            texts.append(f"📱 设备：{event_info.client} {event_info.device_name}")
            
        if event_info.ip:
            location = self._get_ip_location(event_info.ip)
            texts.append(f"🌐 IP：{event_info.ip} {location}")
            
        server_name = self._get_server_display_name(event_info)
        texts.append(f"🖥️ 服务器：{server_name}")

        self._send_message(
            title=f"🔐 {action}提醒",
            texts=texts,
            image=self._SERVER_IMAGES.get(event_info.channel),
            message_type=MessageType.LOGIN
        )

    def _handle_rate_event(self, event_info):
        """处理评分标记事件"""
        item_name = event_info.item_name
        action_text = self._WEBHOOK_ACTIONS.get(event_info.event, "已标记")
        
        texts = [
            f"👤 用户：{event_info.user_name}",
            f"🏷️ 标记：{action_text}",
            f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        
        # 获取图片
        tmdb_id = self._extract_tmdb_id(event_info)
        image_url = event_info.image_url
        if not image_url and tmdb_id:
            mtype = MediaType.MOVIE if event_info.item_type == "MOV" else MediaType.TV
            image_url = self._get_tmdb_image(event_info, mtype)

        self._send_message(
            title=f"⭐ 用户评分：{item_name}",
            texts=texts,
            image=image_url or self._SERVER_IMAGES.get(event_info.channel),
            message_type=MessageType.RATING
        )

    def _process_media_event_with_check(self, event: Event, event_info) -> MessageType:
        """处理常规媒体事件（包含未识别检查）"""
        # 检查是否需要跳过未识别视频
        if self._should_skip_unrecognized(event_info):
            logger.info(f"跳过TMDB未识别的视频: {event_info.item_name}")
            return MessageType.SKIPPED
            
        # 处理媒体事件
        self._process_media_event(event, event_info)
        return MessageType.MEDIA_EVENT

    def _should_skip_unrecognized(self, event_info) -> bool:
        """检查是否应该跳过未识别的视频"""
        if not self._skip_unrecognized:
            return False
            
        # 只检查入库的视频
        if event_info.event != "library.new":
            return False
            
        # 只检查电影和电视剧
        if event_info.item_type not in ["MOV", "TV", "SHOW"]:
            return False
            
        # 尝试获取TMDB ID
        tmdb_id = self._extract_tmdb_id(event_info)
        if tmdb_id:
            return False  # 有TMDB ID，不跳过
            
        # 尝试识别
        if event_info.item_type == "MOV":
            mtype = MediaType.MOVIE
        else:
            mtype = MediaType.TV
            
        tmdb_info = self._try_recognize_media(event_info, mtype)
        
        # 如果没有识别到有效的TMDB信息，则跳过
        return not tmdb_info or not getattr(tmdb_info, 'id', None)

    # ==================== 媒体识别与处理 ====================
    def _try_recognize_media(self, event_info, mtype: MediaType):
        """尝试识别媒体信息"""
        try:
            # 清理媒体名称
            clean_name = self._clean_media_name(event_info.item_name)
            if not clean_name:
                return None
                
            logger.debug(f"尝试识别媒体: {clean_name} ({mtype})")
            
            # 使用chain进行识别
            tmdb_info = self.chain.recognize_by_name(clean_name, mtype)
            
            if tmdb_info and hasattr(tmdb_info, 'id') and tmdb_info.id:
                logger.info(f"识别成功: {clean_name} -> {tmdb_info.title or tmdb_info.name}")
                return tmdb_info
                
            return None
            
        except Exception as e:
            logger.error(f"媒体识别失败: {str(e)}")
            return None

    def _clean_media_name(self, name: str) -> str:
        """清理媒体名称"""
        if not name:
            return ""
            
        # 定义清理规则
        patterns = [
            # 年份和质量信息
            r'\s*[\(\[]?\d{4}[\)\]]?',
            r'\s*[\(\[]?(?:19|20)\d{2}[\)\]]?',
            
            # 视频质量
            r'\s*[\(\[]?(?:1080p|720p|2160p|4K|UHD|HD)[\)\]]?',
            
            # 来源格式
            r'\s*[\(\[]?(?:BluRay|Blu-ray|BD|BDrip|BDRip)[\)\]]?',
            r'\s*[\(\[]?(?:WEB-DL|WEBRip|WEB|HDTV|HDTVRip)[\)\]]?',
            r'\s*[\(\[]?(?:DVD|DVDRip|REMUX)[\)\]]?',
            
            # 编码格式
            r'\s*[\(\[]?(?:H\.?264|H\.?265|HEVC|AVC|x264|x265)[\)\]]?',
            
            # 音频格式
            r'\s*[\(\[]?(?:AAC|AC3|DTS|DDP5\.1|Atmos)[\)\]]?',
            
            # 字幕信息
            r'\s*[\(\[]?(?:CHS|CHT|简繁|简中|繁中)[\)\]]?',
            
            # 文件格式
            r'\s*[\(\[]?(?:MP4|MKV|AVI)[\)\]]?',
            
            # 季集信息
            r'\s*[\(\[]?(?:S\d{2}|Season\s*\d+|第\s*\d+\s*季)[\)\]]?',
            r'\s*[\(\[]?(?:E\d{2}|Episode\s*\d+|第\s*\d+\s*集)[\)\]]?',
            
            # 其他信息
            r'\s*[\(\[]?(?:Complete|Complete Series|全集|Extended|Director\'s Cut)[\)\]]?',
            
            # 特殊字符和空格
            r'^\s+|\s+$',
            r'\s+',
        ]
        
        cleaned = name
        for pattern in patterns:
            cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)
            
        cleaned = cleaned.strip()
        return cleaned if cleaned else name

    def _process_media_event(self, event: Event, event_info):
        """处理常规媒体事件"""
        # 防重复检查
        if not self._check_duplicate_event(event, event_info):
            return
            
        # 构建消息
        message_data = self._build_media_message(event_info)
        if not message_data:
            return
            
        # 发送消息
        self._send_message(
            title=message_data["title"],
            texts=message_data["texts"],
            image=message_data["image_url"],
            link=message_data["play_link"],
            message_type=MessageType.MEDIA_EVENT
        )
        
        # 更新缓存
        self._update_event_cache(event, event_info)

    def _build_media_message(self, event_info):
        """构建媒体消息"""
        # 提取基础信息
        tmdb_id = self._extract_tmdb_id(event_info)
        event_info.tmdb_id = tmdb_id
        
        # 音频处理
        if event_info.item_type == "AUD":
            return self._build_audio_message(event_info)
            
        # 视频处理
        return self._build_video_message(event_info, tmdb_id)

    def _build_audio_message(self, event_info):
        """构建音频消息"""
        item_data = event_info.json_object.get('Item', {}) if event_info.json_object else {}
        
        # 基本信息
        song_name = item_data.get('Name') or event_info.item_name
        artist = (item_data.get('Artists') or ['未知歌手'])[0]
        album = item_data.get('Album', '')
        duration = self._format_duration(item_data.get('RunTimeTicks', 0))
        container = item_data.get('Container', '').upper()
        size = self._format_size(item_data.get('Size', 0))
        
        # 构建文本
        texts = [
            f"⏰ 时间：{time.strftime('%H:%M:%S', time.localtime())}",
            f"👤 歌手：{artist}",
        ]
        
        if album:
            texts.append(f"💿 专辑：{album}")
            
        texts.extend([
            f"⏱️ 时长：{duration}",
            f"📦 格式：{container} · {size}"
        ])
        
        # 获取图片
        image_url = self._get_audio_image_url(event_info.server_name, item_data)
        
        # 播放链接
        play_link = self._get_play_link(event_info) if self._add_play_link else None
        
        return {
            "title": f"🎵 新入库：{song_name}",
            "texts": texts,
            "image_url": image_url or self._SERVER_IMAGES.get(event_info.channel),
            "play_link": play_link
        }

    def _build_video_message(self, event_info, tmdb_id):
        """构建视频消息"""
        # 获取TMDB信息
        tmdb_info = None
        if tmdb_id:
            mtype = MediaType.MOVIE if event_info.item_type == "MOV" else MediaType.TV
            tmdb_info = self._get_tmdb_info_cached(tmdb_id, mtype)
        
        # 构建标题
        title_name = self._get_media_title(event_info, tmdb_info)
        action_text = self._WEBHOOK_ACTIONS.get(event_info.event, "通知")
        title = f"🆕 {title_name} {action_text}"
        
        # 构建内容
        texts = [
            f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}"
        ]
        
        # 分类信息
        category = self._get_media_category(event_info, tmdb_info)
        if category:
            texts.append(f"📂 分类：{category}")
            
        # 季集信息
        self._append_episode_info(texts, event_info, title_name)
        
        # 元数据信息
        self._append_metadata(texts, tmdb_info)
        
        # 简介
        overview = self._get_media_overview(event_info, tmdb_info)
        if overview:
            texts.append(f"📖 简介：\n{overview}")
            
        # 附加信息
        self._append_extra_info(texts, event_info)
        
        # 获取图片
        image_url = event_info.image_url
        if not image_url and tmdb_id:
            mtype = MediaType.MOVIE if event_info.item_type == "MOV" else MediaType.TV
            image_url = self._get_tmdb_image(event_info, mtype)
            
        # 播放链接
        play_link = self._get_play_link(event_info) if self._add_play_link else None
        
        return {
            "title": title,
            "texts": texts,
            "image_url": image_url or self._SERVER_IMAGES.get(event_info.channel),
            "play_link": play_link
        }

    # ==================== 辅助方法 ====================
    def _get_media_title(self, event_info, tmdb_info):
        """获取媒体标题"""
        title = event_info.item_name
        
        # 电视剧获取系列名
        if (event_info.item_type in ["TV", "SHOW"] and 
            event_info.json_object):
            series_name = event_info.json_object.get('Item', {}).get('SeriesName')
            if series_name:
                title = series_name
                
        # 添加年份
        year = None
        if tmdb_info and tmdb_info.year:
            year = tmdb_info.year
        elif event_info.json_object:
            year = event_info.json_object.get('Item', {}).get('ProductionYear')
            
        if year and str(year) not in title:
            title += f" ({year})"
            
        return title

    def _get_media_category(self, event_info, tmdb_info):
        """获取媒体分类"""
        # 智能分类
        if self._smart_category_enabled and tmdb_info:
            try:
                if event_info.item_type == "MOV":
                    return self.category.get_movie_category(tmdb_info)
                else:
                    return self.category.get_tv_category(tmdb_info)
            except Exception as e:
                logger.debug(f"获取智能分类失败: {str(e)}")
                
        # 路径分类
        is_folder = event_info.json_object.get('Item', {}).get('IsFolder', False) if event_info.json_object else False
        return self._get_category_from_path(event_info.item_path, event_info.item_type, is_folder)

    def _get_media_overview(self, event_info, tmdb_info):
        """获取媒体简介"""
        overview = ""
        if tmdb_info and tmdb_info.overview:
            overview = tmdb_info.overview
        elif event_info.overview:
            overview = event_info.overview
            
        if overview and len(overview) > self._overview_max_length:
            overview = overview[:self._overview_max_length].rstrip() + "..."
            
        return overview

    def _append_metadata(self, texts: List[str], tmdb_info):
        """添加元数据信息"""
        if not tmdb_info:
            return
            
        # 评分
        if hasattr(tmdb_info, 'vote_average') and tmdb_info.vote_average:
            texts.append(f"⭐️ 评分：{round(float(tmdb_info.vote_average), 1)}/10")
            
        # 地区
        region = self._get_region_text_cn(tmdb_info)
        
        # 演员
        if hasattr(tmdb_info, 'actors') and tmdb_info.actors:
            actors = [a.get('name') if isinstance(a, dict) else str(a) for a in tmdb_info.actors[:3]]
            if actors:
                texts.append(f"🎬 演员：{'、'.join(actors)}")

    def _append_episode_info(self, texts: List[str], event_info, series_name: str):
        """添加季集信息"""
        if event_info.season_id is not None and event_info.episode_id is not None:
            s_str = str(event_info.season_id).zfill(2)
            e_str = str(event_info.episode_id).zfill(2)
            info = f"📺 季集：S{s_str}E{e_str}"
            
            ep_name = event_info.json_object.get('Item', {}).get('Name') if event_info.json_object else None
            if ep_name and ep_name != series_name:
                info += f" - {ep_name}"
                
            texts.append(info)

    def _append_extra_info(self, texts: List[str], event_info):
        """添加额外信息"""
        extras = []
        if event_info.user_name:
            extras.append(f"👤 用户：{event_info.user_name}")
        if event_info.device_name:
            extras.append(f"📱 设备：{event_info.client} {event_info.device_name}")
        if event_info.ip:
            location = self._get_ip_location(event_info.ip)
            extras.append(f"🌐 IP：{event_info.ip} {location}")
        if event_info.percentage:
            extras.append(f"📊 进度：{round(float(event_info.percentage), 2)}%")
            
        if extras:
            texts.extend(extras)

    # ==================== 聚合功能 ====================
    def _aggregate_tv_episodes(self, series_id: str, event_info, event: Event):
        """聚合TV剧集"""
        with self._lock:
            # 初始化聚合列表
            if series_id not in self._pending_messages:
                self._pending_messages[series_id] = []
                
            self._pending_messages[series_id].append((event_info, event))
            
            # 重启定时器
            if series_id in self._aggregate_timers:
                self._aggregate_timers[series_id].cancel()
                
            timer = threading.Timer(self._aggregate_time, self._send_aggregated_message, [series_id])
            self._aggregate_timers[series_id] = timer
            timer.start()

    def _send_aggregated_message(self, series_id: str):
        """发送聚合消息"""
        with self._lock:
            if series_id not in self._pending_messages:
                return
                
            msg_list = self._pending_messages.pop(series_id)
            if series_id in self._aggregate_timers:
                del self._aggregate_timers[series_id]

        if not msg_list:
            return
            
        # 单条消息直接处理
        if len(msg_list) == 1:
            self._process_media_event_with_check(msg_list[0][1], msg_list[0][0])
            return
            
        # 检查是否应该跳过未识别的聚合
        first_info = msg_list[0][0]
        if self._should_skip_unrecognized(first_info):
            logger.info(f"跳过TMDB未识别的聚合剧集: {first_info.item_name}")
            self._metrics["messages_skipped"] += 1
            self._metrics["by_type"][MessageType.SKIPPED.value] += 1
            return
            
        # 构建聚合消息
        self._build_aggregated_message(msg_list)
        self._metrics["messages_aggregated"] += 1

    def _build_aggregated_message(self, msg_list):
        """构建聚合消息"""
        first_info = msg_list[0][0]
        events_info = [x[0] for x in msg_list]
        count = len(events_info)
        
        # 获取TMDB信息
        tmdb_id = self._extract_tmdb_id(first_info)
        tmdb_info = None
        if tmdb_id:
            tmdb_info = self._get_tmdb_info_cached(tmdb_id, MediaType.TV)
            
        # 构建标题
        title_name = self._get_media_title(first_info, tmdb_info)
        title = f"🆕 {title_name} 已入库 (含{count}个文件)"
        
        # 构建内容
        texts = [
            f"⏰ {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}"
        ]
        
        # 分类
        category = self._get_media_category(first_info, tmdb_info)
        if category:
            texts.append(f"📂 分类：{category}")
            
        # 季集信息
        episodes_str = self._merge_episodes(events_info)
        texts.append(f"📺 季集：{episodes_str}")
        
        # 元数据
        self._append_metadata(texts, tmdb_info)
        
        # 简介
        overview = self._get_media_overview(first_info, tmdb_info)
        if overview:
            texts.extend(
                f"📖 简介：\n{overview}"
            )
            
        # 获取图片
        image_url = first_info.image_url
        if not image_url and tmdb_id:
            image_url = self._get_tmdb_image(first_info, MediaType.TV)
            
        # 播放链接
        play_link = self._get_play_link(first_info) if self._add_play_link else None
        
        # 发送消息
        self._send_message(
            title=title,
            texts=texts,
            image=image_url or self._SERVER_IMAGES.get(first_info.channel),
            link=play_link,
            message_type=MessageType.TV_AGGREGATE
        )

    def _merge_episodes(self, events: List) -> str:
        """合并连续剧集"""
        season_episodes = {}
        
        for event in events:
            season, episode = None, None
            
            if event.json_object:
                item = event.json_object.get("Item", {})
                season = item.get("ParentIndexNumber")
                episode = item.get("IndexNumber")
                
            if season is None:
                season = getattr(event, "season_id", None)
            if episode is None:
                episode = getattr(event, "episode_id", None)
                
            if season is not None and episode is not None:
                if season not in season_episodes:
                    season_episodes[season] = []
                season_episodes[season].append(int(episode))
                
        # 合并连续集数
        merged_details = []
        for season in sorted(season_episodes.keys()):
            episodes = sorted(set(season_episodes[season]))
            if not episodes:
                continue
                
            ranges = []
            start = episodes[0]
            end = episodes[0]
            
            for ep in episodes[1:]:
                if ep == end + 1:
                    end = ep
                else:
                    if start == end:
                        ranges.append(f"S{str(season).zfill(2)}E{str(start).zfill(2)}")
                    else:
                        ranges.append(f"S{str(season).zfill(2)}E{str(start).zfill(2)}-E{str(end).zfill(2)}")
                    start = end = ep
                    
            if start == end:
                ranges.append(f"S{str(season).zfill(2)}E{str(start).zfill(2)}")
            else:
                ranges.append(f"S{str(season).zfill(2)}E{str(start).zfill(2)}-E{str(end).zfill(2)}")
                
            merged_details.extend(ranges)
            
        return ", ".join(merged_details)

    # ==================== 工具方法 ====================
    def _clamp_value(self, value, min_val, max_val):
        """限制数值范围"""
        try:
            num = int(value)
            return max(min_val, min(num, max_val))
        except (ValueError, TypeError):
            return min_val

    def _create_switch(self, model: str, label: str, cols: int = 6, hint: str = ""):
        """创建开关组件"""
        return {
            'component': 'VCol',
            'props': {'cols': 12, 'md': cols},
            'content': [{
                'component': 'VSwitch',
                'props': {'model': model, 'label': label, 'hint': hint}
            }]
        }

    def _create_select(self, model: str, label: str, items: list, multiple: bool = False):
        """创建选择组件"""
        return {
            'component': 'VCol',
            'props': {'cols': 12},
            'content': [{
                'component': 'VSelect',
                'props': {
                    'model': model, 'label': label, 'items': items,
                    'multiple': multiple, 'chips': multiple, 'clearable': multiple
                }
            }]
        }

    def _create_text_field(self, model: str, label: str, cols: int, placeholder: str, hint: str = ""):
        """创建文本输入组件"""
        return {
            'component': 'VCol',
            'props': {'cols': 12, 'md': cols},
            'content': [{
                'component': 'VTextField',
                'props': {
                    'model': model, 'label': label, 'placeholder': placeholder,
                    'type': 'number', 'hint': hint
                }
            }]
        }

    def _is_event_type_enabled(self, event_type: str) -> bool:
        """检查事件类型是否启用"""
        if not self._types:
            return False
            
        # 将配置的类型展开为集合
        allowed_types = set()
        for _type in self._types:
            allowed_types.update(_type.split("|"))
            
        return event_type in allowed_types

    def _check_duplicate_event(self, event: Event, event_info) -> bool:
        """检查重复事件"""
        # 播放停止事件防重复
        expiring_key = f"{event_info.item_id}-{event_info.client}-{event_info.user_name}-{event_info.event}"
        if event_info.event == "playback.stop" and expiring_key in self._webhook_msg_keys:
            self._add_key_cache(expiring_key)
            return False
            
        # 事件去重
        with self._lock:
            current_time = time.time()
            last_event, last_time = self._last_event_cache
            
            if (last_event and 
                (current_time - last_time < 2) and
                (last_event.event_id == event.event_id or 
                 last_event.event_data == event_info)):
                return False
                
            self._last_event_cache = (event, current_time)
            return True

    def _update_event_cache(self, event: Event, event_info):
        """更新事件缓存"""
        if event_info.event == "playback.stop":
            expiring_key = f"{event_info.item_id}-{event_info.client}-{event_info.user_name}-{event_info.event}"
            self._add_key_cache(expiring_key)
        elif event_info.event == "playback.start":
            expiring_key = f"{event_info.item_id}-{event_info.client}-{event_info.user_name}-{event_info.event}"
            self._remove_key_cache(expiring_key)

    def _add_key_cache(self, key):
        """添加缓存键"""
        self._webhook_msg_keys[key] = time.time() + self.DEFAULT_EXPIRATION_TIME

    def _remove_key_cache(self, key):
        """移除缓存键"""
        self._webhook_msg_keys.pop(key, None)

    def _clean_expired_cache(self):
        """清理过期缓存"""
        current_time = time.time()
        expired_keys = [
            k for k, v in self._webhook_msg_keys.items() 
            if v <= current_time
        ]
        for key in expired_keys:
            self._webhook_msg_keys.pop(key, None)

    def _send_message(self, title: str, texts: List[str], image: str = None, 
                     link: str = None, message_type: MessageType = None):
        """发送消息"""
        self.post_message(
            mtype=NotificationType.MediaServer,
            title=title,
            text="\n" + "\n".join(texts),
            image=image,
            link=link
        )
        
        self._metrics["messages_sent"] += 1
        if message_type:
            self._metrics["by_type"][message_type.value] += 1

    # ==================== 数据获取方法 ====================
    def _extract_tmdb_id(self, event_info) -> Optional[str]:
        """提取TMDB ID"""
        # 从事件数据中提取
        if event_info.tmdb_id:
            return event_info.tmdb_id
            
        # 从JSON对象中提取
        if event_info.json_object:
            provider_ids = event_info.json_object.get('Item', {}).get('ProviderIds', {})
            if provider_ids and provider_ids.get('Tmdb'):
                return provider_ids.get('Tmdb')
                
        # 从路径中提取
        if event_info.item_path:
            match = re.search(r'[\[{](?:tmdbid|tmdb)[=-](\d+)[\]}]', 
                            event_info.item_path, re.IGNORECASE)
            if match:
                return match.group(1)
                
        # 从系列ID中获取（剧集）
        if (event_info.json_object and 
            event_info.json_object.get('Item', {}).get('Type') == 'Episode'):
            return self._get_tmdb_id_from_series(event_info)
            
        return None

    def _get_tmdb_id_from_series(self, event_info):
        """从系列中获取TMDB ID"""
        try:
            series_id = event_info.json_object.get('Item', {}).get('SeriesId')
            if not series_id:
                return None
                
            service = self.service_info(event_info.server_name)
            if not service:
                return None
                
            host = service.config.config.get('host')
            apikey = service.config.config.get('apikey')
            if not host or not apikey:
                return None
                
            import requests
            api_url = f"{host}/emby/Items?Ids={series_id}&Fields=ProviderIds&api_key={apikey}"
            response = requests.get(api_url, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                if data and data.get('Items'):
                    parent_ids = data['Items'][0].get('ProviderIds', {})
                    return parent_ids.get('Tmdb')
                    
        except Exception:
            pass
            
        return None

    def _get_server_display_name(self, event_info):
        """获取服务器显示名称"""
        server_name = ""
        if event_info.json_object and isinstance(event_info.json_object.get('Server'), dict):
            server_name = event_info.json_object.get('Server', {}).get('Name')
            
        if not server_name:
            server_name = event_info.server_name or "Emby"
            
        if not server_name.lower().endswith("emby"):
            server_name += "Emby"
            
        return server_name

    def _get_ip_location(self, ip: str) -> str:
        """获取IP地理位置"""
        try:
            return WebUtils.get_location(ip)
        except Exception:
            return ""

    def _get_tmdb_image(self, event_info, mtype: MediaType) -> Optional[str]:
        """获取TMDB图片"""
        if not event_info.tmdb_id:
            return None
            
        cache_key = f"{event_info.tmdb_id}_{event_info.season_id}_{event_info.episode_id}"
        
        # 检查缓存
        if cache_key in self._image_cache:
            self._image_cache.move_to_end(cache_key)
            self._metrics["cache_hits"] += 1
            return self._image_cache[cache_key]
            
        self._metrics["cache_misses"] += 1
        
        try:
            # 尝试获取背景图
            img = self.chain.obtain_specific_image(
                mediaid=event_info.tmdb_id, mtype=mtype,
                image_type=MediaImageType.Backdrop,
                season=event_info.season_id, episode=event_info.episode_id
            )
            
            # 尝试获取海报
            if not img:
                img = self.chain.obtain_specific_image(
                    mediaid=event_info.tmdb_id, mtype=mtype,
                    image_type=MediaImageType.Poster,
                    season=event_info.season_id, episode=event_info.episode_id
                )
                
            if img:
                # 缓存管理
                if len(self._image_cache) >= self.IMAGE_CACHE_MAX_SIZE:
                    self._image_cache.popitem(last=False)
                self._image_cache[cache_key] = img
                return img
                
        except Exception as e:
            logger.debug(f"获取TMDB图片失败: {str(e)}")
            
        return None

    def _get_audio_image_url(self, server_name: str, item_data: dict) -> Optional[str]:
        """获取音频图片URL"""
        if not server_name or not item_data:
            return None
            
        try:
            service = self.service_info(server_name)
            if not service or not service.instance:
                return None
                
            play_url = service.instance.get_play_url("dummy")
            if not play_url:
                return None
                
            parsed = urllib.parse.urlparse(play_url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            
            item_id = item_data.get('Id')
            primary_tag = item_data.get('ImageTags', {}).get('Primary')
            
            if not primary_tag:
                item_id = item_data.get('PrimaryImageItemId')
                primary_tag = item_data.get('PrimaryImageTag')
                
            if item_id and primary_tag:
                return (f"{base_url}/emby/Items/{item_id}/Images/Primary?"
                       f"maxHeight=450&maxWidth=450&tag={primary_tag}&quality=90")
                        
        except Exception:
            pass
            
        return None

    def _get_category_from_path(self, path: str, item_type: str, is_folder: bool = False) -> str:
        """从路径获取分类"""
        if not path:
            return ""
            
        try:
            path = os.path.normpath(path)
            
            if is_folder and item_type in ["TV", "SHOW"]:
                return os.path.basename(os.path.dirname(path))
                
            current_dir = os.path.dirname(path)
            dir_name = os.path.basename(current_dir)
            
            # 跳过季目录
            if re.search(r'^(Season|季|S\d)', dir_name, re.IGNORECASE):
                current_dir = os.path.dirname(current_dir)
                
            category_dir = os.path.dirname(current_dir)
            category = os.path.basename(category_dir)
            
            if not category or category == os.path.sep:
                return ""
                
            return category
            
        except Exception:
            return ""

    def _get_region_text_cn(self, tmdb_info) -> str:
        """获取地区中文名称"""
        if not tmdb_info:
            return ""
            
        try:
            codes = []
            
            if hasattr(tmdb_info, 'origin_country') and tmdb_info.origin_country:
                codes = tmdb_info.origin_country[:2]
            elif hasattr(tmdb_info, 'production_countries') and tmdb_info.production_countries:
                for country in tmdb_info.production_countries[:2]:
                    if isinstance(country, dict):
                        code = country.get('iso_3166_1')
                    else:
                        code = getattr(country, 'iso_3166_1', str(country))
                    if code:
                        codes.append(code)
                        
            if not codes:
                return ""
                
            cn_names = [self._COUNTRY_CN_MAP.get(code.upper(), code) for code in codes]
            return "、".join(cn_names)
            
        except Exception:
            return ""

    def _get_play_link(self, event_info) -> Optional[str]:
        """获取播放链接"""
        if not self._add_play_link or not event_info.server_name:
            return None
            
        service = self.service_info(event_info.server_name)
        if not service or not service.instance:
            return None
            
        return service.instance.get_play_url(event_info.item_id)

    def _format_duration(self, ticks) -> str:
        """格式化时长"""
        if not ticks:
            return "00:00"
            
        seconds = ticks / 10000000
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}:{secs:02d}"

    def _format_size(self, size) -> str:
        """格式化大小"""
        if not size:
            return "0MB"
            
        mb = size / 1024 / 1024
        return f"{mb:.1f} MB"

    @cached(
        region="MediaServerMsgAI",
        maxsize=128,
        ttl=600,
        skip_none=True,
        skip_empty=False
    )
    def _get_tmdb_info_cached(self, tmdb_id: str, mtype: MediaType, season: Optional[int] = None):
        """获取TMDB信息（带缓存）"""
        try:
            return self.chain.tmdb_info(tmdbid=tmdb_id, mtype=mtype, season=season)
        except Exception:
            return None

    # ==================== 媒体服务器相关方法 ====================
    def service_infos(self, type_filter: Optional[str] = None) -> Optional[Dict[str, ServiceInfo]]:
        """获取媒体服务器信息"""
        if not self._mediaservers:
            logger.debug("未配置媒体服务器")
            return None
            
        services = MediaServerHelper().get_services(
            type_filter=type_filter, 
            name_filters=self._mediaservers
        )
        
        if not services:
            logger.debug("获取媒体服务器失败")
            return None
            
        # 过滤活跃服务器
        active_services = {}
        for name, info in services.items():
            if info.instance and not info.instance.is_inactive():
                active_services[name] = info
            else:
                logger.warning(f"服务器 {name} 未连接")
                
        return active_services if active_services else None

    def service_info(self, name: str) -> Optional[ServiceInfo]:
        """获取特定服务器信息"""
        services = self.service_infos()
        return services.get(name) if services else None

    # ==================== 统计和管理 ====================
    def get_metrics(self) -> Dict[str, Any]:
        """获取运行指标"""
        uptime = time.time() - self._metrics["start_time"]
        
        processed = self._metrics["messages_processed"]
        sent = self._metrics["messages_sent"]
        skipped = self._metrics["messages_skipped"]
        
        skip_rate = (skipped / processed * 100) if processed > 0 else 0
        cache_hits = self._metrics["cache_hits"]
        cache_misses = self._metrics["cache_misses"]
        cache_total = cache_hits + cache_misses
        hit_rate = (cache_hits / cache_total * 100) if cache_total > 0 else 0
        
        return {
            "uptime_hours": round(uptime / 3600, 2),
            "messages_processed": processed,
            "messages_sent": sent,
            "messages_skipped": skipped,
            "messages_aggregated": self._metrics["messages_aggregated"],
            "skip_rate_percent": round(skip_rate, 2),
            "cache_hit_rate_percent": round(hit_rate, 2),
            "errors": self._metrics["errors"],
            "message_types": self._metrics["by_type"]
        }

    def stop_service(self):
        """停止服务，清理资源"""
        try:
            # 发送所有待处理的聚合消息
            for series_id in list(self._pending_messages.keys()):
                try:
                    self._send_aggregated_message(series_id)
                except Exception as e:
                    logger.error(f"发送聚合消息失败: {str(e)}")
                    
            # 取消所有定时器
            for timer in self._aggregate_timers.values():
                try:
                    timer.cancel()
                except Exception:
                    pass
                    
            # 清理缓存
            self._aggregate_timers.clear()
            self._pending_messages.clear()
            self._webhook_msg_keys.clear()
            self._image_cache.clear()
            
            # 清理TMDB缓存
            try:
                self._get_tmdb_info_cached.cache_clear()
            except Exception:
                pass
                
            # 打印统计信息
            metrics = self.get_metrics()
            logger.info(f"插件停止，运行统计: {metrics}")
            
        except Exception as e:
            logger.error(f"停止服务失败: {str(e)}")

    # ==================== 音乐专辑处理（保持原功能）====================
    def _handle_music_album(self, event_info, item_data):
        """处理音乐专辑"""
        try:
            album_name = item_data.get('Name', '')
            album_id = item_data.get('Id', '')
            album_artist = (item_data.get('Artists') or ['未知艺术家'])[0]
            
            service = self.service_info(event_info.server_name)
            if not service or not service.instance:
                return
                
            base_url = service.config.config.get('host', '')
            api_key = service.config.config.get('apikey', '')
            
            import requests
            fields = "Path,MediaStreams,Container,Size,RunTimeTicks,ImageTags,ProviderIds"
            api_url = f"{base_url}/emby/Items?ParentId={album_id}&Fields={fields}&api_key={api_key}"
            
            response = requests.get(api_url, timeout=10)
            if response.status_code == 200:
                songs = response.json().get('Items', [])
                logger.info(f"专辑 [{album_name}] 包含 {len(songs)} 首歌曲")
                
                for song in songs:
                    self._send_single_audio_notify(song, album_name, album_artist, base_url)
                    
        except Exception as e:
            logger.error(f"处理音乐专辑失败: {e}")

    def _send_single_audio_notify(self, song: dict, album_name, album_artist, base_url):
        """发送单曲通知"""
        try:
            song_name = song.get('Name', '未知歌曲')
            song_id = song.get('Id')
            artist = (song.get('Artists') or [album_artist])[0]
            
            duration = self._format_duration(song.get('RunTimeTicks', 0))
            container = song.get('Container', '').upper()
            size = self._format_size(song.get('Size', 0))
            
            texts = [
                f"⏰ 入库：{time.strftime('%H:%M:%S', time.localtime())}",
                f"👤 歌手：{artist}",
            ]
            
            if album_name:
                texts.append(f"💿 专辑：{album_name}")
                
            texts.extend([
                f"⏱️ 时长：{duration}",
                f"📦 格式：{container} · {size}"
            ])
            
            # 图片和链接
            image_url = self._get_audio_image_url(song.get('ServerId'), song)
            link = None
            
            if self._add_play_link:
                link = f"{base_url}/web/index.html#!/item?id={song_id}&serverId={song.get('ServerId', '')}"
                
            self._send_message(
                title=f"🎵 新入库媒体：{song_name}",
                texts=texts,
                image=image_url,
                link=link,
                message_type=MessageType.MUSIC
            )
            
        except Exception as e:
            logger.error(f"发送单曲通知失败: {e}")

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """获取插件命令"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """获取插件API"""
        return []

    def get_page(self) -> List[dict]:
        """获取插件页面"""
        return []