"""
DockerCopilotHelper V2 版本 - 单文件版本
"""
from datetime import datetime, timedelta
from typing import Optional, Any, List, Dict, Tuple
from dataclasses import dataclass, field
import time
import pytz
import jwt

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils
import requests
from requests import Response


@dataclass
class DockerContainer:
    """Docker容器信息"""
    id: str
    name: str
    status: str
    haveUpdate: bool
    usingImage: Optional[str] = None
    runningTime: Optional[str] = None
    createTime: Optional[str] = None


@dataclass
class PluginConfig:
    """插件配置"""
    enabled: bool = False
    onlyonce: bool = False
    host: str = ""
    secretKey: str = ""
    
    # 更新通知
    update_cron: Optional[str] = None
    updatable_list: List[str] = field(default_factory=list)
    updatable_notify: bool = False
    
    # 自动更新
    auto_update_cron: Optional[str] = None
    auto_update_list: List[str] = field(default_factory=list)
    auto_update_notify: bool = False
    schedule_report: bool = False
    delete_images: bool = False
    interval: int = 10
    intervallimit: int = 6
    
    # 备份
    backup_cron: Optional[str] = None
    backups_notify: bool = False


class DockerCopilotHelper(_PluginBase):
    # 插件名称
    plugin_name = "DC助手AI版"
    # 插件描述
    plugin_desc = "配合DockerCopilot,完成更新通知、自动更改、自动备份功能"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/siryle/MoviePilot-Plugins/main/icons/Docker_Copilot.png"
    # 插件版本
    plugin_version = "2.0.0"
    # 插件作者
    plugin_author = "gxterry"
    # 作者主页
    author_url = "https://github.com/gxterry"
    # 插件配置项ID前缀
    plugin_config_prefix = "dockercopilothelper_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1

    def __init__(self):
        super().__init__()
        self.config = PluginConfig()
        self._scheduler: Optional[BackgroundScheduler] = None
        self._running_tasks: Dict[str, Any] = {}
        
        # 版本检测
        self.is_v2 = hasattr(settings, 'VERSION_FLAG') and settings.VERSION_FLAG == "v2"

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        # 停止现有服务
        self.stop_service()
        
        # 加载配置
        if config:
            # 适配字段名
            adapted_config = {}
            for key, value in config.items():
                # 将V1的字段名映射到V2
                field_mapping = {
                    "updatecron": "update_cron",
                    "updatablelist": "updatable_list",
                    "updatablenotify": "updatable_notify",
                    "autoupdatecron": "auto_update_cron",
                    "autoupdatelist": "auto_update_list",
                    "autoupdatenotify": "auto_update_notify",
                    "schedulereport": "schedule_report",
                    "deleteimages": "delete_images",
                    "backupcron": "backup_cron",
                    "backupsnotify": "backups_notify",
                    "intervallimit": "interval_limit",
                    "interval": "interval"
                }
                
                new_key = field_mapping.get(key, key)
                adapted_config[new_key] = value
            
            self.config = PluginConfig(**adapted_config)
            
            # 检查必要配置
            if not self.config.secretKey or not self.config.host:
                logger.error("DC助手: secretKey 或 host 未配置")
                return
        
        # 启用插件
        if self.config.enabled or self.config.onlyonce:
            self._setup_scheduler()
            
            # 立即运行一次
            if self.config.onlyonce:
                self._run_once()
                self.config.onlyonce = False
                self._save_config()
            
            logger.info(f"DC助手插件已初始化，版本: {'V2' if self.is_v2 else 'V1兼容'}")

    def _get_jwt_token(self) -> str:
        """获取JWT令牌"""
        payload = {
            "exp": int(time.time()) + 28 * 24 * 60 * 60,
            "iat": int(time.time())
        }
        token = jwt.encode(payload, self.config.secretKey, algorithm="HS256")
        return f"Bearer {token}"

    def _make_request(self, method: str, url: str, **kwargs) -> Optional[Response]:
        """发送HTTP请求"""
        headers = kwargs.pop('headers', {})
        headers['Authorization'] = self._get_jwt_token()
        
        try:
            if method.upper() == "GET":
                return RequestUtils(headers=headers).get_res(url, **kwargs)
            elif method.upper() == "POST":
                return RequestUtils(headers=headers).post_res(url, **kwargs)
            elif method.upper() == "DELETE":
                # 使用requests直接调用
                headers.update(kwargs.get('headers', {}))
                return requests.delete(
                    url,
                    headers=headers,
                    timeout=20,
                    verify=False
                )
            else:
                logger.error(f"不支持的HTTP方法: {method}")
                return None
        except Exception as e:
            logger.error(f"API请求失败: {str(e)}")
            return None

    def get_containers(self) -> List[DockerContainer]:
        """获取容器列表"""
        try:
            url = f"{self.config.host.rstrip('/')}/api/containers"
            response = self._make_request("GET", url)
            
            if response is None:
                return []
                
            data = response.json()
            if data.get("code") == 0 and data.get("data"):
                return [
                    DockerContainer(
                        id=container.get("id", ""),
                        name=container.get("name", ""),
                        status=container.get("status", ""),
                        haveUpdate=container.get("haveUpdate", False),
                        usingImage=container.get("usingImage"),
                        runningTime=container.get("runningTime"),
                        createTime=container.get("createTime")
                    )
                    for container in data["data"]
                ]
            return []
        except Exception as e:
            logger.error(f"获取容器列表失败: {str(e)}")
            return []

    def update_container(self, container_id: str, container_name: str, 
                        image_name: str) -> Optional[Dict[str, Any]]:
        """更新容器"""
        try:
            url = f"{self.config.host.rstrip('/')}/api/container/{container_id}/update"
            response = self._make_request(
                "POST",
                url,
                json={
                    "containerName": container_name,
                    "imageNameAndTag": [image_name]
                }
            )
            
            if response is None:
                return None
                
            data = response.json()
            if data.get("code") == 200 and data.get("data"):
                return {
                    "taskID": data["data"].get("taskID"),
                    "containerName": container_name,
                    "imageName": image_name
                }
            return None
        except Exception as e:
            logger.error(f"更新容器失败: {str(e)}")
            return None

    def get_update_progress(self, task_id: str) -> str:
        """获取更新进度"""
        try:
            url = f"{self.config.host.rstrip('/')}/api/progress/{task_id}"
            response = self._make_request("GET", url)
            
            if response is None:
                return "请求失败"
                
            data = response.json()
            return data.get("msg", "未知状态")
        except Exception as e:
            logger.error(f"获取更新进度失败: {str(e)}")
            return "获取失败"

    def backup_containers(self) -> bool:
        """备份容器"""
        try:
            url = f"{self.config.host.rstrip('/')}/api/container/backup"
            response = self._make_request("GET", url)
            
            if response is None:
                return False
                
            data = response.json()
            return data.get("code") == 200
        except Exception as e:
            logger.error(f"备份容器失败: {str(e)}")
            return False

    def get_images(self) -> List[Dict[str, Any]]:
        """获取镜像列表"""
        try:
            url = f"{self.config.host.rstrip('/')}/api/images"
            response = self._make_request("GET", url)
            
            if response is None:
                return []
                
            data = response.json()
            if data.get("code") == 200 and data.get("data"):
                return data["data"]
            return []
        except Exception as e:
            logger.error(f"获取镜像列表失败: {str(e)}")
            return []

    def remove_image(self, image_id: str) -> bool:
        """删除镜像"""
        try:
            url = f"{self.config.host.rstrip('/')}/api/image/{image_id}?force=false"
            response = self._make_request("DELETE", url)
            
            if response is None:
                return False
                
            data = response.json()
            return data.get("code") == 200
        except Exception as e:
            logger.error(f"删除镜像失败: {str(e)}")
            return False

    def _setup_scheduler(self):
        """设置定时任务"""
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        
        # 备份任务
        if self.config.backup_cron:
            try:
                self._scheduler.add_job(
                    func=self._backup_containers,
                    trigger=CronTrigger.from_crontab(self.config.backup_cron),
                    name="DC助手-备份"
                )
                logger.info(f"DC助手备份任务已安排: {self.config.backup_cron}")
            except Exception as e:
                logger.error(f"备份任务配置错误: {str(e)}")
                self.post_message(
                    mtype=NotificationType.System,
                    title="DC助手配置错误",
                    text=f"备份周期配置错误: {str(e)}"
                )
        
        # 更新通知任务
        if self.config.update_cron:
            try:
                self._scheduler.add_job(
                    func=self._check_updates,
                    trigger=CronTrigger.from_crontab(self.config.update_cron),
                    name="DC助手-更新通知"
                )
                logger.info(f"DC助手更新通知任务已安排: {self.config.update_cron}")
            except Exception as e:
                logger.error(f"更新通知任务配置错误: {str(e)}")
                self.post_message(
                    mtype=NotificationType.System,
                    title="DC助手配置错误",
                    text=f"更新通知周期配置错误: {str(e)}"
                )
        
        # 自动更新任务
        if self.config.auto_update_cron:
            try:
                self._scheduler.add_job(
                    func=self._auto_update,
                    trigger=CronTrigger.from_crontab(self.config.auto_update_cron),
                    name="DC助手-自动更新"
                )
                logger.info(f"DC助手自动更新任务已安排: {self.config.auto_update_cron}")
            except Exception as e:
                logger.error(f"自动更新任务配置错误: {str(e)}")
                self.post_message(
                    mtype=NotificationType.System,
                    title="DC助手配置错误",
                    text=f"自动更新周期配置错误: {str(e)}"
                )
        
        # 启动调度器
        if self._scheduler.get_jobs():
            self._scheduler.start()
            logger.info(f"DC助手调度器已启动，共 {len(self._scheduler.get_jobs())} 个任务")

    def _run_once(self):
        """立即运行一次所有任务"""
        logger.info("DC助手立即执行一次所有任务")
        
        if self.config.backup_cron:
            self._scheduler.add_job(
                func=self._backup_containers,
                trigger='date',
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="DC助手-立即备份"
            )
        
        if self.config.update_cron:
            self._scheduler.add_job(
                func=self._check_updates,
                trigger='date',
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=6),
                name="DC助手-立即检查更新"
            )
        
        if self.config.auto_update_cron:
            self._scheduler.add_job(
                func=self._auto_update,
                trigger='date',
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=10),
                name="DC助手-立即自动更新"
            )

    def _check_updates(self):
        """检查更新并发送通知"""
        logger.info("DC助手开始检查容器更新")
        
        containers = self.get_containers()
        updatable_containers = [
            container for container in containers
            if container.haveUpdate and container.name in self.config.updatable_list
        ]
        
        for container in updatable_containers:
            if container.usingImage and not container.usingImage.startswith("sha256:"):
                # 发送更新通知
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【DC助手-更新通知】",
                    text=f"您有容器可以更新啦！\n"
                         f"【{container.name}】\n"
                         f"当前镜像: {container.usingImage}\n"
                         f"状态: {container.status} {container.runningTime}\n"
                         f"构建时间: {container.createTime}"
                )
                logger.info(f"发送更新通知: {container.name}")
            else:
                # TAG不正确的容器
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【DC助手-更新通知】",
                    text=f"监测到您有容器TAG不正确\n"
                         f"【{container.name}】\n"
                         f"当前镜像: {container.usingImage}\n"
                         f"状态: {container.status} {container.runningTime}\n"
                         f"构建时间: {container.createTime}\n"
                         f"该镜像无法通过DC自动更新,请修改TAG"
                )
                logger.warning(f"容器TAG不正确: {container.name}")

    def _auto_update(self):
        """自动更新容器"""
        logger.info("DC助手开始自动更新容器")
        
        # 清理未使用的镜像
        if self.config.delete_images:
            self._cleanup_unused_images()
        
        # 获取容器列表
        containers = self.get_containers()
        
        # 更新选中的容器
        for container_name in self.config.auto_update_list:
            container = next(
                (c for c in containers if c.name == container_name and c.haveUpdate),
                None
            )
            
            if not container:
                continue
            
            # 检查镜像TAG
            if not container.usingImage or container.usingImage.startswith("sha256:"):
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【DC助手-自动更新】",
                    text=f"监测到您有容器TAG不正确\n"
                         f"【{container.name}】\n"
                         f"当前镜像: {container.usingImage}\n"
                         f"状态: {container.status} {container.runningTime}\n"
                         f"构建时间: {container.createTime}\n"
                         f"该镜像无法通过DC自动更新,请修改TAG"
                )
                continue
            
            # 执行更新
            task = self.update_container(
                container_id=container.id,
                container_name=container.name,
                image_name=container.usingImage
            )
            
            if task:
                if self.config.auto_update_notify:
                    self.post_message(
                        mtype=NotificationType.Plugin,
                        title="【DC助手-自动更新】",
                        text=f"【{container.name}】\n容器更新任务创建成功"
                    )
                
                # 跟踪进度
                if self.config.schedule_report:
                    self._track_update_progress(task)
            else:
                logger.error(f"创建更新任务失败: {container.name}")

    def _track_update_progress(self, task: Dict[str, Any]):
        """跟踪更新进度"""
        logger.info(f"开始跟踪更新进度: {task.get('taskID')}")
        
        iteration = 0
        while iteration < self.config.intervallimit:
            try:
                progress = self.get_update_progress(task.get('taskID', ''))
                
                if self.config.auto_update_notify:
                    self.post_message(
                        mtype=NotificationType.Plugin,
                        title="【DC助手-更新进度】",
                        text=f"【{task.get('containerName')}】\n进度: {progress}"
                    )
                
                if progress == "更新成功":
                    logger.info(f"更新完成: {task.get('containerName')}")
                    break
                
                iteration += 1
                time.sleep(self.config.interval)
                
            except Exception as e:
                logger.error(f"跟踪更新进度失败: {str(e)}")
                break
        
        if iteration >= self.config.intervallimit:
            logger.warning(f"更新进度跟踪超时: {task.get('containerName')}")
            self.post_message(
                mtype=NotificationType.Plugin,
                title="【DC助手-更新跟踪】",
                text=f"【{task.get('containerName')}】\n更新跟踪超时，可能仍在进行中"
            )

    def _cleanup_unused_images(self):
        """清理未使用的镜像"""
        logger.info("开始清理未使用的镜像")
        
        images = self.get_images()
        cleaned_count = 0
        
        for image in images:
            if not image.get("inUsed", False) and image.get("tag"):
                if self.remove_image(image.get("id", "")):
                    cleaned_count += 1
                    logger.info(f"清理镜像: {image.get('id', '')[:12]} ({image.get('tag')})")
        
        if cleaned_count > 0:
            logger.info(f"共清理 {cleaned_count} 个未使用镜像")

    def _backup_containers(self):
        """备份容器"""
        logger.info("开始备份容器")
        
        success = self.backup_containers()
        
        if success:
            if self.config.backups_notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【DC助手-备份成功】",
                    text="镜像备份成功！"
                )
            logger.info("容器备份成功")
        else:
            if self.config.backups_notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【DC助手 备份失败】",
                    text="镜像备份失败，请检查日志"
                )
            logger.error("容器备份失败")

    def _save_config(self):
        """保存配置"""
        # 反向映射回V1字段名
        config_dict = {}
        for field, value in self.config.__dict__.items():
            if field == "update_cron":
                config_dict["updatecron"] = value
            elif field == "updatable_list":
                config_dict["updatablelist"] = value
            elif field == "updatable_notify":
                config_dict["updatablenotify"] = value
            elif field == "auto_update_cron":
                config_dict["autoupdatecron"] = value
            elif field == "auto_update_list":
                config_dict["autoupdatelist"] = value
            elif field == "auto_update_notify":
                config_dict["autoupdatenotify"] = value
            elif field == "schedule_report":
                config_dict["schedulereport"] = value
            elif field == "delete_images":
                config_dict["deleteimages"] = value
            elif field == "backup_cron":
                config_dict["backupcron"] = value
            elif field == "backups_notify":
                config_dict["backupsnotify"] = value
            elif field == "intervallimit":
                config_dict["intervallimit"] = value
            elif field == "interval":
                config_dict["interval"] = value
            else:
                config_dict[field] = value
        
        self.update_config(config_dict)

    def get_state(self) -> bool:
        """获取插件状态"""
        return self.config.enabled

    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event):
        """处理插件动作事件"""
        if not event or not event.event_data:
            return
        
        action = event.event_data.get("action")
        
        if action == "check_updates":
            self._check_updates()
        elif action == "backup":
            self._backup_containers()
        elif action == "auto_update":
            self._auto_update()

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """获取配置表单 - V2版本需要返回两个值"""
        # 动态获取容器列表
        updatable_list = []
        auto_update_list = []
        
        if self.config.secretKey and self.config.host:
            containers = self.get_containers()
            container_names = [c.name for c in containers]
            
            # 清理无效的配置项
            self.config.updatable_list = [
                name for name in self.config.updatable_list 
                if name in container_names
            ]
            self.config.auto_update_list = [
                name for name in self.config.auto_update_list 
                if name in container_names
            ]
            
            if self.config.updatable_list or self.config.auto_update_list:
                self._save_config()
            
            for container in containers:
                updatable_list.append({"title": container.name, "value": container.name})
                auto_update_list.append({"title": container.name, "value": container.name})
        
        # 表单配置
        form_items = [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VSwitch',
                                'props': {
                                    'model': 'enabled',
                                    'label': '启用插件',
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VSwitch',
                                'props': {
                                    'model': 'onlyonce',
                                    'label': '立即运行一次',
                                }
                            }
                        ]
                    }
                ]
            }, {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'host',
                                    'label': '服务器地址',
                                    'placeholder': 'http://127.0.0.1:端口',
                                    'hint': 'dockerCopilot服务地址 http(s)://ip:端口'
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'secretKey',
                                    'label': 'secretKey',
                                    'type': 'password',
                                    'hint': 'dockerCopilot秘钥 环境变量查看'
                                }
                            }
                        ]
                    }
                ]
            },
            {
                'component': 'VTabs',
                'props': {
                    'model': '_tabs',
                    'height': 40,
                    'style': {
                        'margin-top': '20px',
                        'margin-bottom': '60px',
                        'margin-right': '30px'
                    }
                },
                'content': [{
                    'component': 'VTab',
                    'props': {'value': 'C1'},
                    'text': '更新通知'
                },
                    {
                        'component': 'VTab',
                        'props': {'value': 'C2'},
                        'text': '自动更新'
                    },
                    {
                        'component': 'VTab',
                        'props': {'value': 'C3'},
                        'text': '自动备份'
                    }
                ]
            },
            {
                'component': 'VWindow',
                'props': {
                    'model': '_tabs'
                },
                'content': [{
                    'component': 'VWindowItem',
                    'props': {
                        'value': 'C1', 'style': {'margin-top': '30px'}
                    },
                    'content': [{
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'updatecron',
                                            'label': '更新通知周期',
                                            'placeholder': '15 8-23/2 * * *',
                                            'hint': 'Cron表达式，如：每天8点到23点每2小时第15分钟检查'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                        {
                            "component": "VRow",
                            "content": [
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12
                                    },
                                    'content': [
                                        {
                                            'component': 'VSelect',
                                            'props': {
                                                'chips': True,
                                                'multiple': True,
                                                'model': 'updatablelist',
                                                'label': '更新通知容器',
                                                'items': updatable_list,
                                                'hint': '选择容器在有更新时发送通知'
                                            }
                                        }
                                    ]
                                }
                            ],
                        },
                        {
                            "component": "VRow",
                            "content": [
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12,
                                        'md': 6
                                    },
                                    'content': [
                                        {
                                            'component': 'VSwitch',
                                            'props': {
                                                'model': 'updatablenotify',
                                                'label': '发送通知',
                                                'hint': '开启后，当检测到更新时发送通知'
                                            }
                                        }
                                    ]
                                }
                            ]
                        }]
                },
                    {
                        'component': 'VWindowItem',
                        'props': {'value': 'C2', 'style': {'margin-top': '30px'}},
                        'content': [
                            {
                                'component': 'VRow',
                                'content': [
                                    {
                                        'component': 'VCol',
                                        'props': {
                                            'cols': 12,
                                            'md': 6
                                        },
                                        'content': [
                                            {
                                                'component': 'VTextField',
                                                'props': {
                                                    'model': 'autoupdatecron',
                                                    'label': '自动更新周期',
                                                    'placeholder': '15 2 * * *',
                                                    'hint': 'Cron表达式，如：每天凌晨2点15分自动更新'
                                                }
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VCol',
                                        'props': {
                                            'cols': 12,
                                            'md': 3
                                        },
                                        'content': [
                                            {
                                                'component': 'VTextField',
                                                'props': {
                                                    'model': 'interval',
                                                    'label': '跟踪间隔(秒)',
                                                    'placeholder': '10',
                                                    'type': 'number',
                                                    'hint': '开启进度汇报时,每多少秒检查一次进度状态，默认10秒'
                                                }
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VCol',
                                        'props': {
                                            'cols': 12,
                                            'md': 3
                                        },
                                        'content': [
                                            {
                                                'component': 'VTextField',
                                                'props': {
                                                    'model': 'intervallimit',
                                                    'label': '检查次数',
                                                    'placeholder': '6',
                                                    'type': 'number',
                                                    'hint': '开启进度汇报，当达限制检查次数后放弃追踪,默认6次'
                                                }
                                            }
                                        ]
                                    }
                                ]},
                            {
                                'component': 'VRow',
                                'content': [
                                    {
                                        'component': 'VCol',
                                        'props': {
                                            'cols': 12,
                                            'md': 4
                                        },
                                        'content': [
                                            {
                                                'component': 'VSwitch',
                                                'props': {
                                                    'model': 'autoupdatenotify',
                                                    'label': '自动更新通知',
                                                    'hint': '更新任务创建成功发送通知'
                                                }
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VCol',
                                        'props': {
                                            'cols': 12,
                                            'md': 4
                                        },
                                        'content': [
                                            {
                                                'component': 'VSwitch',
                                                'props': {
                                                    'model': 'schedulereport',
                                                    'label': '进度汇报',
                                                    'hint': '追踪更新任务进度并发送通知'
                                                }
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VCol',
                                        'props': {
                                            'cols': 12,
                                            'md': 4
                                        },
                                        'content': [
                                            {
                                                'component': 'VSwitch',
                                                'props': {
                                                    'model': 'deleteimages',
                                                    'label': '清理镜像',
                                                    'hint': '在下次执行时清理无tag且不在使用中的全部镜像'
                                                }
                                            }
                                        ]
                                    },
                                ]},
                            {
                                "component": "VRow",
                                "content": [
                                    {
                                        'component': 'VCol',
                                        'props': {
                                            'cols': 12
                                        },
                                        'content': [
                                            {
                                                'component': 'VSelect',
                                                'props': {
                                                    'chips': False,
                                                    'multiple': True,
                                                    'model': 'autoupdatelist',
                                                    'label': '自动更新容器',
                                                    'items': auto_update_list,
                                                    'hint': '被选则的容器当有新版本时自动更新'
                                                }
                                            }
                                        ]
                                    }
                                ],
                            }, ]
                    },
                    {
                        'component': 'VWindowItem',
                        'props': {
                            'value': 'C3',
                            'style': {'margin-top': '30px'}
                        },
                        'content': [{
                            "component": "VRow",
                            "content": [
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12,
                                        'md': 6
                                    },
                                    'content': [
                                        {
                                            'component': 'VTextField',
                                            'props': {
                                                'model': 'backupcron',
                                                'label': '自动备份周期',
                                                'placeholder': '0 7 * * *',
                                                'hint': 'Cron表达式，如：每天7点整自动备份'
                                            }
                                        }
                                    ]
                                },
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12,
                                        'md': 6
                                    },
                                    'content': [
                                        {
                                            'component': 'VSwitch',
                                            'props': {
                                                'model': 'backupsnotify',
                                                'label': '备份通知',
                                                'hint': '备份成功发送通知'
                                            }
                                        }
                                    ]
                                }
                            ]}]
                    }]
            }
        ]
        
        # 默认配置值
        default_config = {
            "enabled": self.config.enabled,
            "onlyonce": self.config.onlyonce,
            "host": self.config.host,
            "secretKey": self.config.secretKey,
            "updatecron": self.config.update_cron,
            "updatablelist": self.config.updatable_list,
            "updatablenotify": self.config.updatable_notify,
            "autoupdatecron": self.config.auto_update_cron,
            "autoupdatelist": self.config.auto_update_list,
            "autoupdatenotify": self.config.auto_update_notify,
            "schedulereport": self.config.schedule_report,
            "deleteimages": self.config.delete_images,
            "backupcron": self.config.backup_cron,
            "backupsnotify": self.config.backups_notify,
            "interval": self.config.interval,
            "intervallimit": self.config.intervallimit,
            "_tabs": "C1"  # 默认选中的标签页
        }
        
        # V2版本需要返回两个值：表单配置和默认值
        return [
            {
                "component": "VForm",
                "content": form_items
            }
        ], default_config

    def get_page(self) -> List[dict]:
        """获取页面配置 - 提供详情页面"""
        return [
            {
                "component": "VCard",
                "props": {
                    "title": "DC助手状态",
                    "subtitle": f"版本: {self.plugin_version}",
                    "style": {"margin-bottom": "20px"}
                },
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {
                                            "text": f"插件状态: {'已启用' if self.config.enabled else '未启用'}"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {
                                            "text": f"API连接: {'正常' if self.config.host and self.config.secretKey else '未配置'}"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {
                                            "text": f"定时任务: {len(self._scheduler.get_jobs()) if self._scheduler else 0} 个"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {
                                            "text": f"监控容器: {len(self.config.updatable_list)} 个"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {
                                            "text": f"自动更新: {len(self.config.auto_update_list)} 个"
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VCard",
                "props": {
                    "title": "操作",
                    "style": {"margin-bottom": "20px"}
                },
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VBtn",
                                        "props": {
                                            "block": True,
                                            "color": "primary",
                                            "onClick": "checkUpdates"
                                        },
                                        "text": "立即检查更新"
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VBtn",
                                        "props": {
                                            "block": True,
                                            "color": "success",
                                            "onClick": "runBackup"
                                        },
                                        "text": "立即备份"
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VBtn",
                                        "props": {
                                            "block": True,
                                            "color": "warning",
                                            "onClick": "runAutoUpdate"
                                        },
                                        "text": "立即自动更新"
                                    }
                                ]
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VCard",
                "props": {
                    "title": "帮助",
                    "style": {"margin-bottom": "20px"}
                },
                "content": [
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "div",
                                "props": {
                                    "innerHTML": """
                                    <h4>插件功能说明：</h4>
                                    <ol>
                                        <li><strong>更新通知</strong>: 定时检查Docker容器更新并发送通知</li>
                                        <li><strong>自动更新</strong>: 自动更新指定的Docker容器</li>
                                        <li><strong>自动备份</strong>: 定时备份Docker容器镜像</li>
                                    </ol>
                                    <h4>使用说明：</h4>
                                    <ul>
                                        <li>1. 填写正确的DockerCopilot服务器地址和密钥</li>
                                        <li>2. 在相应标签页配置定时任务和容器选择</li>
                                        <li>3. 启用插件并保存配置</li>
                                        <li>4. 可以在详情页手动执行各项操作</li>
                                    </ul>
                                    """
                                }
                            }
                        ]
                    }
                ]
            }
        ]

    def stop_service(self):
        """停止插件服务"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
                logger.info("DC助手调度器已停止")
        except Exception as e:
            logger.error(f"停止DC助手服务失败: {str(e)}")

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """获取命令"""
        return [
            {
                "cmd": "/dc_check",
                "event": EventType.PluginAction,
                "desc": "检查容器更新",
                "data": {
                    "action": "check_updates"
                }
            },
            {
                "cmd": "/dc_backup",
                "event": EventType.PluginAction,
                "desc": "备份容器",
                "data": {
                    "action": "backup"
                }
            },
            {
                "cmd": "/dc_update",
                "event": EventType.PluginAction,
                "desc": "执行自动更新",
                "data": {
                    "action": "auto_update"
                }
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """获取API"""
        return [
            {
                "path": "/check_updates",
                "endpoint": self.check_updates_api,
                "methods": ["GET"],
                "summary": "检查容器更新",
                "description": "手动触发检查容器更新"
            },
            {
                "path": "/backup",
                "endpoint": self.backup_api,
                "methods": ["GET"],
                "summary": "备份容器",
                "description": "手动触发备份容器"
            },
            {
                "path": "/auto_update",
                "endpoint": self.auto_update_api,
                "methods": ["GET"],
                "summary": "执行自动更新",
                "description": "手动触发自动更新"
            }
        ]

    def check_updates_api(self):
        """API: 检查更新"""
        try:
            self._check_updates()
            return {"code": 0, "message": "检查更新任务已触发"}
        except Exception as e:
            return {"code": 1, "message": f"检查更新失败: {str(e)}"}

    def backup_api(self):
        """API: 备份容器"""
        try:
            self._backup_containers()
            return {"code": 0, "message": "备份任务已触发"}
        except Exception as e:
            return {"code": 1, "message": f"备份失败: {str(e)}"}

    def auto_update_api(self):
        """API: 自动更新"""
        try:
            self._auto_update()
            return {"code": 0, "message": "自动更新任务已触发"}
        except Exception as e:
            return {"code": 1, "message": f"自动更新失败: {str(e)}"}