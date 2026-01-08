import shutil
import time
import traceback
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler

from app import schemas
from app.chain.storage import StorageChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.models.transferhistory import TransferHistory
from app.db.transferhistory_oper import TransferHistoryOper
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.helper.downloader import DownloaderHelper
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType, EventType, MediaType, MediaImageType
from app.utils.system import SystemUtils
from app.utils.http import RequestUtils


class SaMediaSyncDel(_PluginBase):
    # 插件名称
    plugin_name = "神医媒体文件同步删除自用版"
    # 插件描述
    plugin_desc = "通过神医插件通知同步删除历史记录、源文件和下载任务。"
    # 插件图标
    plugin_icon = "mediasyncdel.png"
    # 插件版本
    plugin_version = "1.1.1"  # 版本号更新
    # 插件作者
    plugin_author = "DDSRem,thsrite"
    # 作者主页
    author_url = "https://github.com/DDSRem"
    # 插件配置项ID前缀
    plugin_config_prefix = "samediasyncdel_"
    # 加载顺序
    plugin_order = 9
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler: Optional[BackgroundScheduler] = None
    _enabled = False
    _notify = False
    _del_source = False
    _del_history = False
    _local_library_path = None
    _p115_library_path = None
    _p115_force_delete_files = False
    _p123_library_path = None
    _p123_force_delete_files = False
    _transferchain = None
    _downloader_helper = None
    _transferhis = None
    _downloadhis = None
    _storagechain = None
    _mediaserver_helper = None
    _default_downloader = None
    _mediaserver = None
    _mediaservers = None
    _emby_host = None
    _emby_apikey = None
    _emby_user = None

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        try:
            logger.info(f"🔄 [{self.plugin_name}] 插件初始化开始...")
            
            self._transferchain = TransferChain()
            self._downloader_helper = DownloaderHelper()
            self._transferhis = TransferHistoryOper()
            self._downloadhis = DownloadHistoryOper()
            self._storagechain = StorageChain()
            self._mediaserver_helper = MediaServerHelper()
            self._mediaserver = None

            # 读取配置
            if config:
                self._enabled = config.get("enabled")
                self._notify = config.get("notify")
                self._del_source = config.get("del_source")
                self._del_history = config.get("del_history")
                self._local_library_path = config.get("local_library_path")
                self._p115_library_path = config.get("p115_library_path")
                self._p115_force_delete_files = config.get("p115_force_delete_files")
                self._p123_library_path = config.get("p123_library_path")
                self._p123_force_delete_files = config.get("p123_force_delete_files")
                self._mediaservers = config.get("mediaservers") or []

                logger.info(f"📋 插件配置加载: enabled={self._enabled}, notify={self._notify}, "
                          f"del_source={self._del_source}, mediaservers={self._mediaservers}")

                # 获取媒体服务器
                if self._mediaservers:
                    self._mediaserver = [self._mediaservers[0]]
                    logger.info(f"📺 选择的媒体服务器: {self._mediaserver}")

                # 获取默认下载器
                downloader_services = self._downloader_helper.get_services()
                for downloader_name, downloader_info in downloader_services.items():
                    if downloader_info.config.default:
                        self._default_downloader = downloader_name
                        logger.info(f"⬇️ 默认下载器: {self._default_downloader}")
                        break

                # 清理插件历史
                if self._del_history:
                    logger.info("🗑️ 清理插件历史数据...")
                    self.del_data(key="history")

                self.update_config(
                    {
                        "enabled": self._enabled,
                        "notify": self._notify,
                        "del_source": self._del_source,
                        "del_history": False,
                        "local_library_path": self._local_library_path,
                        "p115_library_path": self._p115_library_path,
                        "p115_force_delete_files": self._p115_force_delete_files,
                        "p123_library_path": self._p123_library_path,
                        "p123_force_delete_files": self._p123_force_delete_files,
                        "mediaservers": self._mediaserver,
                    }
                )

            # 获取媒体服务信息
            if self._mediaserver:
                logger.info(f"🔍 获取媒体服务器信息...")
                emby_servers = self._mediaserver_helper.get_services(
                    name_filters=self._mediaserver, type_filter="emby"
                )

                for server_name, emby_server in emby_servers.items():
                    self._emby_user = emby_server.instance.get_user()
                    self._emby_apikey = emby_server.config.config.get("apikey")
                    self._emby_host = emby_server.config.config.get("host")
                    if not self._emby_host.endswith("/"):
                        self._emby_host += "/"
                    if not self._emby_host.startswith("http"):
                        self._emby_host = "http://" + self._emby_host
                    
                    logger.info(f"✅ 媒体服务器配置成功: {server_name}")
                    logger.debug(f"   Host: {self._emby_host}")
                    logger.debug(f"   User: {self._emby_user}")
                    break

            logger.info(f"✅ [{self.plugin_name}] 插件初始化完成")
            
        except Exception as e:
            logger.error(f"❌ [{self.plugin_name}] 插件初始化失败: {str(e)}")
            logger.error(traceback.format_exc())

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "summary": "删除订阅历史记录",
            }
        ]

    def delete_history(self, key: str, apikey: str):
        """
        删除历史记录
        """
        logger.info(f"🗑️ 收到删除历史记录请求: key={key}")
        if apikey != settings.API_TOKEN:
            logger.warning("❌ API密钥错误")
            return schemas.Response(success=False, message="API密钥错误")
        
        # 历史记录
        historys = self.get_data("history")
        if not historys:
            logger.warning("⚠️ 未找到历史记录")
            return schemas.Response(success=False, message="未找到历史记录")
        
        # 删除指定记录
        original_count = len(historys)
        historys = [h for h in historys if h.get("unique") != key]
        deleted_count = original_count - len(historys)
        
        if deleted_count > 0:
            self.save_data("history", historys)
            logger.info(f"✅ 成功删除 {deleted_count} 条历史记录")
            return schemas.Response(success=True, message="删除成功")
        else:
            logger.warning("⚠️ 未找到匹配的历史记录")
            return schemas.Response(success=False, message="未找到匹配的历史记录")

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        logger.debug("📝 加载插件配置表单")

        local_media_tab = [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {
                            "cols": 12,
                        },
                        "content": [
                            {
                                "component": "VTextarea",
                                "props": {
                                    "model": "local_library_path",
                                    "rows": "2",
                                    "label": "本地媒体库路径映射",
                                    "placeholder": "媒体服务器路径#MoviePilot路径（一行一个）",
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "density": "compact",
                    "class": "mt-2",
                },
                "content": [
                    {
                        "component": "div",
                        "text": "关于路径映射（转移后文件路径）：",
                    },
                    {
                        "component": "div",
                        "text": "emby目录：/data/A.mp4",
                    },
                    {
                        "component": "div",
                        "text": "moviepilot目录：/mnt/link/A.mp4",
                    },
                    {
                        "component": "div",
                        "text": "路径映射填：/data#/mnt/link",
                    },
                    {
                        "component": "div",
                        "text": "不正确配置会导致查询不到转移记录！",
                    },
                ],
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "density": "compact",
                    "class": "mt-2",
                    "text": "注意：不同的存储模块不能配置同一个媒体路径，否则会导致匹配失败或误删除！",
                },
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "density": "compact",
                    "class": "mt-2",
                    "text": "注意：本地同步删除功能需要使用神医助手PRO且版本在v3.0.0.3及以上或神医助手社区版且版本在v2.0.0.27及以上！",
                },
            },
        ]

        p115_media_tab = [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "p115_force_delete_files",
                                    "label": "强制网盘删除",
                                    "hint": "MP不存在历史记录或无法获取TMDB ID时强制删除网盘文件",
                                },
                            }
                        ],
                    },
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {
                            "cols": 12,
                        },
                        "content": [
                            {
                                "component": "VTextarea",
                                "props": {
                                    "model": "p115_library_path",
                                    "rows": "2",
                                    "label": "115网盘媒体库路径映射",
                                    "placeholder": "媒体服务器STRM路径#MoviePilot路径#115网盘路径（一行一个）",
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "density": "compact",
                    "class": "mt-2",
                },
                "content": [
                    {
                        "component": "div",
                        "text": "关于路径映射（转移后文件路径）：",
                    },
                    {
                        "component": "div",
                        "text": "emby目录：/media/strm",
                    },
                    {
                        "component": "div",
                        "text": "moviepilot目录：/mnt/strm",
                    },
                    {
                        "component": "div",
                        "text": "115网盘媒体库目录：/影视",
                    },
                    {
                        "component": "div",
                        "text": "路径映射填：/media/strm#/mnt/strm#/影视",
                    },
                    {
                        "component": "div",
                        "text": "不正确配置会导致查询不到转移记录！",
                    },
                ],
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "density": "compact",
                    "class": "mt-2",
                    "text": "注意：不同的存储模块不能配置同一个媒体路径，否则会导致匹配失败或误删除！",
                },
            },
        ]

        p123_media_tab = [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "p123_force_delete_files",
                                    "label": "强制网盘删除",
                                    "hint": "MP不存在历史记录或无法获取TMDB ID时强制删除网盘文件",
                                },
                            }
                        ],
                    },
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {
                            "cols": 12,
                        },
                        "content": [
                            {
                                "component": "VTextarea",
                                "props": {
                                    "model": "p123_library_path",
                                    "rows": "2",
                                    "label": "123云盘媒体库路径映射",
                                    "placeholder": "媒体服务器STRM路径#MoviePilot路径#115网盘路径（一行一个）",
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "density": "compact",
                    "class": "mt-2",
                },
                "content": [
                    {
                        "component": "div",
                        "text": "关于路径映射（转移后文件路径）：",
                    },
                    {
                        "component": "div",
                        "text": "emby目录：/media/strm",
                    },
                    {
                        "component": "div",
                        "text": "moviepilot目录：/mnt/strm",
                    },
                    {
                        "component": "div",
                        "text": "123云盘媒体库目录：/影视",
                    },
                    {
                        "component": "div",
                        "text": "路径映射填：/media/strm#/mnt/strm#/影视",
                    },
                    {
                        "component": "div",
                        "text": "不正确配置会导致查询不到转移记录！",
                    },
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {
                            "cols": 12,
                        },
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "warning",
                                    "variant": "tonal",
                                    "text": "注意：不同的存储模块不能配置同一个媒体路径，否则会导致匹配失败或误删除！",
                                },
                            },
                        ],
                    }
                ],
            },
        ]

        return [
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-3"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "d-flex align-center"},
                        "content": [
                            {
                                "component": "VIcon",
                                "props": {
                                    "icon": "mdi-cog",
                                    "color": "primary",
                                    "class": "mr-2",
                                },
                            },
                            {"component": "span", "text": "基础设置"},
                        ],
                    },
                    {"component": "VDivider"},
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VRow",
                                "content": [
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 2},
                                        "content": [
                                            {
                                                "component": "VSwitch",
                                                "props": {
                                                    "model": "enabled",
                                                    "label": "启用插件",
                                                },
                                            }
                                        ],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 2},
                                        "content": [
                                            {
                                                "component": "VSwitch",
                                                "props": {
                                                    "model": "notify",
                                                    "label": "发送通知",
                                                },
                                            }
                                        ],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 2},
                                        "content": [
                                            {
                                                "component": "VSwitch",
                                                "props": {
                                                    "model": "del_source",
                                                    "label": "删除源文件",
                                                },
                                            }
                                        ],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 2},
                                        "content": [
                                            {
                                                "component": "VSwitch",
                                                "props": {
                                                    "model": "del_history",
                                                    "label": "删除历史",
                                                },
                                            }
                                        ],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12, "md": 4},
                                        "content": [
                                            {
                                                "component": "VSelect",
                                                "props": {
                                                    "multiple": True,
                                                    "chips": True,
                                                    "clearable": True,
                                                    "model": "mediaservers",
                                                    "label": "媒体服务器",
                                                    "items": [
                                                        {
                                                            "title": config.name,
                                                            "value": config.name,
                                                        }
                                                        for config in self._mediaserver_helper.get_configs().values()
                                                        if config.type == "emby"
                                                    ],
                                                },
                                            }
                                        ],
                                    },
                                ],
                            },
                            {
                                "component": "VRow",
                                "content": [
                                    {
                                        "component": "VCol",
                                        "props": {
                                            "cols": 12,
                                        },
                                        "content": [
                                            {
                                                "component": "VAlert",
                                                "props": {
                                                    "type": "info",
                                                    "variant": "tonal",
                                                    "text": "只能配置一个Emby媒体服务器，配置多个默认查寻第一个媒体服务器信息",
                                                },
                                            },
                                        ],
                                    }
                                ],
                            },
                        ],
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {
                        "component": "VTabs",
                        "props": {"model": "tab", "grow": True, "color": "primary"},
                        "content": [
                            {
                                "component": "VTab",
                                "props": {"value": "tab-local"},
                                "content": [
                                    {"component": "span", "text": "本地媒体配置"},
                                ],
                            },
                            {
                                "component": "VTab",
                                "props": {"value": "tab-p115"},
                                "content": [
                                    {"component": "span", "text": "115网盘媒体配置"},
                                ],
                            },
                            {
                                "component": "VTab",
                                "props": {"value": "tab-p123"},
                                "content": [
                                    {"component": "span", "text": "123云盘媒体配置"},
                                ],
                            },
                        ],
                    },
                    {"component": "VDivider"},
                    {
                        "component": "VWindow",
                        "props": {"model": "tab"},
                        "content": [
                            {
                                "component": "VWindowItem",
                                "props": {"value": "tab-local"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "content": local_media_tab,
                                    }
                                ],
                            },
                            {
                                "component": "VWindowItem",
                                "props": {"value": "tab-p115"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "content": p115_media_tab,
                                    }
                                ],
                            },
                            {
                                "component": "VWindowItem",
                                "props": {"value": "tab-p123"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "content": p123_media_tab,
                                    }
                                ],
                            },
                        ],
                    },
                ],
            },
        ], {
            "enabled": False,
            "notify": True,
            "del_source": False,
            "del_history": False,
            "local_library_path": "",
            "p115_library_path": "",
            "p115_force_delete_files": False,
            "p123_library_path": "",
            "p123_force_delete_files": False,
            "mediaservers": [],
            "tab": "local_media_tab",
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        logger.debug("📄 加载插件详情页面")
        
        # 查询同步详情
        historys = self.get_data("history")
        if not historys:
            logger.debug("📊 暂无历史数据")
            return [
                {
                    "component": "div",
                    "text": "暂无数据",
                    "props": {
                        "class": "text-center",
                    },
                }
            ]
        
        logger.info(f"📊 找到 {len(historys)} 条历史记录")
        
        # 数据按时间降序排序
        historys = sorted(historys, key=lambda x: x.get("del_time"), reverse=True)
        
        # 拼装页面
        contents = []
        for history in historys:
            htype = history.get("type")
            title = history.get("title")
            unique = history.get("unique")
            year = history.get("year")
            season = history.get("season")
            episode = history.get("episode")
            image = history.get("image")
            del_time = history.get("del_time")

            if season:
                sub_contents = [
                    {
                        "component": "VCardText",
                        "props": {"class": "pa-0 px-2"},
                        "text": f"类型：{htype}",
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "pa-0 px-2"},
                        "text": f"标题：{title}",
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "pa-0 px-2"},
                        "text": f"年份：{year}",
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "pa-0 px-2"},
                        "text": f"季：{season}",
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "pa-0 px-2"},
                        "text": f"集：{episode}",
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "pa-0 px-2"},
                        "text": f"时间：{del_time}",
                    },
                ]
            else:
                sub_contents = [
                    {
                        "component": "VCardText",
                        "props": {"class": "pa-0 px-2"},
                        "text": f"类型：{htype}",
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "pa-0 px-2"},
                        "text": f"标题：{title}",
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "pa-0 px-2"},
                        "text": f"年份：{year}",
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "pa-0 px-2"},
                        "text": f"时间：{del_time}",
                    },
                ]

            contents.append(
                {
                    "component": "VCard",
                    "content": [
                        {
                            "component": "VDialogCloseBtn",
                            "props": {
                                "innerClass": "absolute top-0 right-0",
                            },
                            "events": {
                                "click": {
                                    "api": "plugin/SaMediaSyncDel/delete_history",
                                    "method": "get",
                                    "params": {
                                        "key": unique,
                                        "apikey": settings.API_TOKEN,
                                    },
                                }
                            },
                        },
                        {
                            "component": "div",
                            "props": {
                                "class": "d-flex justify-space-start flex-nowrap flex-row",
                            },
                            "content": [
                                {
                                    "component": "div",
                                    "content": [
                                        {
                                            "component": "VImg",
                                            "props": {
                                                "src": image,
                                                "height": 120,
                                                "width": 80,
                                                "aspect-ratio": "2/3",
                                                "class": "object-cover shadow ring-gray-500",
                                                "cover": True,
                                            },
                                        }
                                    ],
                                },
                                {"component": "div", "content": sub_contents},
                            ],
                        },
                    ],
                }
            )

        return [
            {
                "component": "div",
                "props": {
                    "class": "grid gap-3 grid-info-card",
                },
                "content": contents,
            }
        ]

    def has_prefix(self, full_path, prefix_path):
        """
        判断路径是否包含
        """
        full = Path(full_path).parts
        prefix = Path(prefix_path).parts

        if len(prefix) > len(full):
            return False

        return full[: len(prefix)] == prefix

    def __get_local_media_path(self, media_path):
        """
        获取本地媒体目录路径
        """
        if not self._local_library_path:
            return False, None
            
        media_paths = self._local_library_path.split("\n")
        for i, path in enumerate(media_paths):
            if not path.strip():
                continue
            parts = path.split("#", 1)
            if len(parts) != 2:
                logger.warning(f"⚠️ 本地路径映射格式错误 (第{i+1}行): {path}")
                continue
            if self.has_prefix(media_path, parts[0]):
                logger.debug(f"✅ 匹配到本地路径映射: {parts[0]} -> {parts[1]}")
                return True, parts
        return False, None

    def __get_p115_media_path(self, media_path):
        """
        获取115网盘媒体目录路径
        """
        if not self._p115_library_path:
            return False, None
            
        media_paths = self._p115_library_path.split("\n")
        for i, path in enumerate(media_paths):
            if not path.strip():
                continue
            parts = path.split("#", 2)
            if len(parts) != 3:
                logger.warning(f"⚠️ 115路径映射格式错误 (第{i+1}行): {path}")
                continue
            if self.has_prefix(media_path, parts[0]):
                logger.debug(f"✅ 匹配到115路径映射: {parts[0]} -> {parts[1]} -> {parts[2]}")
                return True, parts
        return False, None

    def __get_p123_media_path(self, media_path):
        """
        获取123云盘媒体目录路径
        """
        if not self._p123_library_path:
            return False, None
            
        media_paths = self._p123_library_path.split("\n")
        for i, path in enumerate(media_paths):
            if not path.strip():
                continue
            parts = path.split("#", 2)
            if len(parts) != 3:
                logger.warning(f"⚠️ 123路径映射格式错误 (第{i+1}行): {path}")
                continue
            if self.has_prefix(media_path, parts[0]):
                logger.debug(f"✅ 匹配到123路径映射: {parts[0]} -> {parts[1]} -> {parts[2]}")
                return True, parts
        return False, None

    @eventmanager.register(EventType.WebhookMessage)
    def sync_del_by_plugin(self, event):
        """
        emby删除媒体库同步删除历史记录
        """
        if not self._enabled:
            logger.debug("🚫 插件未启用，跳过处理")
            return

        try:
            logger.info("🔔 收到Webhook消息事件")
            event_data = event.event_data
            event_type = event_data.event

            # 神医助手深度删除标识
            if not event_type or str(event_type) != "deep.delete":
                logger.debug(f"📤 事件类型不匹配: {event_type}，跳过处理")
                return

            logger.info("🎯 接收到神医深度删除事件")
            self._process_sync_delete(event_data)
            
        except Exception as e:
            logger.error(f"❌ 处理Webhook事件失败: {str(e)}")
            logger.error(traceback.format_exc())

    def _process_sync_delete(self, event_data):
        """处理同步删除逻辑"""
        try:
            # 媒体类型
            media_type = event_data.item_type
            # 媒体名称
            media_name = event_data.item_name
            # 媒体路径
            media_path = event_data.item_path
            # tmdb_id
            tmdb_id = event_data.tmdb_id
            # 季数
            season_num = event_data.season_id
            # 集数
            episode_num = event_data.episode_id

            logger.info(f"📦 处理媒体删除: {media_name}")
            logger.debug(f"   类型: {media_type}")
            logger.debug(f"   路径: {media_path}")
            logger.debug(f"   TMDB ID: {tmdb_id}")
            logger.debug(f"   季: {season_num}")
            logger.debug(f"   集: {episode_num}")

            # 执行删除逻辑
            if not media_path:
                logger.error("❌ 媒体路径为空，无法处理")
                return

            media_suffix = None
            media_storage = None

            # 匹配媒体存储模块
            logger.info("🔍 开始匹配存储类型...")
            if self._local_library_path:
                status, _ = self.__get_local_media_path(media_path)
                if status:
                    media_storage = "local"
                    logger.info("💾 匹配到本地存储")

            if not media_storage and self._p115_library_path:
                status, _ = self.__get_p115_media_path(media_path)
                if status:
                    media_storage = "p115"
                    logger.info("🗳️ 匹配到115网盘存储")

            if not media_storage and self._p123_library_path:
                status, _ = self.__get_p123_media_path(media_path)
                if status:
                    media_storage = "p123"
                    logger.info("☁️ 匹配到123云盘存储")

            if not media_storage:
                logger.error(f"❌ {media_name} 同步删除失败，未识别到储存类型")
                logger.warning("⚠️ 请检查路径映射配置")
                return

            logger.info(f"✅ 存储类型识别: {media_storage}")

            # 对于网盘文件需要获取媒体后缀名
            if media_storage in ["p115", "p123"]:
                if Path(media_path).suffix:
                    media_suffix = event_data.json_object.get("Item", {}).get(
                        "Container", None
                    )
                    if not media_suffix:
                        if media_storage == "p115":
                            logger.debug("🔍 尝试获取115网盘文件后缀...")
                            media_suffix = self.__get_p115_media_suffix(media_path)
                        else:
                            logger.debug("🔍 尝试获取123云盘文件后缀...")
                            media_suffix = self.__get_p123_media_suffix(media_path)
                        
                        if not media_suffix:
                            logger.error(f"❌ {media_name} 同步删除失败，未识别媒体后缀名")
                            return
                        else:
                            logger.info(f"✅ 获取到文件后缀: {media_suffix}")
                else:
                    logger.debug(f"{media_name} 跳过识别媒体后缀名")

            # 单集或单季缺失 TMDB ID 获取
            if (episode_num or season_num) and (not tmdb_id or not str(tmdb_id).isdigit()):
                logger.warning(f"⚠️ 未获取到TMDB ID，尝试从剧集获取...")
                series_id = event_data.json_object["Item"]["SeriesId"]
                tmdb_id = self.__get_series_tmdb_id(series_id)
                if tmdb_id:
                    logger.info(f"✅ 从剧集获取到TMDB ID: {tmdb_id}")

            if not tmdb_id or not str(tmdb_id).isdigit():
                force_delete = False
                if media_storage == "p115" and self._p115_force_delete_files:
                    force_delete = True
                elif media_storage == "p123" and self._p123_force_delete_files:
                    force_delete = True
                    
                if not force_delete:
                    logger.error(f"❌ {media_name} 同步删除失败，未获取到TMDB ID，请检查媒体库媒体是否刮削")
                    return
                else:
                    logger.warning(f"⚠️ 未获取到TMDB ID，启用强制删除模式")

            # 执行同步删除
            self.__sync_del(
                media_type=media_type,
                media_name=media_name,
                media_path=media_path,
                tmdb_id=tmdb_id,
                season_num=season_num,
                episode_num=episode_num,
                media_storage=media_storage,
                media_suffix=media_suffix,
            )
            
        except Exception as e:
            logger.error(f"❌ 处理同步删除失败: {str(e)}")
            logger.error(traceback.format_exc())

    def __sync_del(
        self,
        media_type: str,
        media_name: str,
        media_path: str,
        tmdb_id: int,
        season_num: str,
        episode_num: str,
        media_storage: str,
        media_suffix: str,
    ):
        """执行同步删除"""
        try:
            logger.info(f"🚀 开始执行同步删除: {media_name}")
            
            if not media_type:
                logger.error(f"❌ {media_name} 同步删除失败，未获取到媒体类型，请检查媒体是否刮削")
                return

            if media_storage == "local":
                self._process_local_delete(media_type, media_name, media_path, tmdb_id, season_num, episode_num)
            elif media_storage == "p115":
                self._process_p115_delete(media_type, media_name, media_path, tmdb_id, season_num, episode_num, media_suffix)
            elif media_storage == "p123":
                self._process_p123_delete(media_type, media_name, media_path, tmdb_id, season_num, episode_num, media_suffix)
            else:
                logger.error(f"❌ 未知存储类型: {media_storage}")
                return
                
        except Exception as e:
            logger.error(f"❌ 执行同步删除失败: {str(e)}")
            logger.error(traceback.format_exc())

    def _process_local_delete(self, media_type, media_name, media_path, tmdb_id, season_num, episode_num):
        """处理本地存储删除"""
        logger.info("💾 处理本地存储删除...")
        
        # 处理路径映射
        if self._local_library_path:
            _, sub_paths = self.__get_local_media_path(media_path)
            if sub_paths:
                original_path = media_path
                media_path = media_path.replace(sub_paths[0], sub_paths[1]).replace("\\", "/")
                logger.info(f"🔄 路径映射: {original_path} -> {media_path}")

        # 兼容重新整理的场景
        if Path(media_path).exists():
            logger.warn(f"⚠️ 转移路径 {media_path} 未被删除或重新生成，跳过处理")
            return

        # 查询转移记录
        msg, transfer_history = self.__get_transfer_his(
            media_type=media_type,
            media_name=media_name,
            media_path=media_path,
            tmdb_id=tmdb_id,
            season_num=season_num,
            episode_num=episode_num,
        )

        logger.info(f"🔍 查询转移记录: {msg}")

        if not transfer_history:
            logger.warn(f"⚠️ {media_type} {media_name} 未获取到可删除数据，请检查路径映射是否配置错误，请检查tmdbid获取是否正确")
            return

        logger.info(f"✅ 获取到 {len(transfer_history)} 条转移记录，开始同步删除")
        
        # 执行删除
        self._execute_deletion(transfer_history, media_name, media_storage="local")

    def _process_p115_delete(self, media_type, media_name, media_path, tmdb_id, season_num, episode_num, media_suffix):
        """处理115网盘删除"""
        logger.info("🗳️ 处理115网盘删除...")
        
        mp_media_path = None
        if self._p115_library_path:
            _, sub_paths = self.__get_p115_media_path(media_path)
            if sub_paths:
                mp_media_path = media_path.replace(sub_paths[0], sub_paths[1]).replace("\\", "/")
                media_path = media_path.replace(sub_paths[0], sub_paths[2]).replace("\\", "/")
                logger.info(f"🔄 115路径映射: {sub_paths[0]} -> {sub_paths[1]} -> {sub_paths[2]}")

        if Path(media_path).suffix and media_suffix:
            # 自动替换媒体文件后缀名称为真实名称
            media_path = str(
                Path(media_path).parent
                / str(Path(media_path).stem + "." + media_suffix)
            )
            # 这里做一次大小写转换，避免资源后缀名为全大写情况
            if media_suffix.isupper():
                media_suffix = media_suffix.lower()
            elif media_suffix.islower():
                media_suffix = media_suffix.upper()
            media_path_2 = str(
                Path(media_path).parent
                / str(Path(media_path).stem + "." + media_suffix)
            )
            logger.debug(f"🔄 文件后缀处理: {media_path} -> {media_path_2}")
        else:
            media_path_2 = media_path

        # 兼容重新整理的场景
        if mp_media_path and Path(mp_media_path).exists():
            logger.warn(f"⚠️ 转移路径 {media_path} 未被删除或重新生成，跳过处理")
            return

        # 查询转移记录
        msg, transfer_history = self.__get_transfer_his(
            media_type=media_type,
            media_name=media_name,
            media_path=media_path,
            tmdb_id=tmdb_id,
            season_num=season_num,
            episode_num=episode_num,
        )

        if not msg:
            msg = media_name

        logger.info(f"🔍 查询转移记录: {msg}")

        if not transfer_history:
            msg, transfer_history = self.__get_transfer_his(
                media_type=media_type,
                media_name=media_name,
                media_path=media_path_2,
                tmdb_id=tmdb_id,
                season_num=season_num,
                episode_num=episode_num,
            )
            
            if not transfer_history:
                if self._p115_force_delete_files:
                    logger.warn(f"⚠️ {media_name} 强制删除网盘媒体文件")
                    self.__delete_p115_files(
                        file_path=media_path,
                        media_name=media_name,
                    )
                else:
                    logger.warn(f"⚠️ {media_type} {media_name} 未获取到可删除数据，请检查路径映射是否配置错误，请检查tmdbid获取是否正确")
                return
            else:
                media_path = media_path_2

        if transfer_history:
            logger.info(f"✅ 获取到 {len(transfer_history)} 条转移记录，开始同步删除")
            self._execute_deletion(transfer_history, media_name, media_storage="p115", media_path=media_path)

    def _process_p123_delete(self, media_type, media_name, media_path, tmdb_id, season_num, episode_num, media_suffix):
        """处理123云盘删除"""
        logger.info("☁️ 处理123云盘删除...")
        
        mp_media_path = None
        if self._p123_library_path:
            _, sub_paths = self.__get_p123_media_path(media_path)
            if sub_paths:
                mp_media_path = media_path.replace(sub_paths[0], sub_paths[1]).replace("\\", "/")
                media_path = media_path.replace(sub_paths[0], sub_paths[2]).replace("\\", "/")
                logger.info(f"🔄 123路径映射: {sub_paths[0]} -> {sub_paths[1]} -> {sub_paths[2]}")

        if Path(media_path).suffix and media_suffix:
            # 自动替换媒体文件后缀名称为真实名称
            media_path = str(
                Path(media_path).parent
                / str(Path(media_path).stem + "." + media_suffix)
            )
            # 这里做一次大小写转换，避免资源后缀名为全大写情况
            if media_suffix.isupper():
                media_suffix = media_suffix.lower()
            elif media_suffix.islower():
                media_suffix = media_suffix.upper()
            media_path_2 = str(
                Path(media_path).parent
                / str(Path(media_path).stem + "." + media_suffix)
            )
            logger.debug(f"🔄 文件后缀处理: {media_path} -> {media_path_2}")
        else:
            media_path_2 = media_path

        # 兼容重新整理的场景
        if mp_media_path and Path(mp_media_path).exists():
            logger.warn(f"⚠️ 转移路径 {media_path} 未被删除或重新生成，跳过处理")
            return

        # 查询转移记录
        msg, transfer_history = self.__get_transfer_his(
            media_type=media_type,
            media_name=media_name,
            media_path=media_path,
            tmdb_id=tmdb_id,
            season_num=season_num,
            episode_num=episode_num,
        )

        if not msg:
            msg = media_name

        logger.info(f"🔍 查询转移记录: {msg}")

        if not transfer_history:
            msg, transfer_history = self.__get_transfer_his(
                media_type=media_type,
                media_name=media_name,
                media_path=media_path_2,
                tmdb_id=tmdb_id,
                season_num=season_num,
                episode_num=episode_num,
            )
            
            if not transfer_history:
                if self._p123_force_delete_files:
                    logger.warn(f"⚠️ {media_name} 强制删除网盘媒体文件")
                    self.__delete_p123_files(
                        file_path=media_path,
                        media_name=media_name,
                    )
                else:
                    logger.warn(f"⚠️ {media_type} {media_name} 未获取到可删除数据，请检查路径映射是否配置错误，请检查tmdbid获取是否正确")
                return
            else:
                media_path = media_path_2

        if transfer_history:
            logger.info(f"✅ 获取到 {len(transfer_history)} 条转移记录，开始同步删除")
            self._execute_deletion(transfer_history, media_name, media_storage="p123", media_path=media_path)

    def _execute_deletion(self, transfer_history, media_name, media_storage="local", media_path=None):
        """执行删除操作"""
        try:
            year = None
            del_torrent_hashs = []
            stop_torrent_hashs = []
            error_cnt = 0
            image = "https://emby.media/notificationicon.png"
            
            logger.info(f"🗑️ 开始删除 {len(transfer_history)} 条转移记录...")
            
            for i, transferhis in enumerate(transfer_history, 1):
                logger.info(f"📝 处理第 {i}/{len(transfer_history)} 条记录: {transferhis.title}")
                
                title = transferhis.title
                if title not in media_name:
                    logger.warn(f"⚠️ 当前转移记录 {transferhis.id} {title} {transferhis.tmdbid} 与删除媒体{media_name}不符，防误删，暂不自动删除")
                    continue
                    
                image = transferhis.image or image
                year = transferhis.year

                # 0、删除转移记录
                logger.debug(f"🗑️ 删除转移记录 ID: {transferhis.id}")
                self._transferhis.delete(transferhis.id)
                logger.info(f"✅ 转移记录 {transferhis.id} 已删除")

                # 1、删除网盘文件（如果是网盘存储）
                if media_storage == "p115":
                    self.__delete_p115_files(
                        file_path=transferhis.dest,
                        media_name=media_name,
                    )
                elif media_storage == "p123":
                    self.__delete_p123_files(
                        file_path=transferhis.dest,
                        media_name=media_name,
                    )

                # 删除种子任务
                if self._del_source:
                    logger.debug("🔧 开始处理源文件删除...")
                    # 1、直接删除源文件
                    # 当源文件是本地文件且整理方式不是移动才进行源文件删除
                    if (
                        transferhis.src
                        and Path(transferhis.src).suffix in settings.RMT_MEDIAEXT
                        and transferhis.src_storage == "local"
                        and transferhis.mode != "move"
                    ):
                        # 删除源文件
                        if Path(transferhis.src).exists():
                            logger.info(f"🗑️ 源文件 {transferhis.src} 开始删除")
                            Path(transferhis.src).unlink(missing_ok=True)
                            logger.info(f"✅ 源文件 {transferhis.src} 已删除")
                            self.__remove_parent_dir(Path(transferhis.src))

                        if transferhis.download_hash:
                            try:
                                logger.debug(f"🔧 处理种子任务: {transferhis.download_hash}")
                                # 2、判断种子是否被删除完
                                delete_flag, success_flag, handle_torrent_hashs = (
                                    self.handle_torrent(
                                        type=transferhis.type,
                                        src=transferhis.src,
                                        torrent_hash=transferhis.download_hash,
                                    )
                                )
                                if not success_flag:
                                    error_cnt += 1
                                    logger.warning(f"⚠️ 种子处理失败: {transferhis.download_hash}")
                                else:
                                    if delete_flag:
                                        del_torrent_hashs += handle_torrent_hashs
                                        logger.info(f"✅ 种子已删除: {handle_torrent_hashs}")
                                    else:
                                        stop_torrent_hashs += handle_torrent_hashs
                                        logger.info(f"⏸️ 种子已暂停: {handle_torrent_hashs}")
                            except Exception as e:
                                logger.error(f"❌ 删除种子失败：{str(e)}")
                                logger.error(traceback.format_exc())

            logger.info(f"🎉 同步删除 {media_name} 完成！")
            
            # 转换媒体类型
            media_type_enum = MediaType.MOVIE if media_storage == "p115" else MediaType.TV
            
            # 发送通知
            self._send_notification(
                media_name=media_name,
                media_type=media_type_enum,
                media_path=media_path or transfer_history[0].dest if transfer_history else "",
                tmdb_id=transfer_history[0].tmdbid if transfer_history else None,
                season_num=None,
                episode_num=None,
                media_storage=media_storage,
                transfer_history=transfer_history,
                del_torrent_hashs=del_torrent_hashs,
                stop_torrent_hashs=stop_torrent_hashs,
                error_cnt=error_cnt,
                image=image,
                year=year
            )
            
            # 保存历史记录
            self._save_history(
                media_name=media_name,
                media_type=media_type_enum,
                media_path=media_path or transfer_history[0].dest if transfer_history else "",
                tmdb_id=transfer_history[0].tmdbid if transfer_history else None,
                year=year,
                season_num=None,
                episode_num=None,
                image=image
            )
            
        except Exception as e:
            logger.error(f"❌ 执行删除操作失败: {str(e)}")
            logger.error(traceback.format_exc())

    def _send_notification(self, media_name, media_type, media_path, tmdb_id, season_num, episode_num,
                          media_storage, transfer_history, del_torrent_hashs, stop_torrent_hashs, 
                          error_cnt, image, year):
        """发送通知"""
        if not self._notify:
            logger.debug("🔕 通知功能未启用")
            return
            
        try:
            logger.info("📨 准备发送通知...")
            
            # 获取背景图片
            backrop_image = (
                self.chain.obtain_specific_image(
                    mediaid=tmdb_id,
                    mtype=media_type,
                    image_type=MediaImageType.Backdrop,
                    season=season_num,
                    episode=episode_num,
                )
                or image
            )

            # 统计种子操作信息
            torrent_cnt_msg = ""
            if del_torrent_hashs:
                torrent_cnt_msg += f"🗑️ 种子：{len(set(del_torrent_hashs))}个\n"
            if stop_torrent_hashs:
                stop_cnt = 0
                # 排除已删除
                for stop_hash in set(stop_torrent_hashs):
                    if stop_hash not in set(del_torrent_hashs):
                        stop_cnt += 1
                if stop_cnt > 0:
                    torrent_cnt_msg += f"⏸️ 种子：{stop_cnt}个\n"
            if error_cnt:
                torrent_cnt_msg += f"❌ 失败：{error_cnt}个\n"

            # 获取媒体信息
            tmdb_info = None
            if tmdb_id:
                mtype = media_type
                try:
                    tmdb_info = self.chain.recognize_media(tmdbid=int(tmdb_id), mtype=mtype)
                    logger.debug(f"✅ 获取到TMDB信息: {tmdb_info.title if tmdb_info else '无'}")
                except Exception as e:
                    logger.warning(f"⚠️ 获取TMDB信息失败: {str(e)}")
            
            media_year = tmdb_info.year if (tmdb_info and tmdb_info.year) else year
            
            show_title = tmdb_info.title if tmdb_info else media_name
            if episode_num: 
                show_title += f" ({media_year}) S{int(season_num):02d}E{int(episode_num):02d}"
            elif season_num:
                show_title += f" ({media_year}) S{int(season_num):02d}"
            else:
                show_title += f" ({media_year})" if media_year else show_title

            # 存储类型显示
            if media_storage == "p115":
                show_storage = "115网盘"
            elif media_storage == "p123":
                show_storage = "123网盘"
            elif media_storage == "local":
                show_storage = "本地存储"
            else:
                show_storage = "未知存储类型"
            
            # 判断媒体类型emoji
            media_emoji = "🎬" if media_type == MediaType.MOVIE else "📺"
            
            # 构建通知内容
            notification_text = (
                f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))}\n"
                f"📊 类型：{media_type.value if hasattr(media_type, 'value') else media_type}\n"
                f"💾 存储：{show_storage}\n"
                f"📊 记录：删除转移记录 {len(transfer_history) if transfer_history else 0} 条\n"
                f"{torrent_cnt_msg if torrent_cnt_msg else '✅ 操作：无相关种子'}\n"
                f"📁 路径：\n{media_path}\n"
            )
            
            logger.debug(f"📋 通知内容:\n{notification_text}")
            
            # 发送通知
            self.post_message(
                mtype=NotificationType.Plugin,
                title=f"{media_emoji} {show_title} 已删除",
                image=backrop_image,
                text=notification_text,
            )
            
            logger.info("✅ 通知发送成功")
            
        except Exception as e:
            logger.error(f"❌ 发送通知失败: {str(e)}")
            logger.error(traceback.format_exc())

    def _save_history(self, media_name, media_type, media_path, tmdb_id, year, season_num, episode_num, image):
        """保存历史记录"""
        try:
            logger.debug("💾 保存历史记录...")
            
            # 读取历史记录
            history = self.get_data("history") or []

            # 获取poster图片
            poster_image = (
                self.chain.obtain_specific_image(
                    mediaid=tmdb_id,
                    mtype=media_type,
                    image_type=MediaImageType.Poster,
                )
                or image
            )

            # 使用emoji表示媒体类型
            media_type_emoji = "🎬" if media_type == MediaType.MOVIE else "📺"

            history.append(
                {
                    "type": f"{media_type_emoji} {media_type.value}",
                    "title": media_name,
                    "year": year,
                    "path": media_path,
                    "season": season_num if season_num and str(season_num).isdigit() else None,
                    "episode": episode_num if episode_num and str(episode_num).isdigit() else None,
                    "image": poster_image,
                    "del_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())),
                    "unique": f"{media_name}:{tmdb_id}:{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))}",
                }
            )

            # 保存历史
            self.save_data("history", history)
            logger.info(f"✅ 历史记录保存成功，当前共 {len(history)} 条记录")
            
        except Exception as e:
            logger.error(f"❌ 保存历史记录失败: {str(e)}")
            logger.error(traceback.format_exc())

    def __delete_p115_files(self, file_path: str, media_name: str):
        """
        删除115网盘文件
        """
        try:
            logger.info(f"🗑️ 删除115网盘文件: {file_path}")
            
            # 获取文件(夹)详细信息
            fileitem = self._storagechain.get_file_item(
                storage="u115", path=Path(file_path)
            )
            if fileitem.type == "dir":
                # 删除整个文件夹
                self._storagechain.delete_file(fileitem)
                logger.info(f"✅ {media_name} 删除网盘文件夹：{file_path}")
            else:
                # 调用 MP 模块删除媒体文件和空媒体目录
                self._storagechain.delete_media_file(fileitem=fileitem)
                logger.info(f"✅ {media_name} 删除网盘媒体文件：{file_path}")
        except Exception as e:
            logger.error(f"❌ {media_name} 删除网盘媒体 {file_path} 失败: {str(e)}")
            logger.error(traceback.format_exc())

    def __delete_p123_files(self, file_path: str, media_name: str):
        """
        删除123云盘文件
        """
        try:
            logger.info(f"🗑️ 删除123云盘文件: {file_path}")
            
            # 获取文件(夹)详细信息
            fileitem = self._storagechain.get_file_item(
                storage="123云盘", path=Path(file_path)
            )
            if fileitem.type == "dir":
                # 删除整个文件夹
                self._storagechain.delete_file(fileitem)
                logger.info(f"✅ {media_name} 删除网盘文件夹：{file_path}")
            else:
                # 调用 MP 模块删除媒体文件和空媒体目录
                self._storagechain.delete_media_file(fileitem=fileitem)
                logger.info(f"✅ {media_name} 删除网盘媒体文件：{file_path}")
        except Exception as e:
            logger.error(f"❌ {media_name} 删除网盘媒体 {file_path} 失败: {str(e)}")
            logger.error(traceback.format_exc())

    def __get_p115_media_suffix(self, file_path: str):
        """
        115网盘 遍历文件夹获取媒体文件后缀
        """
        try:
            logger.debug(f"🔍 获取115网盘文件后缀: {file_path}")
            _, sub_paths = self.__get_p115_media_path(file_path)
            if not sub_paths:
                logger.warning("⚠️ 未找到115路径映射")
                return None
                
            file_path = file_path.replace(sub_paths[0], sub_paths[2]).replace("\\", "/")
            file_dir = Path(file_path).parent
            file_basename = Path(file_path).stem
            
            logger.debug(f"🔍 查询目录: {file_dir}, 文件名: {file_basename}")
            
            file_dir_fileitem = self._storagechain.get_file_item(
                storage="u115", path=Path(file_dir)
            )
            
            for item in self._storagechain.list_files(file_dir_fileitem):
                if item.basename == file_basename:
                    logger.info(f"✅ 找到文件后缀: {item.extension}")
                    return item.extension
                    
            logger.warning("⚠️ 未找到匹配的文件")
            return None
            
        except Exception as e:
            logger.error(f"❌ 获取115文件后缀失败: {str(e)}")
            logger.error(traceback.format_exc())
            return None

    def __get_p123_media_suffix(self, file_path: str):
        """
        123云盘 遍历文件夹获取媒体文件后缀
        """
        try:
            logger.debug(f"🔍 获取123云盘文件后缀: {file_path}")
            _, sub_paths = self.__get_p123_media_path(file_path)
            if not sub_paths:
                logger.warning("⚠️ 未找到123路径映射")
                return None
                
            file_path = file_path.replace(sub_paths[0], sub_paths[2]).replace("\\", "/")
            file_dir = Path(file_path).parent
            file_basename = Path(file_path).stem
            
            logger.debug(f"🔍 查询目录: {file_dir}, 文件名: {file_basename}")
            
            file_dir_fileitem = self._storagechain.get_file_item(
                storage="123云盘", path=Path(file_dir)
            )
            
            for item in self._storagechain.list_files(file_dir_fileitem):
                if item.basename == file_basename:
                    logger.info(f"✅ 找到文件后缀: {item.extension}")
                    return item.extension
                    
            logger.warning("⚠️ 未找到匹配的文件")
            return None
            
        except Exception as e:
            logger.error(f"❌ 获取123文件后缀失败: {str(e)}")
            logger.error(traceback.format_exc())
            return None

    def __remove_parent_dir(self, file_path: Path):
        """
        删除父目录
        """
        try:
            logger.debug(f"🗑️ 检查并删除空目录: {file_path.parent}")
            
            # 删除空目录
            # 判断当前媒体父路径下是否有媒体文件，如有则无需遍历父级
            if not SystemUtils.exits_files(file_path.parent, settings.RMT_MEDIAEXT):
                # 判断父目录是否为空, 为空则删除
                i = 0
                for parent_path in file_path.parents:
                    i += 1
                    if i > 3:
                        break
                    if str(parent_path.parent) != str(file_path.root):
                        # 父目录非根目录，才删除父目录
                        if not SystemUtils.exits_files(parent_path, settings.RMT_MEDIAEXT):
                            # 当前路径下没有媒体文件则删除
                            try:
                                shutil.rmtree(parent_path)
                                logger.info(f"✅ 本地空目录 {parent_path} 已删除")
                            except Exception as e:
                                logger.warning(f"⚠️ 删除目录失败 {parent_path}: {str(e)}")
            else:
                logger.debug(f"📁 目录 {file_path.parent} 仍有媒体文件，跳过删除")
                
        except Exception as e:
            logger.error(f"❌ 删除父目录失败: {str(e)}")
            logger.error(traceback.format_exc())

    def __get_transfer_his(
        self,
        media_type: str,
        media_name: str,
        media_path: str,
        tmdb_id: int,
        season_num: str,
        episode_num: str,
    ):
        """
        查询转移记录
        """
        try:
            logger.debug(f"🔍 查询转移记录: {media_name}, TMDB: {tmdb_id}, 路径: {media_path}")
            
            # 季数
            if season_num and str(season_num).isdigit():
                season_num = str(season_num).rjust(2, "0")
                logger.debug(f"   季数格式化: {season_num}")
            else:
                season_num = None
                
            # 集数
            if episode_num and str(episode_num).isdigit():
                episode_num = str(episode_num).rjust(2, "0")
                logger.debug(f"   集数格式化: {episode_num}")
            else:
                episode_num = None

            # 类型
            mtype = MediaType.MOVIE if media_type in ["Movie", "MOV"] else MediaType.TV
            logger.debug(f"   媒体类型: {mtype}")

            # 删除电影
            if mtype == MediaType.MOVIE:
                msg = f"电影 {media_name} {tmdb_id}"
                logger.debug(f"   查询电影转移记录: tmdbid={tmdb_id}, dest={media_path}")
                transfer_history: List[TransferHistory] = self._transferhis.get_by(
                    tmdbid=tmdb_id, mtype=mtype.value, dest=media_path
                )
            # 删除电视剧
            elif mtype == MediaType.TV and not season_num and not episode_num:
                msg = f"剧集 {media_name} {tmdb_id}"
                logger.debug(f"   查询剧集转移记录: tmdbid={tmdb_id}")
                transfer_history: List[TransferHistory] = self._transferhis.get_by(
                    tmdbid=tmdb_id, mtype=mtype.value
                )
            # 删除季
            elif mtype == MediaType.TV and season_num and not episode_num:
                if not season_num or not str(season_num).isdigit():
                    logger.error(f"❌ {media_name} 季同步删除失败，未获取到具体季")
                    return "", []
                msg = f"剧集 {media_name} S{season_num} {tmdb_id}"
                logger.debug(f"   查询季转移记录: tmdbid={tmdb_id}, season=S{season_num}")
                transfer_history: List[TransferHistory] = self._transferhis.get_by(
                    tmdbid=tmdb_id, mtype=mtype.value, season=f"S{season_num}"
                )
            # 删除集
            elif mtype == MediaType.TV and season_num and episode_num:
                if (
                    not season_num
                    or not str(season_num).isdigit()
                    or not episode_num
                    or not str(episode_num).isdigit()
                ):
                    logger.error(f"❌ {media_name} 集同步删除失败，未获取到具体集")
                    return "", []
                msg = f"剧集 {media_name} S{season_num}E{episode_num} {tmdb_id}"
                logger.debug(f"   查询集转移记录: tmdbid={tmdb_id}, season=S{season_num}, episode=E{episode_num}")
                transfer_history: List[TransferHistory] = self._transferhis.get_by(
                    tmdbid=tmdb_id,
                    mtype=mtype.value,
                    season=f"S{season_num}",
                    episode=f"E{episode_num}",
                    dest=media_path,
                )
            else:
                logger.warning("⚠️ 未知的媒体类型或参数组合")
                return "", []
                
            if transfer_history:
                logger.info(f"✅ 查询到 {len(transfer_history)} 条转移记录")
                for i, his in enumerate(transfer_history[:3]):  # 只显示前3条记录
                    logger.debug(f"   记录{i+1}: ID={his.id}, 标题={his.title}, 路径={his.dest}")
                if len(transfer_history) > 3:
                    logger.debug(f"   ... 还有 {len(transfer_history)-3} 条记录")
            else:
                logger.warning("⚠️ 未查询到转移记录")
                
            return msg, transfer_history
            
        except Exception as e:
            logger.error(f"❌ 查询转移记录失败: {str(e)}")
            logger.error(traceback.format_exc())
            return "", []

    def __get_series_tmdb_id(self, series_id):
        """
        获取剧集 TMDB ID
        """
        try:
            logger.info(f"🔍 获取剧集TMDB ID, Series ID: {series_id}")
            
            if not self._emby_host or not self._emby_apikey or not self._emby_user:
                logger.error("❌ Emby服务器配置不完整")
                return None
                
            req_url = f"{self._emby_host}emby/Users/{self._emby_user}/Items/{series_id}?api_key={self._emby_apikey}"
            logger.debug(f"🌐 请求URL: {req_url}")
            
            with RequestUtils().get_res(req_url) as res:
                if res:
                    data = res.json()
                    tmdb_id = data.get("ProviderIds", {}).get("Tmdb")
                    if tmdb_id:
                        logger.info(f"✅ 获取到TMDB ID: {tmdb_id}")
                    else:
                        logger.warning("⚠️ 未找到TMDB ID")
                    return tmdb_id
                else:
                    logger.error("❌ 获取剧集 TMDB ID 失败，无法连接Emby！")
                    return None
        except Exception as e:
            logger.error(f"❌ 连接Items出错：{str(e)}")
            logger.error(traceback.format_exc())
            return None

    def handle_torrent(self, type: str, src: str, torrent_hash: str):
        """
        判断种子是否局部删除
        局部删除则暂停种子
        全部删除则删除种子
        """
        try:
            logger.info(f"🔧 处理种子任务: {torrent_hash}, 类型: {type}, 文件: {src}")
            
            download_id = torrent_hash
            download = self._default_downloader
            history_key = "%s-%s" % (download, torrent_hash)
            plugin_id = "TorrentTransfer"
            
            logger.debug(f"🔍 查询转种历史: {history_key}")
            transfer_history = self.get_data(key=history_key, plugin_id=plugin_id)
            logger.info(f"📋 查询到 {history_key} 转种历史: {transfer_history}")

            handle_torrent_hashs = []
            
            # 删除本次种子记录
            logger.debug(f"🗑️ 删除下载历史记录: {src}")
            self._downloadhis.delete_file_by_fullpath(fullpath=src)

            # 根据种子hash查询所有下载器文件记录
            logger.debug(f"🔍 查询种子文件记录: {torrent_hash}")
            download_files = self._downloadhis.get_files_by_hash(
                download_hash=torrent_hash
            )
            if not download_files:
                logger.warning(f"⚠️ 未查询到种子任务 {torrent_hash} 存在文件记录，未执行下载器文件同步或该种子已被删除")
                return False, False, []

            # 查询未删除数
            no_del_cnt = 0
            for download_file in download_files:
                if (
                    download_file
                    and download_file.state
                    and int(download_file.state) == 1
                ):
                    no_del_cnt += 1

            if no_del_cnt > 0:
                logger.info(f"⚠️ 查询种子任务 {torrent_hash} 存在 {no_del_cnt} 个未删除文件，执行暂停种子操作")
                delete_flag = False
            else:
                logger.info(f"✅ 查询种子任务 {torrent_hash} 文件已全部删除，执行删除种子操作")
                delete_flag = True

            # 如果有转种记录，则删除转种后的下载任务
            if transfer_history and isinstance(transfer_history, dict):
                download = transfer_history["to_download"]
                download_id = transfer_history["to_download_id"]
                delete_source = transfer_history["delete_source"]

                logger.info(f"🔄 处理转种记录: 目标下载器={download}, 目标ID={download_id}")

                # 删除种子
                if delete_flag:
                    # 删除转种记录
                    logger.debug(f"🗑️ 删除转种历史记录: {history_key}")
                    self.del_data(key=history_key, plugin_id=plugin_id)

                    # 转种后未删除源种时，同步删除源种
                    if not delete_source:
                        logger.info(f"🔄 {history_key} 转种时未删除源下载任务，开始删除源下载任务…")

                        # 删除源种子
                        logger.info(f"🗑️ 删除源下载器下载任务：{self._default_downloader} - {torrent_hash}")
                        self.chain.remove_torrents(torrent_hash)
                        handle_torrent_hashs.append(torrent_hash)

                    # 删除转种后任务
                    logger.info(f"🗑️ 删除转种后下载任务：{download} - {download_id}")
                    # 删除转种后下载任务
                    self.chain.remove_torrents(hashs=torrent_hash, downloader=download)
                    handle_torrent_hashs.append(download_id)
                else:
                    # 暂停种子
                    # 转种后未删除源种时，同步暂停源种
                    if not delete_source:
                        logger.info(f"🔄 {history_key} 转种时未删除源下载任务，开始暂停源下载任务…")

                        # 暂停源种子
                        logger.info(f"⏸️ 暂停源下载器下载任务：{self._default_downloader} - {torrent_hash}")
                        self.chain.stop_torrents(torrent_hash)
                        handle_torrent_hashs.append(torrent_hash)

                    logger.info(f"⏸️ 暂停转种后下载任务：{download} - {download_id}")
                    # 删除转种后下载任务
                    self.chain.stop_torrents(hashs=download_id, downloader=download)
                    handle_torrent_hashs.append(download_id)
            else:
                # 未转种的情况
                if delete_flag:
                    # 删除源种子
                    logger.info(f"🗑️ 删除源下载器下载任务：{download} - {download_id}")
                    self.chain.remove_torrents(download_id)
                else:
                    # 暂停源种子
                    logger.info(f"⏸️ 暂停源下载器下载任务：{download} - {download_id}")
                    self.chain.stop_torrents(download_id)
                handle_torrent_hashs.append(download_id)

            # 处理辅种
            handle_torrent_hashs = self.__del_seed(
                download_id=download_id,
                delete_flag=delete_flag,
                handle_torrent_hashs=handle_torrent_hashs,
            )
            
            # 处理合集
            if str(type) == "电视剧":
                handle_torrent_hashs = self.__del_collection(
                    src=src,
                    delete_flag=delete_flag,
                    torrent_hash=torrent_hash,
                    download_files=download_files,
                    handle_torrent_hashs=handle_torrent_hashs,
                )
                
            logger.info(f"✅ 种子处理完成: 删除={delete_flag}, 处理种子数={len(handle_torrent_hashs)}")
            return delete_flag, True, handle_torrent_hashs
            
        except Exception as e:
            logger.error(f"❌ 处理种子失败：{str(e)}")
            logger.error(traceback.format_exc())
            return False, False, []

    def __del_collection(
        self,
        src: str,
        delete_flag: bool,
        torrent_hash: str,
        download_files: list,
        handle_torrent_hashs: list,
    ):
        """
        处理做种合集
        """
        try:
            logger.info(f"🔗 处理合集种子: {torrent_hash}")
            
            src_download_files = self._downloadhis.get_files_by_fullpath(fullpath=src)
            if src_download_files:
                for download_file in src_download_files:
                    # src查询记录 判断download_hash是否不一致
                    if (
                        download_file
                        and download_file.download_hash
                        and str(download_file.download_hash) != str(torrent_hash)
                    ):
                        logger.info(f"🔍 发现合集种子: {download_file.download_hash}")
                        
                        # 查询新download_hash对应files数量
                        hash_download_files = self._downloadhis.get_files_by_hash(
                            download_hash=download_file.download_hash
                        )
                        # 新download_hash对应files数量 > 删种download_hash对应files数量 = 合集种子
                        if (
                            hash_download_files
                            and len(hash_download_files) > len(download_files)
                            and hash_download_files[0].id > download_files[-1].id
                        ):
                            logger.info(f"📊 合集种子统计: 新文件数={len(hash_download_files)}, 原文件数={len(download_files)}")
                            
                            # 查询未删除数
                            no_del_cnt = 0
                            for hash_download_file in hash_download_files:
                                if (
                                    hash_download_file
                                    and hash_download_file.state
                                    and int(hash_download_file.state) == 1
                                ):
                                    no_del_cnt += 1
                                    
                            if no_del_cnt > 0:
                                logger.info(f"⚠️ 合集种子 {download_file.download_hash} 文件未完全删除，执行暂停种子操作")
                                delete_flag = False

                            # 删除合集种子
                            if delete_flag:
                                self.chain.remove_torrents(
                                    hashs=download_file.download_hash,
                                    downloader=download_file.downloader,
                                )
                                logger.info(f"✅ 删除合集种子 {download_file.downloader} {download_file.download_hash}")
                            else:
                                # 暂停合集种子
                                self.chain.stop_torrents(
                                    hashs=download_file.download_hash,
                                    downloader=download_file.downloader,
                                )
                                logger.info(f"⏸️ 暂停合集种子 {download_file.downloader} {download_file.download_hash}")
                                
                            # 已处理种子+1
                            handle_torrent_hashs.append(download_file.download_hash)

                            # 处理合集辅种
                            handle_torrent_hashs = self.__del_seed(
                                download_id=download_file.download_hash,
                                delete_flag=delete_flag,
                                handle_torrent_hashs=handle_torrent_hashs,
                            )
            else:
                logger.debug("📭 未找到其他下载文件记录")
                
        except Exception as e:
            logger.error(f"❌ 处理 {torrent_hash} 合集失败: {str(e)}")
            logger.error(traceback.format_exc())

        return handle_torrent_hashs

    def __del_seed(self, download_id, delete_flag, handle_torrent_hashs):
        """
        删除辅种
        """
        try:
            logger.info(f"🔗 处理辅种: {download_id}")
            
            # 查询是否有辅种记录
            history_key = download_id
            plugin_id = "IYUUAutoSeed"
            
            logger.debug(f"🔍 查询辅种历史: {history_key}")
            seed_history = self.get_data(key=history_key, plugin_id=plugin_id) or []
            logger.info(f"📋 查询到 {history_key} 辅种历史: {len(seed_history)} 条")

            # 有辅种记录则处理辅种
            if seed_history and isinstance(seed_history, list):
                for i, history in enumerate(seed_history):
                    downloader = history.get("downloader")
                    torrents = history.get("torrents")
                    if not downloader or not torrents:
                        continue
                        
                    if not isinstance(torrents, list):
                        torrents = [torrents]

                    logger.info(f"🌱 处理第 {i+1} 条辅种记录: 下载器={downloader}, 种子数={len(torrents)}")

                    # 删除辅种历史
                    for torrent in torrents:
                        handle_torrent_hashs.append(torrent)
                        # 删除辅种
                        if delete_flag:
                            logger.info(f"🗑️ 删除辅种：{downloader} - {torrent}")
                            self.chain.remove_torrents(hashs=torrent, downloader=downloader)
                        # 暂停辅种
                        else:
                            self.chain.stop_torrents(hashs=torrent, downloader=downloader)
                            logger.info(f"⏸️ 暂停辅种：{downloader} - {torrent}")

                        # 处理辅种的辅种
                        handle_torrent_hashs = self.__del_seed(
                            download_id=torrent,
                            delete_flag=delete_flag,
                            handle_torrent_hashs=handle_torrent_hashs,
                        )

                # 删除辅种历史
                if delete_flag:
                    logger.debug(f"🗑️ 删除辅种历史记录: {history_key}")
                    self.del_data(key=history_key, plugin_id=plugin_id)
            else:
                logger.debug("📭 无辅种记录")
                
        except Exception as e:
            logger.error(f"❌ 处理辅种失败: {str(e)}")
            logger.error(traceback.format_exc())
            
        return handle_torrent_hashs

    def get_state(self):
        """获取插件状态"""
        logger.debug(f"📊 插件状态查询: enabled={self._enabled}")
        return self._enabled

    def stop_service(self):
        """
        退出插件
        """
        try:
            logger.info(f"🛑 停止插件服务: {self.plugin_name}")
            
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
                logger.info("✅ 计划任务已停止")
                
            logger.info(f"✅ 插件 {self.plugin_name} 已停止")
            
        except Exception as e:
            logger.error(f"❌ 退出插件失败：{str(e)}")
            logger.error(traceback.format_exc())

    @eventmanager.register(EventType.DownloadFileDeleted)
    def downloadfile_del_sync(self, event: Event):
        """
        下载文件删除处理事件
        """
        if not event:
            logger.debug("📭 收到空事件，跳过处理")
            return
            
        try:
            logger.info("🔔 收到下载文件删除事件")
            event_data = event.event_data
            src = event_data.get("src")
            
            if not src:
                logger.warning("⚠️ 事件中未找到文件路径")
                return
                
            logger.info(f"🗑️ 处理删除文件: {src}")
            
            # 查询下载hash
            download_hash = self._downloadhis.get_hash_by_fullpath(src)
            if download_hash:
                logger.info(f"🔍 找到下载记录: {download_hash}")
                download_history = self._downloadhis.get_by_hash(download_hash)
                if download_history:
                    self.handle_torrent(
                        type=download_history.type, src=src, torrent_hash=download_hash
                    )
                else:
                    logger.warning(f"⚠️ 未找到下载历史记录: {download_hash}")
            else:
                logger.warning(f"⚠️ 未查询到文件 {src} 对应的下载记录")
                
        except Exception as e:
            logger.error(f"❌ 处理下载文件删除事件失败: {str(e)}")
            logger.error(traceback.format_exc())

    @staticmethod
    def get_tmdbimage_url(path: str, prefix="w500"):
        """
        获取 TMDB 图片地址
        """
        if not path:
            logger.debug("📭 图片路径为空")
            return ""
            
        tmdb_image_url = f"https://{settings.TMDB_IMAGE_DOMAIN}"
        url = tmdb_image_url + f"/t/p/{prefix}{path}"
        logger.debug(f"🖼️ 生成TMDB图片URL: {url}")
        return url