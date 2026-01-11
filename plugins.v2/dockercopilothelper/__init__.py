"""
DockerCopilotHelper 插件 - V2 版本
功能：配合 DockerCopilot 完成容器更新通知、自动更新、自动备份等功能
版本：2.0.2
作者：gxterry
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
    """
    DockerCopilot 辅助插件类
    主要功能：
    1. 容器更新通知：定期检查指定容器是否有更新，并发送通知
    2. 自动更新：自动更新指定的容器
    3. 自动备份：定期备份 Docker 配置
    4. 镜像清理：清理无用的 Docker 镜像
    """
    
    # 插件基本信息
    plugin_name = "DC助手AI版"
    plugin_desc = "配合DockerCopilot,完成更新通知、自动更改、自动备份功能"
    plugin_icon = "https://raw.githubusercontent.com/siryle/MoviePilot-Plugins/main/icons/Docker_Copilot.png"
    plugin_version = "2.0.2"
    plugin_author = "gxterry"
    author_url = "https://github.com/gxterry"
    plugin_config_prefix = "dockercopilothelper_"
    plugin_order = 15
    auth_level = 1

    # 插件配置参数（私有属性）
    _enabled = False            # 插件是否启用
    _onlyonce = False           # 是否立即运行一次
    _update_cron = None         # 更新通知的 cron 表达式
    _updatable_list = []        # 需要检查更新的容器列表
    _updatable_notify = False   # 是否发送更新通知
    _schedule_report = False    # 是否启用进度汇报
    _auto_update_cron = None    # 自动更新的 cron 表达式
    _auto_update_list = []      # 需要自动更新的容器列表
    _auto_update_notify = False # 是否发送自动更新通知
    _delete_images = False      # 是否清理无用镜像
    _intervallimit = 6          # 进度检查次数限制
    _interval = 10              # 进度检查间隔（秒）
    _backup_cron = None         # 自动备份的 cron 表达式
    _backups_notify = False     # 是否发送备份通知
    _host = None                # DockerCopilot 服务器地址
    _secretKey = None           # DockerCopilot 密钥
    _scheduler = None           # 任务调度器
    
    # 操作统计信息
    _update_success_count = 0   # 更新成功次数
    _update_fail_count = 0      # 更新失败次数
    _backup_success_count = 0   # 备份成功次数
    _backup_fail_count = 0      # 备份失败次数
    _notify_sent_count = 0      # 通知发送成功次数
    _notify_failed_count = 0    # 通知发送失败次数
    _cleanup_success_count = 0  # 镜像清理成功次数
    _cleanup_fail_count = 0     # 镜像清理失败次数
    
    # 日志前缀
    _log_prefix = "[DC助手]"

    def __init__(self):
        """初始化插件"""
        super().__init__()
        logger.info(f"{self._log_prefix} 插件初始化完成 - 版本: {self.plugin_version}")

    def init_plugin(self, config: dict = None):
        """
        初始化插件配置
        
        Args:
            config: 插件配置字典
        """
        logger.info(f"{self._log_prefix} 开始初始化插件配置")
        
        # 停止现有服务
        self.stop_service()
        
        try:
            if config:
                # 加载配置参数
                self._load_configuration(config)
                
                logger.info(f"{self._log_prefix} 配置加载完成: 启用={self._enabled}, 服务器={self._host}")
                
                # 检查必要配置
                if not self._secretKey or not self._host:
                    logger.error(f"{self._log_prefix} 服务配置不完整: secretKey或host未填写")
                    self._enabled = False
                    self.__update_config()
                    return

                # 初始化任务调度器
                if self._enabled or self._onlyonce:
                    self._initialize_scheduler()
            else:
                logger.warning(f"{self._log_prefix} 插件配置为空，使用默认配置")
                
        except Exception as e:
            logger.error(f"{self._log_prefix} 插件初始化异常: {str(e)}")
            logger.debug(f"{self._log_prefix} 异常详情: {traceback.format_exc()}")
        
        logger.info(f"{self._log_prefix} 插件初始化完成")

    def get_state(self) -> bool:
        """
        获取插件状态
        
        Returns:
            bool: 插件是否启用
        """
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
        """
        自动更新容器
        
        功能：
        1. 清理无用的 Docker 镜像（如果启用）
        2. 检查指定容器是否有更新
        3. 自动更新有更新的容器
        4. 跟踪更新进度并发送通知（如果启用）
        """
        logger.info(f"{self._log_prefix} 开始执行自动更新任务")
        
        # 检查配置
        if not self._auto_update_cron:
            logger.info(f"{self._log_prefix} 自动更新任务未配置，跳过执行")
            return
            
        if not self._auto_update_list:
            logger.warning(f"{self._log_prefix} 自动更新容器列表为空，跳过执行")
            return
        
        try:
            # 获取 JWT 令牌
            jwt_token = self.get_jwt()
            if not jwt_token:
                logger.error(f"{self._log_prefix} 获取JWT令牌失败，无法执行自动更新")
                return
            
            # 获取容器列表
            containers = self.get_docker_list()
            if not containers:
                logger.warning(f"{self._log_prefix} 获取容器列表失败，无法执行自动更新")
                return
            
            # 清理无用镜像
            self._cleanup_unused_images()
            
            # 执行自动更新
            self._execute_auto_updates(containers, jwt_token)
                        
        except Exception as e:
            logger.error(f"{self._log_prefix} 自动更新执行失败: {str(e)}")
            logger.debug(f"{self._log_prefix} 异常详情: {traceback.format_exc()}")
            self._update_fail_count += 1
            self.__update_config()

    def updatable(self):
        """
        更新通知
        
        功能：
        1. 检查指定容器是否有更新
        2. 发送更新通知给用户
        3. 对于使用 SHA256 格式镜像的容器，发送特殊提醒
        """
        logger.info(f"{self._log_prefix} 开始执行更新通知任务")
        
        # 检查配置
        if not self._update_cron:
            logger.info(f"{self._log_prefix} 更新通知任务未配置，跳过执行")
            return
            
        if not self._updatable_list:
            logger.warning(f"{self._log_prefix} 更新通知容器列表为空，跳过执行")
            return
        
        try:
            # 获取容器列表
            docker_list = self.get_docker_list()
            if not docker_list:
                logger.warning(f"{self._log_prefix} 获取容器列表失败，无法发送更新通知")
                return
            
            # 发送更新通知
            notify_sent, notify_failed = self._send_update_notifications(docker_list)
            
            # 更新统计信息
            if notify_sent > 0:
                self._notify_sent_count += notify_sent
                logger.info(f"{self._log_prefix} 更新通知发送完成，共发送 {notify_sent} 条通知")
            if notify_failed > 0:
                self._notify_failed_count += notify_failed
                logger.warning(f"{self._log_prefix} 更新通知发送失败 {notify_failed} 条")
                
            if notify_sent > 0 or notify_failed > 0:
                self.__update_config()
            else:
                logger.info(f"{self._log_prefix} 未发现需要发送通知的容器")
        
        except Exception as e:
            logger.error(f"{self._log_prefix} 更新通知执行失败: {str(e)}")
            logger.debug(f"{self._log_prefix} 异常详情: {traceback.format_exc()}")
            self._notify_failed_count += 1
            self.__update_config()

    def backup(self):
        """
        备份 Docker 配置
        
        功能：
        1. 调用 DockerCopilot API 备份所有Docker 配置
        2. 发送备份成功/失败通知（如果启用）
        3. 更新备份统计信息
        """
        logger.info(f"{self._log_prefix} 开始执行备份任务")
        
        try:
            # 获取 JWT 令牌
            jwt_token = self.get_jwt()
            if not jwt_token:
                logger.error(f"{self._log_prefix} 获取JWT令牌失败，无法执行备份")
                self._backup_fail_count += 1
                self.__update_config()
                return
            
            # 调用备份 API
            backup_url = f'{self._host}/api/container/backup'
            logger.debug(f"{self._log_prefix} 发送备份请求")
            
            result = RequestUtils(headers={"Authorization": jwt_token}).get_res(backup_url)
            if not result:
                logger.error(f"{self._log_prefix} 备份请求无响应")
                self._backup_fail_count += 1
                self.__update_config()
                return
                
            # 处理备份结果
            data = result.json()
            self._handle_backup_result(data)
            
            self.__update_config()
        
        except Exception as e:
            logger.error(f"{self._log_prefix} 备份执行失败: {str(e)}")
            logger.debug(f"{self._log_prefix} 异常详情: {traceback.format_exc()}")
            self._backup_fail_count += 1
            self.__update_config()

    def get_jwt(self) -> str:
        """
        生成 JWT 令牌
        
        Returns:
            str: JWT 令牌字符串，格式为 "Bearer {token}"
            如果生成失败，返回空字符串
        """
        if not self._secretKey:
            logger.error(f"{self._log_prefix} 未配置secretKey，无法生成JWT")
            return ""
        
        try:
            # 构造 JWT payload
            payload = {
                "exp": int(time.time()) + 28 * 24 * 60 * 60,  # 28天过期
                "iat": int(time.time())                       # 签发时间
            }
            
            # 生成 JWT
            encoded_jwt = jwt.encode(payload, self._secretKey, algorithm="HS256")
            logger.debug(f"{self._log_prefix} JWT令牌生成成功")
            
            return "Bearer " + encoded_jwt
        except Exception as e:
            logger.error(f"{self._log_prefix} JWT令牌生成失败: {str(e)}")
            return ""

    def get_docker_list(self) -> List[Dict[str, Any]]:
        """
        获取 Docker 容器列表
        
        Returns:
            List[Dict[str, Any]]: 容器列表，每个容器是一个字典
            如果获取失败，返回空列表
        """
        if not self._host or not self._secretKey:
            logger.error(f"{self._log_prefix} 未配置host或secretKey，无法获取容器列表")
            return []
        
        try:
            # 构造 API URL
            docker_url = f"{self._host}/api/containers"
            jwt_token = self.get_jwt()
            
            if not jwt_token:
                return []
            
            # 发送请求
            logger.debug(f"{self._log_prefix} 获取容器列表: {docker_url}")
            result = RequestUtils(headers={"Authorization": jwt_token}).get_res(docker_url)
            
            if not result:
                logger.warning(f"{self._log_prefix} 获取容器列表无响应")
                return []
            
            # 解析响应
            data = result.json()
            if data.get("code") == 0:
                containers = data.get("data", [])
                logger.info(f"{self._log_prefix} 获取到 {len(containers)} 个容器")
                return containers
            else:
                logger.error(f"{self._log_prefix} 获取容器列表失败: {data.get('msg')}")
                return []
        
        except Exception as e:
            logger.error(f"{self._log_prefix} 获取容器列表异常: {str(e)}")
            return []

    def get_images_list(self) -> List[Dict[str, Any]]:
        """
        获取 Docker 镜像列表
        
        Returns:
            List[Dict[str, Any]]: 镜像列表，每个镜像是一个字典
            如果获取失败，返回空列表
        """
        if not self._host or not self._secretKey:
            logger.error(f"{self._log_prefix} 未配置host或secretKey，无法获取镜像列表")
            return []
        
        try:
            # 构造 API URL
            images_url = f"{self._host}/api/images"
            jwt_token = self.get_jwt()
            
            if not jwt_token:
                return []
            
            # 发送请求
            logger.debug(f"{self._log_prefix} 获取镜像列表: {images_url}")
            result = RequestUtils(headers={"Authorization": jwt_token}).get_res(images_url)
            
            if not result:
                logger.warning(f"{self._log_prefix} 获取镜像列表无响应")
                return []
            
            # 解析响应
            data = result.json()
            if data.get("code") == 200:
                images = data.get("data", [])
                logger.info(f"{self._log_prefix} 获取到 {len(images)} 个镜像")
                return images
            else:
                logger.error(f"{self._log_prefix} 获取镜像列表失败: {data.get('msg')}")
                return []
        
        except Exception as e:
            logger.error(f"{self._log_prefix} 获取镜像列表异常: {str(e)}")
            return []

    def remove_image(self, sha: str) -> bool:
        """
        删除指定的 Docker 镜像
        
        Args:
            sha: 镜像的 SHA256 标识
            
        Returns:
            bool: 删除是否成功
        """
        if not self._host or not self._secretKey:
            logger.error(f"{self._log_prefix} 未配置host或secretKey，无法清理镜像")
            return False
        
        try:
            # 构造 API URL
            images_url = f"{self._host}/api/image/{sha}?force=false"
            jwt_token = self.get_jwt()
            
            if not jwt_token:
                return False
            
            # 发送删除请求
            logger.debug(f"{self._log_prefix} 清理镜像: {sha}")
            result = requests.delete(
                images_url,
                headers={"Authorization": jwt_token},
                timeout=30,
                verify=False
            )
            
            # 解析响应
            data = result.json()
            if data.get("code") == 200:
                logger.info(f"{self._log_prefix} 镜像清理成功: {sha}")
                return True
            else:
                logger.error(f"{self._log_prefix} 镜像清理失败: {data.get('msg')}")
                return False
        
        except Exception as e:
            logger.error(f"{self._log_prefix} 镜像清理异常: {str(e)}")
            return False

    def stop_service(self):
        """停止插件服务"""
        try:
            if self._scheduler:
                if self._scheduler.running:
                    jobs_count = len(self._scheduler.get_jobs())
                    self._scheduler.shutdown()
                    logger.info(f"{self._log_prefix} 停止定时服务，共停止 {jobs_count} 个任务")
                self._scheduler = None
        except Exception as e:
            logger.error(f"{self._log_prefix} 停止插件服务失败: {str(e)}")
            logger.debug(f"{self._log_prefix} 异常详情: {traceback.format_exc()}")

    # ==================== 辅助方法 ====================

    def _load_configuration(self, config: dict):
        """
        加载插件配置
        
        Args:
            config: 配置字典
        """
        self._enabled = config.get("enabled", False)
        self._onlyonce = config.get("onlyonce", False)
        self._update_cron = config.get("updatecron")
        self._updatable_list = config.get("updatablelist", [])
        self._updatable_notify = config.get("updatablenotify", False)
        self._schedule_report = config.get("schedulereport", False)
        self._auto_update_cron = config.get("autoupdatecron")
        self._auto_update_list = config.get("autoupdatelist", [])
        self._auto_update_notify = config.get("autoupdatenotify", False)
        self._delete_images = config.get("deleteimages", False)
        self._backup_cron = config.get("backupcron")
        self._backups_notify = config.get("backupsnotify", False)
        
        # 修复：为 None 值提供默认值
        self._intervallimit = config.get("intervallimit", 6) or 6
        self._interval = config.get("interval", 10) or 10
        self._host = config.get("host", "")
        self._secretKey = config.get("secretKey", "")
        
        # 加载统计信息
        self._update_success_count = config.get("update_success_count", 0)
        self._update_fail_count = config.get("update_fail_count", 0)
        self._backup_success_count = config.get("backup_success_count", 0)
        self._backup_fail_count = config.get("backup_fail_count", 0)
        self._notify_sent_count = config.get("notify_sent_count", 0)
        self._notify_failed_count = config.get("notify_failed_count", 0)
        self._cleanup_success_count = config.get("cleanup_success_count", 0)
        self._cleanup_fail_count = config.get("cleanup_fail_count", 0)

    def _initialize_scheduler(self):
        """
        初始化任务调度器
        """
        # 创建调度器
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        jobs_count = 0
        
        # 添加一次性任务（如果启用）
        if self._onlyonce:
            logger.info(f"{self._log_prefix} 启动一次性任务执行")
            jobs_count = self._add_one_time_tasks()
            
            # 关闭一次性开关并保存配置
            self._onlyonce = False
            self.__update_config()
            logger.info(f"{self._log_prefix} 已添加 {jobs_count} 个一次性任务")
        
        # 添加周期性任务
        jobs_count = self._add_periodic_tasks()
        
        # 启动调度器
        if self._scheduler.get_jobs():
            self._scheduler.start()
            logger.info(f"{self._log_prefix} 定时服务已启动，共 {len(self._scheduler.get_jobs())} 个任务")
        else:
            logger.warning(f"{self._log_prefix} 未配置任何定时任务")

    def _add_one_time_tasks(self) -> int:
        """
        添加一次性任务
        
        Returns:
            int: 添加的任务数量
        """
        jobs_count = 0
        
        if self._backup_cron:
            self._scheduler.add_job(
                self.backup, 
                'date',
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="DC助手-备份"
            )
            jobs_count += 1
            
        if self._update_cron:
            self._scheduler.add_job(
                self.updatable,
                'date',
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=6),
                name="DC助手-更新通知"
            )
            jobs_count += 1
            
        if self._auto_update_cron:
            self._scheduler.add_job(
                self.auto_update,
                'date',
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=10),
                name="DC助手-自动更新"
            )
            jobs_count += 1
            
        return jobs_count

    def _add_periodic_tasks(self) -> int:
        """
        添加周期性任务
        
        Returns:
            int: 添加的任务数量
        """
        jobs_count = 0
        
        # 添加备份任务
        if self._backup_cron:
            try:
                self._scheduler.add_job(
                    func=self.backup,
                    trigger=CronTrigger.from_crontab(self._backup_cron),
                    name="DC助手-备份"
                )
                jobs_count += 1
                logger.debug(f"{self._log_prefix} 添加备份任务: {self._backup_cron}")
            except Exception as err:
                logger.error(f"{self._log_prefix} 备份任务配置错误: {str(err)}")
        
        # 添加更新通知任务
        if self._update_cron:
            try:
                self._scheduler.add_job(
                    func=self.updatable,
                    trigger=CronTrigger.from_crontab(self._update_cron),
                    name="DC助手-更新通知"
                )
                jobs_count += 1
                logger.debug(f"{self._log_prefix} 添加更新通知任务: {self._update_cron}")
            except Exception as err:
                logger.error(f"{self._log_prefix} 更新通知任务配置错误: {str(err)}")
        
        # 添加自动更新任务
        if self._auto_update_cron:
            try:
                self._scheduler.add_job(
                    func=self.auto_update,
                    trigger=CronTrigger.from_crontab(self._auto_update_cron),
                    name="DC助手-自动更新"
                )
                jobs_count += 1
                logger.debug(f"{self._log_prefix} 添加自动更新任务: {self._auto_update_cron}")
            except Exception as err:
                logger.error(f"{self._log_prefix} 自动更新任务配置错误: {str(err)}")
        
        return jobs_count

    def _cleanup_unused_images(self):
        """
        清理无用的 Docker 镜像
        """
        if self._delete_images:
            logger.info(f"{self._log_prefix} 开始清理无用镜像")
            images_list = self.get_images_list()
            cleanup_count = 0
            
            for image in images_list:
                # 检查镜像是否在使用中且有标签
                if not image.get("inUsed") and image.get("tag"):
                    if self.remove_image(image["id"]):
                        self._cleanup_success_count += 1
                        cleanup_count += 1
                    else:
                        self._cleanup_fail_count += 1
            
            if cleanup_count > 0:
                logger.info(f"{self._log_prefix} 清理完成，共清理 {cleanup_count} 个镜像")
            
            self.__update_config()

    def _execute_auto_updates(self, containers: List[Dict], jwt_token: str):
        """
        执行自动更新
        
        Args:
            containers: 容器列表
            jwt_token: JWT 令牌
        """
        update_count = 0
        
        for name in self._auto_update_list:
            logger.debug(f"{self._log_prefix} 检查容器更新状态: {name}")
            
            for container in containers:
                if container["name"] == name and container["haveUpdate"]:
                    logger.info(f"{self._log_prefix} 发现容器 {name} 有可用更新")
                    
                    # 检查镜像格式（SHA256格式无法自动更新）
                    if not container["usingImage"] or container["usingImage"].startswith("sha256:"):
                        logger.warning(f"{self._log_prefix} 容器 {name} 使用SHA256格式镜像，无法自动更新")
                        if self._auto_update_notify:
                            self._send_notification(
                                title="🔧 【DC助手-自动更新】",
                                text=f"⚠️ 监测到您有容器TAG不正确\n📦 【{container['name']}】\n🔹 当前镜像:{container['usingImage']}\n🔸 状态:{container['status']} "
                                     f"{container['runningTime']}\n📅 构建时间：{container['createTime']}\n❌ 该镜像无法通过DC自动更新,请修改TAG"
                            )
                        continue
                    
                    # 提交更新请求
                    url = f'{self._host}/api/container/{container["id"]}/update'
                    usingImage = {container['usingImage']}
                    
                    logger.debug(f"{self._log_prefix} 提交更新请求: {name}")
                    rescanres = RequestUtils(headers={"Authorization": jwt_token}).post_res(
                        url, {"containerName": name, "imageNameAndTag": usingImage}
                    )
                    data = rescanres.json()
                    
                    # 处理更新响应
                    if data.get("code") == 200 and data.get("msg") == "success":
                        logger.info(f"{self._log_prefix} 容器 {name} 更新任务创建成功")
                        update_count += 1
                        
                        if self._auto_update_notify:
                            self._send_notification(
                                title="✅ 【DC助手-自动更新】",
                                text=f"📦 【{name}】\n✅ 容器更新任务创建成功"
                            )
                        
                        # 跟踪更新进度
                        if self._schedule_report and data.get("data", {}).get("taskID"):
                            task_id = data["data"]["taskID"]
                            self._track_update_progress(name, task_id, jwt_token)
        
        # 记录更新结果
        if update_count > 0:
            logger.info(f"{self._log_prefix} 自动更新完成，共处理 {update_count} 个容器")
        else:
            logger.info(f"{self._log_prefix} 未发现需要更新的容器")

    def _track_update_progress(self, container_name: str, task_id: str, jwt_token: str):
        """
        跟踪容器更新进度
        
        Args:
            container_name: 容器名称
            task_id: 任务ID
            jwt_token: JWT 令牌
        """
        logger.info(f"{self._log_prefix} 开始跟踪容器 {container_name} 更新进度")
        
        iteration = 0
        intervallimit = int(self._intervallimit) if self._intervallimit else 6
        interval = int(self._interval) if self._interval else 10
        
        while iteration < intervallimit:
            time.sleep(interval)
            iteration += 1
            
            try:
                # 查询进度
                progress_url = f'{self._host}/api/progress/{task_id}'
                progress_res = RequestUtils(headers={"Authorization": jwt_token}).get_res(progress_url)
                progress_data = progress_res.json()
                
                if progress_data.get("code") == 200:
                    progress_msg = progress_data.get("msg", "")
                    logger.info(f"{self._log_prefix} 容器 {container_name} 更新进度: {progress_msg}")
                    
                    # 发送进度通知
                    if self._auto_update_notify:
                        self._send_notification(
                            title="📊 【DC助手-更新进度】",
                            text=f"📦 【{container_name}】\n📈 进度：{progress_msg}"
                        )
                    
                    # 判断更新结果
                    if progress_msg == "更新成功":
                        logger.info(f"{self._log_prefix} 容器 {container_name} 更新成功")
                        self._update_success_count += 1
                        break
                    elif "失败" in progress_msg or "错误" in progress_msg:
                        logger.error(f"{self._log_prefix} 容器 {container_name} 更新失败: {progress_msg}")
                        self._update_fail_count += 1
                        break
                else:
                    logger.warning(f"{self._log_prefix} 获取进度失败: {progress_data.get('msg')}")
                    
            except Exception as e:
                logger.error(f"{self._log_prefix} 跟踪进度时发生异常: {str(e)}")
        
        # 检查是否超时
        if iteration >= intervallimit:
            logger.warning(f"{self._log_prefix} 容器 {container_name} 进度跟踪超时")
            self._update_fail_count += 1
        
        self.__update_config()

    def _send_update_notifications(self, docker_list: List[Dict]) -> Tuple[int, int]:
        """
        发送更新通知
        
        Args:
            docker_list: 容器列表
            
        Returns:
            Tuple[int, int]: (发送成功的通知数量, 发送失败的通知数量)
        """
        notify_sent = 0
        notify_failed = 0
        
        for docker in docker_list:
            # 检查容器是否需要发送通知
            if docker["haveUpdate"] and docker["name"] in self._updatable_list:
                logger.info(f"{self._log_prefix} 发现容器 {docker['name']} 有可用更新")
                
                try:
                    # 根据镜像格式发送不同的通知
                    if docker["usingImage"] and not docker["usingImage"].startswith("sha256:"):
                        self._send_notification(
                            title="🔔 【DC助手-更新通知】",
                            text=f"🎉 您有容器可以更新啦！\n📦 【{docker['name']}】\n🔹 当前镜像:{docker['usingImage']}\n🔸 状态:{docker['status']} {docker['runningTime']}\n📅 构建时间：{docker['createTime']}"
                        )
                        notify_sent += 1
                    else:
                        self._send_notification(
                            title="⚠️ 【DC助手-更新通知】",
                            text=f"⚠️ 监测到您有容器TAG不正确\n📦 【{docker['name']}】\n🔹 当前镜像:{docker['usingImage']}\n🔸 状态:{docker['status']} "
                                 f"{docker['runningTime']}\n📅 构建时间：{docker['createTime']}\n❌ 该镜像无法通过DC自动更新,请修改TAG"
                        )
                        notify_sent += 1
                        
                except Exception as e:
                    logger.error(f"{self._log_prefix} 发送容器 {docker['name']} 通知失败: {str(e)}")
                    notify_failed += 1
        
        return notify_sent, notify_failed

    def _handle_backup_result(self, data: Dict):
        """
        处理备份结果
        
        Args:
            data: 备份API的响应数据
        """
        if data.get("code") == 200:
            logger.info(f"{self._log_prefix} 备份成功")
            self._backup_success_count += 1
            
            # 发送成功通知
            if self._backups_notify:
                self._send_notification(
                    title="✅ 【DC助手-备份成功】",
                    text="💾 Docker备份成功！"
                )
                
        else:
            logger.error(f"{self._log_prefix} 备份失败: {data.get('msg', '未知错误')}")
            self._backup_fail_count += 1
            
            # 发送失败通知
            if self._backups_notify:
                self._send_notification(
                    title="❌ 【DC助手-备份失败】",
                    text=f"❌ Docker备份失败拉~！\n⚠️ 【失败原因】:{data.get('msg', '未知错误')}"
                )

    def _send_notification(self, title: str, text: str):
        """
        发送通知的辅助方法
        
        Args:
            title: 通知标题
            text: 通知内容
        """
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=title,
                text=text
            )
            self._notify_sent_count += 1
            logger.debug(f"{self._log_prefix} 通知发送成功: {title}")
        except Exception as e:
            logger.error(f"{self._log_prefix} 通知发送失败: {str(e)}")
            self._notify_failed_count += 1

    # ==================== 事件处理器 ====================

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        """
        远程同步事件处理
        
        Args:
            event: 事件对象
        """
        # 当前版本未实现远程同步功能
        pass

    # ==================== 系统接口方法 ====================

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        获取插件命令
        
        Returns:
            List[Dict[str, Any]]: 命令列表
        """
        # 当前版本未定义命令
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        
        Returns:
            List[Dict[str, Any]]: API列表
        """
        # 当前版本未提供API
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        获取插件配置页面
        
        Returns:
            Tuple[List[dict], Dict[str, Any]]: (表单配置列表, 表单默认值)
        """
        logger.debug(f"{self._log_prefix} 加载配置表单")
        
        # 获取容器选项列表
        updatable_list, auto_update_list = self._get_container_options()
        
        # 构造表单配置
        form_config = self._build_form_config(updatable_list, auto_update_list)
        
        # 构造表单默认值
        form_defaults = {
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
            "_tabs": "C1"  # 默认显示第一个标签页
        }
        
        return form_config, form_defaults

    def get_page(self) -> List[dict]:
        """
        获取插件详情页面
        
        Returns:
            List[dict]: 页面配置列表
        """
        logger.info(f"{self._log_prefix} 加载插件详情页面")
        
        # 获取容器列表和更新状态
        docker_list = self.get_docker_list()
        updatable_containers = [
            container["name"] 
            for container in docker_list 
            if container.get("haveUpdate")
        ] if docker_list else []
        
        # 检查任务配置状态
        update_notify_set = bool(self._update_cron and self._updatable_list)
        auto_update_set = bool(self._auto_update_cron and self._auto_update_list)
        auto_backup_set = bool(self._backup_cron)
        
        # 计算启用的任务数量
        enabled_tasks = sum([
            1 if update_notify_set else 0,
            1 if auto_update_set else 0,
            1 if auto_backup_set else 0
        ]) if self._enabled else 0
        
        # 构造详情页面
        return self._build_detail_page(
            docker_list, 
            updatable_containers, 
            update_notify_set, 
            auto_update_set, 
            auto_backup_set, 
            enabled_tasks
        )

    # ==================== 表单和页面构建方法 ====================

    def _get_container_options(self) -> Tuple[List[Dict], List[Dict]]:
        """
        获取容器选项列表
        
        Returns:
            Tuple[List[Dict], List[Dict]]: (更新通知容器选项, 自动更新容器选项)
        """
        updatable_list = []
        auto_update_list = []
        
        # 如果配置了服务器和密钥，获取容器列表
        if self._secretKey and self._host:
            try:
                data = self.get_docker_list()
                if data:
                    # 清理无效的容器选择
                    self._cleanup_invalid_container_selections(data)
                    
                    # 生成选项列表
                    for item in data:
                        if item.get('name'):
                            container_option = {"title": item["name"], "value": item["name"]}
                            updatable_list.append(container_option)
                            auto_update_list.append(container_option)
                    
                    logger.debug(f"{self._log_prefix} 表单加载 {len(data)} 个容器选项")
            
            except Exception as e:
                logger.error(f"{self._log_prefix} 表单加载容器列表失败: {str(e)}")
        
        return updatable_list, auto_update_list

    def _cleanup_invalid_container_selections(self, data: List[Dict]):
        """
        清理无效的容器选择
        
        Args:
            data: 容器列表
        """
        # 获取有效的容器名称
        valid_names = [item.get('name') for item in data if item.get('name')]
        
        # 清理更新通知列表中的无效容器
        if self._updatable_list:
            self._updatable_list = [
                item for item in self._updatable_list 
                if item in valid_names
            ]
        
        # 清理自动更新列表中的无效容器
        if self._auto_update_list:
            self._auto_update_list = [
                item for item in self._auto_update_list 
                if item in valid_names
            ]
        
        # 如果列表有变化，更新配置
        if self._updatable_list or self._auto_update_list:
            self.__update_config()
        
        # 确保列表不为空
        self._updatable_list = self._updatable_list or []
        self._auto_update_list = self._auto_update_list or []

    def _build_form_config(self, updatable_list: List[Dict], auto_update_list: List[Dict]) -> List[dict]:
        """
        构建表单配置
        
        Args:
            updatable_list: 更新通知容器选项
            auto_update_list: 自动更新容器选项
            
        Returns:
            List[dict]: 表单配置
        """
        return [
            {
                "component": "VForm",
                "content": [
                    # 第一行：启用开关和立即运行
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    
                    # 第二行：服务器配置
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "host",
                                            "label": "服务器地址",
                                            "placeholder": "http://localhost:8080",
                                            "hint": "DockerCopilot服务地址"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "secretKey",
                                            "label": "DockerCopilot密钥",
                                            "placeholder": "DockerCopilot密钥",
                                            "hint": "环境变量查看"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    
                    # 第三行：标签页
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VTabs",
                                "props": {
                                    "model": "_tabs",
                                    "height": 40,
                                },
                                "content": [
                                    {
                                        "component": "VTab",
                                        "props": {"value": "C1"},
                                        "text": "更新通知"
                                    },
                                    {
                                        "component": "VTab",
                                        "props": {"value": "C2"},
                                        "text": "自动更新"
                                    },
                                    {
                                        "component": "VTab",
                                        "props": {"value": "C3"},
                                        "text": "自动备份"
                                    }
                                ]
                            }]
                        }]
                    },
                    
                    # 第四行：标签页内容
                    {
                        "component": "VWindow",
                        "props": {"model": "_tabs"},
                        "content": [
                            # 标签页1：更新通知
                            self._build_update_notify_tab(updatable_list),
                            
                            # 标签页2：自动更新
                            self._build_auto_update_tab(auto_update_list),
                            
                            # 标签页3：自动备份
                            self._build_backup_tab()
                        ]
                    }
                ]
            }
        ]

    def _build_update_notify_tab(self, updatable_list: List[Dict]) -> Dict:
        """
        构建更新通知标签页
        
        Args:
            updatable_list: 容器选项列表
            
        Returns:
            Dict: 标签页配置
        """
        return {
            "component": "VWindowItem",
            "props": {"value": "C1", "style": {"margin-top": "30px"}},
            "content": [
                # 定时配置
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "updatecron",
                                        "label": "更新通知周期",
                                        "placeholder": "15 8-23/2 * * *",
                                        "hint": "Cron表达式"
                                    }
                                }
                            ]
                        }
                    ]
                },
                
                # 容器选择
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VSelect",
                                    "props": {
                                        "chips": True,
                                        "multiple": True,
                                        "model": "updatablelist",
                                        "label": "更新通知容器",
                                        "items": updatable_list,
                                        "hint": "选择容器在有更新时发送通知"
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }

    def _build_auto_update_tab(self, auto_update_list: List[Dict]) -> Dict:
        """
        构建自动更新标签页
        
        Args:
            auto_update_list: 容器选项列表
            
        Returns:
            Dict: 标签页配置
        """
        return {
            "component": "VWindowItem",
            "props": {"value": "C2", "style": {"margin-top": "30px"}},
            "content": [
                # 定时和跟踪配置
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "autoupdatecron",
                                        "label": "自动更新周期",
                                        "placeholder": "15 2 * * *",
                                        "hint": "Cron表达式"
                                    }
                                }
                            ]
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "interval",
                                        "label": "跟踪间隔(秒)",
                                        "placeholder": "10",
                                        "hint": "开启进度汇报时,每多少秒检查一次进度状态，默认10秒"
                                    }
                                }
                            ]
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "intervallimit",
                                        "label": "检查次数",
                                        "placeholder": "6",
                                        "hint": "开启进度汇报，当达限制检查次数后放弃追踪,默认6次"
                                    }
                                }
                            ]
                        }
                    ]
                },
                
                # 功能开关
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
                                        "model": "autoupdatenotify",
                                        "label": "自动更新通知",
                                        "hint": "更新任务创建成功发送通知"
                                    }
                                }
                            ]
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "schedulereport",
                                        "label": "进度汇报",
                                        "hint": "追踪更新任务进度并发送通知"
                                    }
                                }
                            ]
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "deleteimages",
                                        "label": "清理镜像",
                                        "hint": "在下次执行时清理无tag且不在使用中的全部镜像"
                                    }
                                }
                            ]
                        }
                    ]
                },
                
                # 容器选择
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VSelect",
                                    "props": {
                                        "chips": True,
                                        "multiple": True,
                                        "model": "autoupdatelist",
                                        "label": "自动更新容器",
                                        "items": auto_update_list,
                                        "hint": "被选择的容器当有新版本时自动更新"
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }

    def _build_backup_tab(self) -> Dict:
        """
        构建自动备份标签页
        
        Returns:
            Dict: 标签页配置
        """
        return {
            "component": "VWindowItem",
            "props": {"value": "C3", "style": {"margin-top": "30px"}},
            "content": [
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "backupcron",
                                        "label": "自动备份",
                                        "placeholder": "0 7 * * *",
                                        "hint": "Cron表达式"
                                    }
                                }
                            ]
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "backupsnotify",
                                        "label": "备份通知",
                                        "hint": "备份成功发送通知"
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }

    def _build_status_overview_row(self, docker_list: List[Dict], enabled_tasks: int) -> Dict:
        """
        构建状态概览行（调整布局，运行状态:定时任务:服务器 = 1:3:1）
        
        Args:
            docker_list: 容器列表
            enabled_tasks: 启用的任务数量
            
        Returns:
            Dict: 状态概览行配置
        """
        return {
            "component": "VRow",
            "props": {
                "class": "mb-3"
            },
            "content": [
                # 运行状态卡片（宽度比例1）
                {
                    "component": "VCol",
                    "props": {
                        "cols": 12,
                        "md": 2
                    },
                    "content": [
                        {
                            "component": "VCard",
                            "props": {
                                "variant": "outlined",
                                "class": "h-100"
                            },
                            "content": [
                                {
                                    "component": "VCardTitle",
                                    "props": {
                                        "class": "pa-2 text-center"
                                    },
                                    "text": "运行状态"
                                },
                                {
                                    "component": "VDivider"
                                },
                                {
                                    "component": "VCardText",
                                    "props": {
                                        "class": "pa-2 text-center"
                                    },
                                    "content": [
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "d-flex flex-column align-center"
                                            },
                                            "content": [
                                                {
                                                    "component": "div",
                                                    "props": {
                                                        "class": "text-h4 mb-1"
                                                    },
                                                    "text": "✅" if self._enabled else "❌"
                                                },
                                                {
                                                    "component": "div",
                                                    "props": {
                                                        "class": "text-h6"
                                                    },
                                                    "text": "已启用" if self._enabled else "未启用"
                                                },
                                                {
                                                    "component": "div",
                                                    "props": {
                                                        "class": "text-caption text-medium-emphasis mt-1"
                                                    },
                                                    "text": f"{enabled_tasks} 个任务" if self._enabled else ""
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                },
                
                # 定时任务栏（宽度比例3）
                {
                    "component": "VCol",
                    "props": {
                        "cols": 12,
                        "md": 6
                    },
                    "content": [
                        {
                            "component": "VCard",
                            "props": {
                                "variant": "outlined",
                                "class": "h-100"
                            },
                            "content": [
                                {
                                    "component": "VCardTitle",
                                    "props": {
                                        "class": "pa-2 text-center"
                                    },
                                    "text": "定时任务"
                                },
                                {
                                    "component": "VDivider"
                                },
                                {
                                    "component": "VCardText",
                                    "props": {
                                        "class": "pa-2"
                                    },
                                    "content": [
                                        {
                                            "component": "VRow",
                                            "content": [
                                                # 更新通知定时任务
                                                self._build_schedule_card_mini(
                                                    "更新通知", 
                                                    bool(self._update_cron and self._updatable_list), 
                                                    self._update_cron, 
                                                    "info"
                                                ),
                                                
                                                # 自动更新定时任务
                                                self._build_schedule_card_mini(
                                                    "自动更新", 
                                                    bool(self._auto_update_cron and self._auto_update_list), 
                                                    self._auto_update_cron, 
                                                    "warning"
                                                ),
                                                
                                                # 自动备份定时任务
                                                self._build_schedule_card_mini(
                                                    "自动备份", 
                                                    bool(self._backup_cron), 
                                                    self._backup_cron, 
                                                    "success"
                                                )
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                },
                
                # 服务器地址卡片（宽度比例1）
                {
                    "component": "VCol",
                    "props": {
                        "cols": 12,
                        "md": 4
                    },
                    "content": [
                        {
                            "component": "VCard",
                            "props": {
                                "variant": "outlined",
                                "class": "h-100"
                            },
                            "content": [
                                {
                                    "component": "VCardTitle",
                                    "props": {
                                        "class": "pa-2 text-center"
                                    },
                                    "text": "服务器"
                                },
                                {
                                    "component": "VDivider"
                                },
                                {
                                    "component": "VCardText",
                                    "props": {
                                        "class": "pa-2 text-center"
                                    },
                                    "content": [
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "d-flex flex-column align-center"
                                            },
                                            "content": [
                                                {
                                                    "component": "div",
                                                    "props": {
                                                        "class": "text-h4 mb-1"
                                                    },
                                                    "text": "🌐"
                                                },
                                                {
                                                    "component": "div",
                                                    "props": {
                                                        "class": "text-h6 text-truncate",
                                                        "style": "max-width: 100%"
                                                    },
                                                    "text": self._host if self._host else "未设置"
                                                },
                                                {
                                                    "component": "div",
                                                    "props": {
                                                        "class": "text-caption text-medium-emphasis mt-1"
                                                    },
                                                    "text": f"{len(docker_list)} 个容器" if docker_list else "未连接"
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

    def _build_schedule_card_mini(self, title: str, is_set: bool, cron: str, color: str) -> Dict:
        """
        构建紧凑版定时任务卡片（用于状态概览行）
        
        Args:
            title: 卡片标题
            is_set: 是否已配置
            cron: cron表达式
            color: 卡片颜色
            
        Returns:
            Dict: 卡片配置
        """
        return {
            "component": "VCol",
            "props": {
                "cols": 12,
                "md": 4
            },
            "content": [
                {
                    "component": "VCard",
                    "props": {
                        "variant": "tonal",
                        "color": color if is_set else "grey",
                        "class": "text-center h-100 pa-1"
                    },
                    "content": [
                        {
                            "component": "VCardText",
                            "props": {
                                "class": "pa-1"
                            },
                            "content": [
                                {
                                    "component": "div",
                                    "props": {
                                        "class": "text-subtitle-2 mb-1"
                                    },
                                    "text": title
                                },
                                {
                                    "component": "div",
                                    "props": {
                                        "class": "text-h6 mb-1"
                                    },
                                    "text": "✅" if is_set else "❌"
                                },
                                {
                                    "component": "div",
                                    "props": {
                                        "class": "text-caption text-medium-emphasis text-truncate",
                                        "style": "max-width: 100%"
                                    },
                                    "text": cron if cron else "未配置"
                                }
                            ]
                        }
                    ]
                }
            ]
        }

    def _build_updatable_containers_row(self, updatable_containers: List[str]) -> Dict:
        """
        构建可更新容器状态行
        
        Args:
            updatable_containers: 可更新容器列表
            
        Returns:
            Dict: 可更新容器状态行配置
        """
        return {
            "component": "VCard",
            "props": {
                "variant": "outlined",
                "class": "mb-3"
            },
            "content": [
                {
                    "component": "VCardTitle",
                    "props": {
                        "class": "pa-3"
                    },
                    "text": "检查更新"
                },
                {
                    "component": "VDivider"
                },
                {
                    "component": "VCardText",
                    "props": {
                        "class": "pa-3"
                    },
                    "content": [
                        {
                            "component": "div",
                            "props": {
                                "class": "d-flex align-center justify-space-between mb-2"
                            },
                            "content": [
                                {
                                    "component": "div",
                                    "props": {
                                        "class": "d-flex align-center"
                                    },
                                    "content": [
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "text-h4 mr-2"
                                            },
                                            "text": "🆕" if updatable_containers else "📦"
                                        },
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "text-h6"
                                            },
                                            "text": f"{len(updatable_containers)} 个可更新容器"
                                        }
                                    ]
                                }
                            ]
                        },
                        {
                            "component": "div",
                            "props": {
                                "class": "mt-3"
                            },
                            "content": [
                                {
                                    "component": "div",
                                    "props": {
                                        "class": "text-body-2 mb-1"
                                    },
                                    "text": "可更新容器列表:"
                                },
                                {
                                    "component": "div",
                                    "props": {
                                        "class": "d-flex flex-wrap gap-1 mt-2"
                                    },
                                    "content": [
                                        self._build_container_chip(container_name, "warning")
                                        for container_name in updatable_containers
                                    ] if updatable_containers else [
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "text-caption text-medium-emphasis"
                                            },
                                            "text": "暂无可用更新"
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }

    def _build_container_config_row(self) -> Dict:
        """
        构建容器配置行（合并了容器名称详情）
        
        Returns:
            Dict: 容器配置行配置
        """
        return {
            "component": "VRow",
            "props": {
                "class": "mb-3"
            },
            "content": [
                # 更新通知容器卡片
                {
                    "component": "VCol",
                    "props": {
                        "cols": 12,
                        "md": 6
                    },
                    "content": [
                        {
                            "component": "VCard",
                            "props": {
                                "variant": "outlined",
                                "class": "h-100"
                            },
                            "content": [
                                {
                                    "component": "VCardTitle",
                                    "props": {
                                        "class": "pa-3"
                                    },
                                    "text": "更新通知"
                                },
                                {
                                    "component": "VDivider"
                                },
                                {
                                    "component": "VCardText",
                                    "props": {
                                        "class": "pa-3"
                                    },
                                    "content": [
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "d-flex align-center justify-space-between mb-3"
                                            },
                                            "content": [
                                                {
                                                    "component": "div",
                                                    "props": {
                                                        "class": "d-flex align-center"
                                                    },
                                                    "content": [
                                                        {
                                                            "component": "div",
                                                            "props": {
                                                                "class": "text-h4 mr-2"
                                                            },
                                                            "text": "🔔"
                                                        },
                                                        {
                                                            "component": "div",
                                                            "props": {
                                                                "class": "text-h6"
                                                            },
                                                            "text": f"{len(self._updatable_list)} 个容器"
                                                        }
                                                    ]
                                                }
                                            ]
                                        },
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "text-body-2 mb-2"
                                            },
                                            "text": "以下容器在有更新时会收到通知："
                                        },
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "mt-2"
                                            },
                                            "content": [
                                                {
                                                    "component": "div",
                                                    "props": {
                                                        "class": "d-flex flex-wrap gap-1"
                                                    },
                                                    "content": [
                                                        self._build_container_chip(container_name, "primary")
                                                        for container_name in self._updatable_list
                                                    ] if self._updatable_list else [
                                                        {
                                                            "component": "div",
                                                            "props": {
                                                                "class": "text-caption text-medium-emphasis"
                                                            },
                                                            "text": "未选择任何容器"
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
                
                # 自动更新容器卡片
                {
                    "component": "VCol",
                    "props": {
                        "cols": 12,
                        "md": 6
                    },
                    "content": [
                        {
                            "component": "VCard",
                            "props": {
                                "variant": "outlined",
                                "class": "h-100"
                            },
                            "content": [
                                {
                                    "component": "VCardTitle",
                                    "props": {
                                        "class": "pa-3"
                                    },
                                    "text": "自动更新"
                                },
                                {
                                    "component": "VDivider"
                                },
                                {
                                    "component": "VCardText",
                                    "props": {
                                        "class": "pa-3"
                                    },
                                    "content": [
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "d-flex align-center justify-space-between mb-3"
                                            },
                                            "content": [
                                                {
                                                    "component": "div",
                                                    "props": {
                                                        "class": "d-flex align-center"
                                                    },
                                                    "content": [
                                                        {
                                                            "component": "div",
                                                            "props": {
                                                                "class": "text-h4 mr-2"
                                                            },
                                                            "text": "🔄"
                                                        },
                                                        {
                                                            "component": "div",
                                                            "props": {
                                                                "class": "text-h6"
                                                            },
                                                            "text": f"{len(self._auto_update_list)} 个容器"
                                                        }
                                                    ]
                                                }
                                            ]
                                        },
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "text-body-2 mb-2"
                                            },
                                            "text": "以下容器在有更新时会自动更新："
                                        },
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "mt-2"
                                            },
                                            "content": [
                                                {
                                                    "component": "div",
                                                    "props": {
                                                        "class": "d-flex flex-wrap gap-1"
                                                    },
                                                    "content": [
                                                        self._build_container_chip(container_name, "success")
                                                        for container_name in self._auto_update_list
                                                    ] if self._auto_update_list else [
                                                        {
                                                            "component": "div",
                                                            "props": {
                                                                "class": "text-caption text-medium-emphasis"
                                                            },
                                                            "text": "未选择任何容器"
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

    def _build_container_chip(self, container_name: str, color: str) -> Dict:
        """
        构建容器标签（Chip）
        
        Args:
            container_name: 容器名称
            color: 标签颜色
            
        Returns:
            Dict: 容器标签配置
        """
        return {
            "component": "VChip",
            "props": {
                "color": color,
                "size": "small",
                "class": "ma-1"
            },
            "text": container_name
        }

    def _build_statistics_row(self) -> Dict:
        """
        构建统计信息行
        
        Returns:
            Dict: 统计信息行配置
        """
        return {
            "component": "VCard",
            "props": {
                "variant": "outlined"
            },
            "content": [
                {
                    "component": "VCardTitle",
                    "props": {
                        "class": "pa-3"
                    },
                    "text": "操作统计"
                },
                {
                    "component": "VDivider"
                },
                {
                    "component": "VCardText",
                    "props": {
                        "class": "pa-3"
                    },
                    "content": [
                        {
                            "component": "VRow",
                            "content": [
                                # 更新成功
                                self._build_stat_card(
                                    "更新成功", 
                                    self._update_success_count, 
                                    "success"
                                ),
                                
                                # 更新失败
                                self._build_stat_card(
                                    "更新失败", 
                                    self._update_fail_count, 
                                    "error"
                                ),
                                
                                # 备份成功
                                self._build_stat_card(
                                    "备份成功", 
                                    self._backup_success_count, 
                                    "success"
                                ),
                                
                                # 清理成功
                                self._build_stat_card(
                                    "清理成功", 
                                    self._cleanup_success_count, 
                                    "success"
                                )
                            ]
                        }
                    ]
                }
            ]
        }

    def _build_stat_card(self, title: str, value: int, color: str) -> Dict:
        """
        构建单个统计卡片
        
        Args:
            title: 卡片标题
            value: 统计值
            color: 卡片颜色
            
        Returns:
            Dict: 卡片配置
        """
        return {
            "component": "VCol",
            "props": {
                "cols": 6,
                "sm": 3
            },
            "content": [
                {
                    "component": "VCard",
                    "props": {
                        "variant": "tonal",
                        "color": color,
                        "class": "text-center pa-2"
                    },
                    "content": [
                        {
                            "component": "div",
                            "props": {
                                "class": "text-h5"
                            },
                            "text": f"{value}"
                        },
                        {
                            "component": "div",
                            "props": {
                                "class": "text-caption"
                            },
                            "text": title
                        }
                    ]
                }
            ]
        }

    def _build_detail_page(self, docker_list: List[Dict], updatable_containers: List[str],
                          update_notify_set: bool, auto_update_set: bool, 
                          auto_backup_set: bool, enabled_tasks: int) -> List[dict]:
        """
        构建详情页面（调整布局结构）
        
        Args:
            docker_list: 容器列表
            updatable_containers: 可更新容器列表
            update_notify_set: 更新通知是否配置
            auto_update_set: 自动更新是否配置
            auto_backup_set: 自动备份是否配置
            enabled_tasks: 启用的任务数量
            
        Returns:
            List[dict]: 详情页面配置
        """
        return [
            {
                "component": "VCard",
                "content": [
                    {
                        "component": "VCardText",
                        "props": {
                            "class": "pa-4"
                        },
                        "content": [
                            # 第一行：运行状态、定时任务、服务器（1:3:1比例）
                            self._build_status_overview_row(docker_list, enabled_tasks),
                            
                            # 第二行：可更新容器状态（原检查更新行）
                            self._build_updatable_containers_row(updatable_containers),
                            
                            # 第三行：容器配置（合并了容器名称详情）
                            self._build_container_config_row(),
                            
                            # 第四行：操作统计
                            self._build_statistics_row()
                        ]
                    }
                ]
            }
        ]