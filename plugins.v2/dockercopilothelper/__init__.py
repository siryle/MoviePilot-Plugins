# plugins.v2/dockercopilothelper/__init__.py
"""
DockerCopilotHelper插件 - V2版本
修复了get_page()方法和auto_update中的int()转换错误
"""

import time
import jwt
import requests
import traceback
from datetime import datetime, timedelta
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# 导入必要的模块
from app.plugins import _PluginBase
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils


class DockerCopilotHelper(_PluginBase):
    # 插件基本信息
    plugin_name = "DC助手AI版"
    plugin_desc = "配合DockerCopilot,完成更新通知、自动更改、自动备份功能"
    plugin_icon = "https://raw.githubusercontent.com/siryle/MoviePilot-Plugins/main/icons/Docker_Copilot.png"
    plugin_version = "2.0.0"  # 更新版本号
    plugin_author = "gxterry"
    author_url = "https://github.com/gxterry"
    plugin_config_prefix = "dockercopilothelper_"
    plugin_order = 15
    auth_level = 1

    # 私有属性
    _enabled = False
    _onlyonce = False
    _update_cron = None
    _updatable_list = []
    _updatable_notify = False
    _schedule_report = False
    _auto_update_cron = None
    _auto_update_list = []
    _auto_update_notify = False
    _delete_images = False
    _intervallimit = 6  # 默认值
    _interval = 10  # 默认值
    _backup_cron = None
    _backups_notify = False
    _host = None
    _secretKey = None
    _scheduler = None
    
    # 记录统计信息
    _update_success_count = 0
    _update_fail_count = 0
    _backup_success_count = 0
    _backup_fail_count = 0
    _notify_sent_count = 0
    _notify_failed_count = 0
    _cleanup_success_count = 0
    _cleanup_fail_count = 0

    def __init__(self):
        """初始化插件"""
        super().__init__()
        logger.info(f"DC助手AI版插件初始化 - 版本: {self.plugin_version}")

    def init_plugin(self, config: dict = None):
        """初始化插件配置"""
        logger.info("DC助手AI版插件初始化开始")
        
        # 停止现有服务
        self.stop_service()
        
        try:
            if config:
                self._enabled = config.get("enabled", False)
                self._onlyonce = config.get("onlyonce", False)
                self._update_cron = config.get("updatecron")
                self._updatable_list = config.get("updatablelist", [])
                self._updatable_notify = config.get("updatablenotify", False)
                self._auto_update_cron = config.get("autoupdatecron")
                self._auto_update_list = config.get("autoupdatelist", [])
                self._auto_update_notify = config.get("autoupdatenotify", False)
                self._schedule_report = config.get("schedulereport", False)
                self._delete_images = config.get("deleteimages", False)
                self._backup_cron = config.get("backupcron")
                self._backups_notify = config.get("backupsnotify", False)
                # 修复：为 None 值提供默认值
                self._intervallimit = config.get("intervallimit", 6) or 6
                self._interval = config.get("interval", 10) or 10
                self._host = config.get("host", "")
                self._secretKey = config.get("secretKey", "")
                
                # 初始化统计信息
                self._update_success_count = config.get("update_success_count", 0)
                self._update_fail_count = config.get("update_fail_count", 0)
                self._backup_success_count = config.get("backup_success_count", 0)
                self._backup_fail_count = config.get("backup_fail_count", 0)
                self._notify_sent_count = config.get("notify_sent_count", 0)
                self._notify_failed_count = config.get("notify_failed_count", 0)
                self._cleanup_success_count = config.get("cleanup_success_count", 0)
                self._cleanup_fail_count = config.get("cleanup_fail_count", 0)
                
                logger.info(f"插件配置加载: enabled={self._enabled}, intervallimit={self._intervallimit}, interval={self._interval}")
                
                # 获取DC列表数据
                if not self._secretKey or not self._host:
                    logger.error("DC助手服务结束 secretKey或host未填写")
                    self._enabled = False
                    return

                # 加载模块
                if self._enabled or self._onlyonce:
                    # 定时服务
                    self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                    # 立即运行一次
                    if self._onlyonce:
                        logger.info("DC助手服务启动，立即运行一次")
                        if self._backup_cron:
                            self._scheduler.add_job(
                                self.backup, 
                                'date',
                                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                name="DC助手-备份"
                            )
                        if self._update_cron:
                            self._scheduler.add_job(
                                self.updatable,
                                'date',
                                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=6),
                                name="DC助手-更新通知"
                            )
                        if self._auto_update_cron:
                            self._scheduler.add_job(
                                self.auto_update,
                                'date',
                                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=10),
                                name="DC助手-自动更新"
                            )
                        # 关闭一次性开关
                        self._onlyonce = False
                        # 保存配置
                        self.__update_config()
                    
                    # 周期运行
                    if self._backup_cron:
                        try:
                            self._scheduler.add_job(
                                func=self.backup,
                                trigger=CronTrigger.from_crontab(self._backup_cron),
                                name="DC助手-备份"
                            )
                        except Exception as err:
                            logger.error(f"定时任务配置错误：{str(err)}")
                    
                    if self._update_cron:
                        try:
                            self._scheduler.add_job(
                                func=self.updatable,
                                trigger=CronTrigger.from_crontab(self._update_cron),
                                name="DC助手-更新通知"
                            )
                        except Exception as err:
                            logger.error(f"定时任务配置错误：{str(err)}")
                    
                    if self._auto_update_cron:
                        try:
                            self._scheduler.add_job(
                                func=self.auto_update,
                                trigger=CronTrigger.from_crontab(self._auto_update_cron),
                                name="DC助手-自动更新"
                            )
                        except Exception as err:
                            logger.error(f"定时任务配置错误：{str(err)}")
                    
                    # 启动任务
                    if self._scheduler.get_jobs():
                        self._scheduler.print_jobs()
                        self._scheduler.start()
                        logger.info(f"定时服务已启动，共 {len(self._scheduler.get_jobs())} 个任务")
            else:
                logger.warning("插件配置为空，使用默认配置")
                
        except Exception as e:
            logger.error(f"插件初始化异常: {str(e)}")
            logger.error(traceback.format_exc())
        
        logger.info("DC助手AI版插件初始化完成")

    def get_state(self) -> bool:
        """获取插件状态"""
        return self._enabled

    def __update_config(self):
        """更新配置文件"""
        self.update_config({
            "onlyonce": self._onlyonce,
            "enabled": self._enabled,
            "updatecron": self._update_cron,
            "updatablelist": self._updatable_list,
            "updatablenotify": self._updatable_notify,
            "autoupdatecron": self._auto_update_cron,
            "autoupdatelist": self._auto_update_list,
            "autoupdatenotify": self._auto_update_notify,
            "schedulereport": self._schedule_report,
            "deleteimages": self._delete_images,
            "backupcron": self._backup_cron,
            "backupsnotify": self._backups_notify,
            "host": self._host,
            "secretKey": self._secretKey,
            "intervallimit": self._intervallimit,
            "interval": self._interval,
            "update_success_count": self._update_success_count,
            "update_fail_count": self._update_fail_count,
            "backup_success_count": self._backup_success_count,
            "backup_fail_count": self._backup_fail_count,
            "notify_sent_count": self._notify_sent_count,
            "notify_failed_count": self._notify_failed_count,
            "cleanup_success_count": self._cleanup_success_count,
            "cleanup_fail_count": self._cleanup_fail_count
        })

    def auto_update(self):
        """自动更新"""
        logger.info("DC助手-自动更新-准备执行")
        if not self._auto_update_cron or not self._auto_update_list:
            logger.info("未配置自动更新任务或容器列表为空")
            return
        
        try:
            jwt_token = self.get_jwt()
            if not jwt_token:
                logger.error("获取JWT令牌失败")
                return
            
            containers = self.get_docker_list()
            if not containers:
                logger.warning("获取容器列表失败")
                return
            
            # 清理无标签且不在使用中的镜像
            if self._delete_images:
                images_list = self.get_images_list()
                for image in images_list:
                    if not image.get("inUsed") and image.get("tag"):
                        if self.remove_image(image["id"]):
                            self._cleanup_success_count += 1
                        else:
                            self._cleanup_fail_count += 1
                        self.__update_config()
            
            # 自动更新
            for name in self._auto_update_list:
                for container in containers:
                    if container["name"] == name and container["haveUpdate"]:
                        if not container["usingImage"] or container["usingImage"].startswith("sha256:"):
                            self.post_message(
                                mtype=NotificationType.Plugin,
                                title="🔧 【DC助手-自动更新】",
                                text=f"⚠️ 监测到您有容器TAG不正确\n📦 【{container['name']}】\n🔹 当前镜像:{container['usingImage']}\n🔸 状态:{container['status']} "
                                     f"{container['runningTime']}\n📅 构建时间：{container['createTime']}\n❌ 该镜像无法通过DC自动更新,请修改TAG"
                            )
                            continue
                        
                        url = f'{self._host}/api/container/{container["id"]}/update'
                        usingImage = {container['usingImage']}
                        rescanres = RequestUtils(headers={"Authorization": jwt_token}).post_res(
                            url, {"containerName": name, "imageNameAndTag": usingImage}
                        )
                        data = rescanres.json()
                        
                        if data.get("code") == 200 and data.get("msg") == "success":
                            logger.info(f"{name} 容器更新任务创建成功")
                            
                            if self._auto_update_notify:
                                self.post_message(
                                    mtype=NotificationType.Plugin,
                                    title="✅ 【DC助手-自动更新】",
                                    text=f"📦 【{name}】\n✅ 容器更新任务创建成功"
                                )
                                self._notify_sent_count += 1
                                self.__update_config()
                            
                            if self._schedule_report and data.get("data", {}).get("taskID"):
                                task_id = data["data"]["taskID"]
                                iteration = 0
                                # 修复：确保 intervallimit 有值
                                intervallimit = int(self._intervallimit) if self._intervallimit else 6
                                interval = int(self._interval) if self._interval else 10
                                
                                while iteration < intervallimit:
                                    time.sleep(interval)
                                    
                                    progress_url = f'{self._host}/api/progress/{task_id}'
                                    progress_res = RequestUtils(headers={"Authorization": jwt_token}).get_res(progress_url)
                                    progress_data = progress_res.json()
                                    
                                    if progress_data.get("code") == 200:
                                        progress_msg = progress_data.get("msg", "")
                                        logger.info(f"{name} 进度：{progress_msg}")
                                        
                                        if self._auto_update_notify:
                                            self.post_message(
                                                mtype=NotificationType.Plugin,
                                                title="📊 【DC助手-更新进度】",
                                                text=f"📦 【{name}】\n📈 进度：{progress_msg}"
                                            )
                                            self._notify_sent_count += 1
                                            self.__update_config()
                                        
                                        if progress_msg == "更新成功":
                                            logger.info(f"{name} 更新成功")
                                            self._update_success_count += 1
                                            self.__update_config()
                                            break
                                        elif "失败" in progress_msg or "错误" in progress_msg:
                                            logger.error(f"{name} 更新失败: {progress_msg}")
                                            self._update_fail_count += 1
                                            self.__update_config()
                                            break
                                    
                                    iteration += 1
                                    if iteration >= intervallimit:
                                        logger.info(f'DC助手-更新进度追踪--{name}-超时')
                                        self._update_fail_count += 1
                                        self.__update_config()
                        
        except Exception as e:
            logger.error(f"自动更新执行失败: {str(e)}")
            logger.error(traceback.format_exc())
            self._update_fail_count += 1
            self.__update_config()

    def updatable(self):
        """更新通知"""
        logger.info("DC助手-更新通知-准备执行")
        if not self._update_cron or not self._updatable_list:
            logger.info("未配置更新通知任务或容器列表为空")
            return
        
        try:
            docker_list = self.get_docker_list()
            notify_sent = 0
            notify_failed = 0
            
            for docker in docker_list:
                if docker["haveUpdate"] and docker["name"] in self._updatable_list:
                    try:
                        if docker["usingImage"] and not docker["usingImage"].startswith("sha256:"):
                            self.post_message(
                                mtype=NotificationType.Plugin,
                                title="🔔 【DC助手-更新通知】",
                                text=f"🎉 您有容器可以更新啦！\n📦 【{docker['name']}】\n🔹 当前镜像:{docker['usingImage']}\n🔸 状态:{docker['status']} {docker['runningTime']}\n📅 构建时间：{docker['createTime']}"
                            )
                            logger.info(f"您有容器可以更新啦:{docker['name']}")
                            notify_sent += 1
                        else:
                            self.post_message(
                                mtype=NotificationType.Plugin,
                                title="⚠️ 【DC助手-更新通知】",
                                text=f"⚠️ 监测到您有容器TAG不正确\n📦 【{docker['name']}】\n🔹 当前镜像:{docker['usingImage']}\n🔸 状态:{docker['status']} "
                                     f"{docker['runningTime']}\n📅 构建时间：{docker['createTime']}\n❌ 该镜像无法通过DC自动更新,请修改TAG"
                            )
                            logger.info(f"监测到您有容器TAG不正确 {docker['name']}")
                            notify_sent += 1
                    except Exception as e:
                        logger.error(f"发送通知失败: {str(e)}")
                        notify_failed += 1
            
            # 更新通知统计
            if notify_sent > 0:
                self._notify_sent_count += notify_sent
            if notify_failed > 0:
                self._notify_failed_count += notify_failed
            if notify_sent > 0 or notify_failed > 0:
                self.__update_config()
        
        except Exception as e:
            logger.error(f"更新通知执行失败: {str(e)}")
            logger.error(traceback.format_exc())
            self._notify_failed_count += 1
            self.__update_config()

    def backup(self):
        """备份"""
        try:
            logger.info("DC-备份-准备执行")
            backup_url = f'{self._host}/api/container/backup'
            result = RequestUtils(headers={"Authorization": self.get_jwt()}).get_res(backup_url)
            data = result.json()
            
            if data.get("code") == 200:
                if self._backups_notify:
                    self.post_message(
                        mtype=NotificationType.Plugin,
                        title="✅ 【DC助手-备份成功】",
                        text="💾 镜像备份成功！"
                    )
                    self._notify_sent_count += 1
                    self.__update_config()
                logger.info("DC-备份完成")
                self._backup_success_count += 1
                self.__update_config()
            else:
                if self._backups_notify:
                    self.post_message(
                        mtype=NotificationType.Plugin,
                        title="❌ 【DC助手-备份失败】",
                        text=f"❌ 镜像备份失败拉~！\n⚠️ 【失败原因】:{data.get('msg', '未知错误')}"
                    )
                    self._notify_sent_count += 1
                    self.__update_config()
                logger.error(f"DC-备份失败 Error code: {data.get('code')}, message: {data.get('msg')}")
                self._backup_fail_count += 1
                self.__update_config()
        
        except Exception as e:
            logger.error(f"DC-备份失败,网络异常,请检查DockerCopilot服务是否正常: {str(e)}")
            logger.error(traceback.format_exc())
            self._backup_fail_count += 1
            self.__update_config()

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        """远程同步事件处理"""
        pass

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """获取插件命令"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """获取插件API"""
        return []

    def get_jwt(self) -> str:
        """获取JWT令牌"""
        if not self._secretKey:
            logger.error("未配置secretKey，无法生成JWT")
            return ""
        
        try:
            payload = {
                "exp": int(time.time()) + 28 * 24 * 60 * 60,
                "iat": int(time.time())
            }
            encoded_jwt = jwt.encode(payload, self._secretKey, algorithm="HS256")
            logger.debug(f"生成JWT令牌成功")
            return "Bearer " + encoded_jwt
        except Exception as e:
            logger.error(f"生成JWT令牌失败: {str(e)}")
            return ""

    def get_docker_list(self) -> List[Dict[str, Any]]:
        """获取容器列表"""
        if not self._host or not self._secretKey:
            logger.error("未配置host或secretKey，无法获取容器列表")
            return []
        
        try:
            docker_url = f"{self._host}/api/containers"
            jwt_token = self.get_jwt()
            if not jwt_token:
                return []
            
            result = RequestUtils(headers={"Authorization": jwt_token}).get_res(docker_url)
            if not result:
                return []
            
            data = result.json()
            if data.get("code") == 0:
                return data.get("data", [])
            else:
                logger.error(f"获取容器列表失败: {data.get('msg')}")
                return []
        
        except Exception as e:
            logger.error(f"请求容器列表时发生网络异常: {str(e)}")
            return []

    def get_images_list(self) -> List[Dict[str, Any]]:
        """获取镜像列表"""
        if not self._host or not self._secretKey:
            logger.error("未配置host或secretKey，无法获取镜像列表")
            return []
        
        try:
            images_url = f"{self._host}/api/images"
            jwt_token = self.get_jwt()
            if not jwt_token:
                return []
            
            result = RequestUtils(headers={"Authorization": jwt_token}).get_res(images_url)
            if not result:
                return []
            
            data = result.json()
            if data.get("code") == 200:
                return data.get("data", [])
            else:
                logger.error(f"获取镜像列表失败: {data.get('msg')}")
                return []
        
        except Exception as e:
            logger.error(f"请求镜像列表时发生网络异常: {str(e)}")
            return []

    def remove_image(self, sha) -> bool:
        """清理镜像"""
        if not self._host or not self._secretKey:
            logger.error("未配置host或secretKey，无法清理镜像")
            return False
        
        try:
            images_url = f"{self._host}/api/image/{sha}?force=false"
            jwt_token = self.get_jwt()
            if not jwt_token:
                return False
            
            result = requests.delete(
                images_url,
                headers={"Authorization": jwt_token},
                timeout=30,
                verify=False
            )
            data = result.json()
            
            if data.get("code") == 200:
                logger.info(f"清理镜像成功: {sha}")
                return True
            else:
                logger.error(f"清理镜像失败: {data.get('msg')}")
                return False
        
        except Exception as e:
            logger.error(f"请求清理镜像时发生网络异常: {str(e)}")
            return False

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """获取插件配置页面"""
        updatable_list = []
        auto_update_list = []
        
        # 获取容器列表
        if self._secretKey and self._host:
            try:
                data = self.get_docker_list()
                if data:
                    # 清理不存在的选项
                    names = [item.get('name') for item in data if item.get('name')]
                    if self._updatable_list:
                        self._updatable_list = [item for item in self._updatable_list if item in names]
                    if self._auto_update_list:
                        self._auto_update_list = [item for item in self._auto_update_list if item in names]
                    
                    # 更新配置
                    if self._updatable_list or self._auto_update_list:
                        self.__update_config()
                    
                    # 生成选项列表
                    for item in data:
                        if item.get('name'):
                            updatable_list.append({"title": item["name"], "value": item["name"]})
                            auto_update_list.append({"title": item["name"], "value": item["name"]})
            
            except Exception as e:
                logger.error(f"获取容器列表失败: {str(e)}")
        
        # 确保列表不为None
        self._updatable_list = self._updatable_list or []
        self._auto_update_list = self._auto_update_list or []
        
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
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
                                'props': {'cols': 12, 'md': 6},
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
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'host',
                                            'label': '服务器地址',
                                            'placeholder': 'http://localhost:8080',
                                            'hint': 'DockerCopilot服务地址'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'secretKey',
                                            'label': 'DockerCopilot密钥',
                                            'placeholder': 'DockerCopilot密钥',
                                            'hint': '环境变量查看'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{
                                'component': 'VTabs',
                                'props': {
                                    'model': '_tabs',
                                    'height': 40,
                                },
                                'content': [
                                    {
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
                            }]
                        }]
                    },
                    {
                        'component': 'VWindow',
                        'props': {'model': '_tabs'},
                        'content': [
                            {
                                'component': 'VWindowItem',
                                'props': {'value': 'C1', 'style': {'margin-top': '30px'}},
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'updatecron',
                                                            'label': '更新通知周期',
                                                            'placeholder': '15 8-23/2 * * *',
                                                            'hint': 'Cron表达式'
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
                                                'props': {'cols': 12},
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
                                        ]
                                    }
                                ]
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
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'autoupdatecron',
                                                            'label': '自动更新周期',
                                                            'placeholder': '15 2 * * *',
                                                            'hint': 'Cron表达式'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 3},
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'interval',
                                                            'label': '跟踪间隔(秒)',
                                                            'placeholder': '10',
                                                            'hint': '开启进度汇报时,每多少秒检查一次进度状态，默认10秒'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 3},
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'intervallimit',
                                                            'label': '检查次数',
                                                            'placeholder': '6',
                                                            'hint': '开启进度汇报，当达限制检查次数后放弃追踪,默认6次'
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 4},
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
                                                'props': {'cols': 12, 'md': 4},
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
                                                'props': {'cols': 12, 'md': 4},
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
                                            }
                                        ]
                                    },
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12},
                                                'content': [
                                                    {
                                                        'component': 'VSelect',
                                                        'props': {
                                                            'chips': True,
                                                            'multiple': True,
                                                            'model': 'autoupdatelist',
                                                            'label': '自动更新容器',
                                                            'items': auto_update_list,
                                                            'hint': '被选择的容器当有新版本时自动更新'
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                            {
                                'component': 'VWindowItem',
                                'props': {'value': 'C3', 'style': {'margin-top': '30px'}},
                                'content': [
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'backupcron',
                                                            'label': '自动备份',
                                                            'placeholder': '0 7 * * *',
                                                            'hint': 'Cron表达式'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
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
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "updatablenotify": self._updatable_notify,
            "autoupdatenotify": self._auto_update_notify,
            "schedulereport": self._schedule_report,
            "deleteimages": self._delete_images,
            "backupsnotify": self._backups_notify,
            "interval": self._interval,
            "intervallimit": self._intervallimit,
            "host": self._host or "",
            "secretKey": self._secretKey or "",
            "updatecron": self._update_cron or "",
            "updatablelist": self._updatable_list,
            "autoupdatecron": self._auto_update_cron or "",
            "autoupdatelist": self._auto_update_list,
            "backupcron": self._backup_cron or "",
            "_tabs": "C1"
        }

    def get_page(self) -> List[dict]:
        """
        获取插件详情页面
        这个方法是必需的，用于显示插件的详情页面
        """
        logger.info("加载插件详情页面")
        
        # 获取容器列表
        docker_list = self.get_docker_list()
        updatable_containers = []
        
        if docker_list:
            # 找出有更新的容器
            updatable_containers = [
                container["name"] 
                for container in docker_list 
                if container.get("haveUpdate")
            ]
        
        # 检查定时任务是否设置 - 使用实例变量
        update_notify_set = bool(self._update_cron and self._updatable_list)
        auto_update_set = bool(self._auto_update_cron and self._auto_update_list)
        auto_backup_set = bool(self._backup_cron)
        
        # 获取当前启用的任务数量
        enabled_tasks = 0
        if self._enabled:
            if update_notify_set:
                enabled_tasks += 1
            if auto_update_set:
                enabled_tasks += 1
            if auto_backup_set:
                enabled_tasks += 1
        
        # 简化版详情页面 - 垂直排列，填满默认页面
        return [
            {
                'component': 'VCard',
                'content': [
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-4'
                        },
                        'content': [
                            # 第一行：运行状态概览
                            {
                                'component': 'VRow',
                                'props': {
                                    'class': 'mb-3'
                                },
                                'content': [
                                    # 运行状态卡片
                                    {
                                        'component': 'VCol',
                                        'props': {
                                            'cols': 12,
                                            'md': 4
                                        },
                                        'content': [
                                            {
                                                'component': 'VCard',
                                                'props': {
                                                    'variant': 'outlined',
                                                    'class': 'h-100'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VCardTitle',
                                                        'props': {
                                                            'class': 'pa-2'
                                                        },
                                                        'text': '运行状态'
                                                    },
                                                    {
                                                        'component': 'VDivider'
                                                    },
                                                    {
                                                        'component': 'VCardText',
                                                        'props': {
                                                            'class': 'pa-2 text-center'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'div',
                                                                'props': {
                                                                    'class': 'd-flex flex-column align-center'
                                                                },
                                                                'content': [
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-h4 mb-1'
                                                                        },
                                                                        'text': '✅' if self._enabled else '❌'
                                                                    },
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-h6'
                                                                        },
                                                                        'text': '已启用' if self._enabled else '未启用'
                                                                    },
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-caption text-medium-emphasis mt-1'
                                                                        },
                                                                        'text': f'{enabled_tasks} 个任务' if self._enabled else ''
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    # 服务器地址卡片
                                    {
                                        'component': 'VCol',
                                        'props': {
                                            'cols': 12,
                                            'md': 4
                                        },
                                        'content': [
                                            {
                                                'component': 'VCard',
                                                'props': {
                                                    'variant': 'outlined',
                                                    'class': 'h-100'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VCardTitle',
                                                        'props': {
                                                            'class': 'pa-2'
                                                        },
                                                        'text': '服务器状态'
                                                    },
                                                    {
                                                        'component': 'VDivider'
                                                    },
                                                    {
                                                        'component': 'VCardText',
                                                        'props': {
                                                            'class': 'pa-2 text-center'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'div',
                                                                'props': {
                                                                    'class': 'd-flex flex-column align-center'
                                                                },
                                                                'content': [
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-h4 mb-1'
                                                                        },
                                                                        'text': '🌐'
                                                                    },
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-h6 text-truncate',
                                                                            'style': 'max-width: 100%'
                                                                        },
                                                                        'text': self._host if self._host else '未设置'
                                                                    },
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-caption text-medium-emphasis mt-1'
                                                                        },
                                                                        'text': f'{len(docker_list)} 个容器' if docker_list else '未连接'
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    # 可更新容器卡片
                                    {
                                        'component': 'VCol',
                                        'props': {
                                            'cols': 12,
                                            'md': 4
                                        },
                                        'content': [
                                            {
                                                'component': 'VCard',
                                                'props': {
                                                    'variant': 'outlined',
                                                    'class': 'h-100'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VCardTitle',
                                                        'props': {
                                                            'class': 'pa-2'
                                                        },
                                                        'text': '更新状态'
                                                    },
                                                    {
                                                        'component': 'VDivider'
                                                    },
                                                    {
                                                        'component': 'VCardText',
                                                        'props': {
                                                            'class': 'pa-2 text-center'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'div',
                                                                'props': {
                                                                    'class': 'd-flex flex-column align-center'
                                                                },
                                                                'content': [
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-h4 mb-1'
                                                                        },
                                                                        'text': '🔄' if updatable_containers else '📦'
                                                                    },
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-h6'
                                                                        },
                                                                        'text': f'{len(updatable_containers)} 个可更新'
                                                                    },
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-caption text-medium-emphasis mt-1'
                                                                        },
                                                                        'text': ', '.join(updatable_containers[:3]) + ('...' if len(updatable_containers) > 3 else '') if updatable_containers else '暂无更新'
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                            
                            # 第二行：定时任务状态
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'outlined',
                                    'class': 'mb-3'
                                },
                                'content': [
                                    {
                                        'component': 'VCardTitle',
                                        'props': {
                                            'class': 'pa-3'
                                        },
                                        'text': '定时任务配置'
                                    },
                                    {
                                        'component': 'VDivider'
                                    },
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'pa-3'
                                        },
                                        'content': [
                                            {
                                                'component': 'VRow',
                                                'content': [
                                                    # 更新通知定时任务
                                                    {
                                                        'component': 'VCol',
                                                        'props': {
                                                            'cols': 12,
                                                            'md': 4
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'VCard',
                                                                'props': {
                                                                    'variant': 'tonal',
                                                                    'color': 'info' if update_notify_set else 'grey',
                                                                    'class': 'text-center h-100'
                                                                },
                                                                'content': [
                                                                    {
                                                                        'component': 'VCardText',
                                                                        'props': {
                                                                            'class': 'pa-3'
                                                                        },
                                                                        'content': [
                                                                            {
                                                                                'component': 'div',
                                                                                'props': {
                                                                                    'class': 'text-h6 mb-2'
                                                                                },
                                                                                'text': '更新通知'
                                                                            },
                                                                            {
                                                                                'component': 'div',
                                                                                'props': {
                                                                                    'class': 'text-h5 mb-1'
                                                                                },
                                                                                'text': '✅' if update_notify_set else '❌'
                                                                            },
                                                                            {
                                                                                'component': 'div',
                                                                                'props': {
                                                                                    'class': 'text-caption text-medium-emphasis'
                                                                                },
                                                                                'text': self._update_cron if self._update_cron else '未配置'
                                                                            }
                                                                        ]
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                    # 自动更新定时任务
                                                    {
                                                        'component': 'VCol',
                                                        'props': {
                                                            'cols': 12,
                                                            'md': 4
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'VCard',
                                                                'props': {
                                                                    'variant': 'tonal',
                                                                    'color': 'warning' if auto_update_set else 'grey',
                                                                    'class': 'text-center h-100'
                                                                },
                                                                'content': [
                                                                    {
                                                                        'component': 'VCardText',
                                                                        'props': {
                                                                            'class': 'pa-3'
                                                                        },
                                                                        'content': [
                                                                            {
                                                                                'component': 'div',
                                                                                'props': {
                                                                                    'class': 'text-h6 mb-2'
                                                                                },
                                                                                'text': '自动更新'
                                                                            },
                                                                            {
                                                                                'component': 'div',
                                                                                'props': {
                                                                                    'class': 'text-h5 mb-1'
                                                                                },
                                                                                'text': '✅' if auto_update_set else '❌'
                                                                            },
                                                                            {
                                                                                'component': 'div',
                                                                                'props': {
                                                                                    'class': 'text-caption text-medium-emphasis'
                                                                                },
                                                                                'text': self._auto_update_cron if self._auto_update_cron else '未配置'
                                                                            }
                                                                        ]
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                    # 自动备份定时任务
                                                    {
                                                        'component': 'VCol',
                                                        'props': {
                                                            'cols': 12,
                                                            'md': 4
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'VCard',
                                                                'props': {
                                                                    'variant': 'tonal',
                                                                    'color': 'success' if auto_backup_set else 'grey',
                                                                    'class': 'text-center h-100'
                                                                },
                                                                'content': [
                                                                    {
                                                                        'component': 'VCardText',
                                                                        'props': {
                                                                            'class': 'pa-3'
                                                                        },
                                                                        'content': [
                                                                            {
                                                                                'component': 'div',
                                                                                'props': {
                                                                                    'class': 'text-h6 mb-2'
                                                                                },
                                                                                'text': '自动备份'
                                                                            },
                                                                            {
                                                                                'component': 'div',
                                                                                'props': {
                                                                                    'class': 'text-h5 mb-1'
                                                                                },
                                                                        'text': '✅' if auto_backup_set else '❌'
                                                                            },
                                                                            {
                                                                                'component': 'div',
                                                                                'props': {
                                                                                    'class': 'text-caption text-medium-emphasis'
                                                                                },
                                                                                'text': self._backup_cron if self._backup_cron else '未配置'
                                                                            }
                                                                        ]
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                            
                            # 第三行：容器配置
                            {
                                'component': 'VRow',
                                'props': {
                                    'class': 'mb-4'
                                },
                                'content': [
                                    # 更新通知容器卡片
                                    {
                                        'component': 'VCol',
                                        'props': {
                                            'cols': 12,
                                            'md': 6
                                        },
                                        'content': [
                                            {
                                                'component': 'VCard',
                                                'props': {
                                                    'variant': 'outlined',
                                                    'class': 'h-100'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VCardTitle',
                                                        'props': {
                                                            'class': 'pa-3'
                                                        },
                                                        'text': '更新通知容器'
                                                    },
                                                    {
                                                        'component': 'VDivider'
                                                    },
                                                    {
                                                        'component': 'VCardText',
                                                        'props': {
                                                            'class': 'pa-3'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'div',
                                                                'props': {
                                                                    'class': 'd-flex align-center mb-2'
                                                                },
                                                                'content': [
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-h6'
                                                                        },
                                                                        'text': f'🔔 {len(self._updatable_list)} 个容器'
                                                                    }
                                                                ]
                                                            },
                                                            {
                                                                'component': 'div',
                                                                'props': {
                                                                    'class': 'text-body-2 text-medium-emphasis'
                                                                },
                                                                'text': ', '.join(self._updatable_list) if self._updatable_list else '未选择任何容器'
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    # 自动更新容器卡片
                                    {
                                        'component': 'VCol',
                                        'props': {
                                            'cols': 12,
                                            'md': 6
                                        },
                                        'content': [
                                            {
                                                'component': 'VCard',
                                                'props': {
                                                    'variant': 'outlined',
                                                    'class': 'h-100'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VCardTitle',
                                                        'props': {
                                                            'class': 'pa-3'
                                                        },
                                                        'text': '自动更新容器'
                                                    },
                                                    {
                                                        'component': 'VDivider'
                                                    },
                                                    {
                                                        'component': 'VCardText',
                                                        'props': {
                                                            'class': 'pa-3'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'div',
                                                                'props': {
                                                                    'class': 'd-flex align-center mb-2'
                                                                },
                                                                'content': [
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-h6'
                                                                        },
                                                                        'text': f'🔄 {len(self._auto_update_list)} 个容器'
                                                                    }
                                                                ]
                                                            },
                                                            {
                                                                'component': 'div',
                                                                'props': {
                                                                    'class': 'text-body-2 text-medium-emphasis'
                                                                },
                                                                'text': ', '.join(self._auto_update_list) if self._auto_update_list else '未选择任何容器'
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                            
                            # 第四行：详细记录统计
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'outlined'
                                },
                                'content': [
                                    {
                                        'component': 'VCardTitle',
                                        'props': {
                                            'class': 'pa-3'
                                        },
                                        'text': '操作统计'
                                    },
                                    {
                                        'component': 'VDivider'
                                    },
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'pa-3'
                                        },
                                        'content': [
                                            {
                                                'component': 'VRow',
                                                'content': [
                                                    # 更新成功
                                                    {
                                                        'component': 'VCol',
                                                        'props': {
                                                            'cols': 6,
                                                            'sm': 3
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'VCard',
                                                                'props': {
                                                                    'variant': 'tonal',
                                                                    'color': 'success',
                                                                    'class': 'text-center pa-2'
                                                                },
                                                                'content': [
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-h5'
                                                                        },
                                                                        'text': f'{self._update_success_count}'
                                                                    },
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-caption'
                                                                        },
                                                                        'text': '更新成功'
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                    # 更新失败
                                                    {
                                                        'component': 'VCol',
                                                        'props': {
                                                            'cols': 6,
                                                            'sm': 3
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'VCard',
                                                                'props': {
                                                                    'variant': 'tonal',
                                                                    'color': 'error',
                                                                    'class': 'text-center pa-2'
                                                                },
                                                                'content': [
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-h5'
                                                                        },
                                                                        'text': f'{self._update_fail_count}'
                                                                    },
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-caption'
                                                                        },
                                                                        'text': '更新失败'
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                    # 备份成功
                                                    {
                                                        'component': 'VCol',
                                                        'props': {
                                                            'cols': 6,
                                                            'sm': 3
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'VCard',
                                                                'props': {
                                                                    'variant': 'tonal',
                                                                    'color': 'success',
                                                                    'class': 'text-center pa-2'
                                                                },
                                                                'content': [
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-h5'
                                                                        },
                                                                        'text': f'{self._backup_success_count}'
                                                                    },
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-caption'
                                                                        },
                                                                        'text': '备份成功'
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                    # 清理成功
                                                    {
                                                        'component': 'VCol',
                                                        'props': {
                                                            'cols': 6,
                                                            'sm': 3
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'VCard',
                                                                'props': {
                                                                    'variant': 'tonal',
                                                                    'color': 'success',
                                                                    'class': 'text-center pa-2'
                                                                },
                                                                'content': [
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-h5'
                                                                        },
                                                                        'text': f'{self._cleanup_success_count}'
                                                                    },
                                                                    {
                                                                        'component': 'div',
                                                                        'props': {
                                                                            'class': 'text-caption'
                                                                        },
                                                                        'text': '清理成功'
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
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
                logger.info("DC助手定时服务已停止")
        except Exception as e:
            logger.error(f"停止插件服务失败: {str(e)}")
            logger.error(traceback.format_exc())