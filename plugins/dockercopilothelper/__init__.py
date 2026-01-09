from datetime import datetime, timedelta
from typing import Optional, Any, List, Dict, Tuple
import time
import pytz
import jwt
import requests
from requests import Session, Response
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.core.event import eventmanager, Event
from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils


class DockerCopilotHelper(_PluginBase):
    # 插件名称
    plugin_name = "DC助手AI版"
    # 插件描述
    plugin_desc = "配合DockerCopilot,完成更新通知、自动更改、自动备份功能"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/siryle/MoviePilot-Plugins/main/icons/Docker_Copilot.png"
    # 插件版本
    plugin_version = "1.2.0"
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
    _intervallimit = None
    _interval = None
    _backup_cron = None
    _backups_notify = False
    _host = None
    _secretKey = None
    _scheduler: Optional[BackgroundScheduler] = None
    _jwt_token: Optional[str] = None
    _jwt_expiry: Optional[datetime] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()
        
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._update_cron = config.get("updatecron")
            self._updatable_list = config.get("updatablelist") or []
            self._updatable_notify = config.get("updatablenotify")
            self._auto_update_cron = config.get("autoupdatecron")
            self._auto_update_list = config.get("autoupdatelist") or []
            self._auto_update_notify = config.get("autoupdatenotify")
            self._schedule_report = config.get("schedulereport")
            self._delete_images = config.get("deleteimages")
            self._backup_cron = config.get("backupcron")
            self._backups_notify = config.get("backupsnotify")
            self._intervallimit = int(config.get("intervallimit") or 6)
            self._interval = int(config.get("interval") or 10)
            self._host = config.get("host")
            self._secretKey = config.get("secretKey")

            # 重置JWT token
            self._jwt_token = None
            self._jwt_expiry = None

            # 验证配置
            if not self._secretKey or not self._host:
                logger.error("DC助手服务结束: secretKey或host未填写")
                self.systemmessage.put("DC助手: secretKey或host未填写")
                return False

            # 加载模块
            if self._enabled or self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                
                # 立即运行一次
                if self._onlyonce:
                    logger.info("DC助手服务启动，立即运行一次")
                    job_added = False
                    
                    if self._backup_cron:
                        self._scheduler.add_job(
                            self.backup, 'date',
                            run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                            name="DC助手-备份"
                        )
                        job_added = True
                        
                    if self._update_cron:
                        self._scheduler.add_job(
                            self.updatable, 'date',
                            run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=6),
                            name="DC助手-更新通知"
                        )
                        job_added = True
                        
                    if self._auto_update_cron:
                        self._scheduler.add_job(
                            self.auto_update, 'date',
                            run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=10),
                            name="DC助手-自动更新"
                        )
                        job_added = True
                    
                    if job_added:
                        # 关闭一次性开关
                        self._onlyonce = False
                        self.__update_config()
                
                # 周期运行
                if self._backup_cron:
                    try:
                        self._scheduler.add_job(
                            func=self.backup,
                            trigger=CronTrigger.from_crontab(self._backup_cron),
                            name="DC助手-备份"
                        )
                        logger.info(f"已添加备份任务，执行周期: {self._backup_cron}")
                    except Exception as err:
                        logger.error(f"备份定时任务配置错误：{str(err)}")
                        self.systemmessage.put(f"备份执行周期配置错误：{err}")
                
                if self._update_cron:
                    try:
                        self._scheduler.add_job(
                            func=self.updatable,
                            trigger=CronTrigger.from_crontab(self._update_cron),
                            name="DC助手-更新通知"
                        )
                        logger.info(f"已添加更新通知任务，执行周期: {self._update_cron}")
                    except Exception as err:
                        logger.error(f"更新通知定时任务配置错误：{str(err)}")
                        self.systemmessage.put(f"更新通知执行周期配置错误：{err}")
                
                if self._auto_update_cron:
                    try:
                        self._scheduler.add_job(
                            func=self.auto_update,
                            trigger=CronTrigger.from_crontab(self._auto_update_cron),
                            name="DC助手-自动更新"
                        )
                        logger.info(f"已添加自动更新任务，执行周期: {self._auto_update_cron}")
                    except Exception as err:
                        logger.error(f"自动更新定时任务配置错误：{str(err)}")
                        self.systemmessage.put(f"自动更新执行周期配置错误：{err}")
                
                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()
                    logger.info("DC助手定时任务已启动")

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self):
        """更新配置"""
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
            "interval": self._interval
        })

    def get_valid_jwt(self) -> Optional[str]:
        """获取有效的JWT token，必要时重新获取"""
        if self._jwt_token and self._jwt_expiry and datetime.now() < self._jwt_expiry:
            return self._jwt_token
        
        # 使用官方API获取JWT
        try:
            auth_url = f"{self._host}/api/auth"
            response = requests.post(
                auth_url,
                json={"secretKey": self._secretKey},
                timeout=10,
                verify=False
            )
            
            if response.status_code == 201:
                data = response.json()
                if data.get("code") == 201:
                    self._jwt_token = f"Bearer {data['data']['jwt']}"
                    # 设置过期时间（假设JWT有效期为24小时）
                    self._jwt_expiry = datetime.now() + timedelta(hours=23)
                    logger.debug("JWT token获取成功")
                    return self._jwt_token
                else:
                    logger.error(f"获取JWT失败: {data.get('msg')}")
            else:
                logger.error(f"获取JWT HTTP错误: {response.status_code}")
        except Exception as e:
            logger.error(f"获取JWT时发生异常: {str(e)}")
        
        return None

    def auto_update(self):
        """自动更新"""
        logger.info("DC助手-自动更新-准备执行")
        
        if not self._auto_update_cron or not self._auto_update_list:
            logger.info("自动更新未配置或未选择容器")
            return
        
        jwt_token = self.get_valid_jwt()
        if not jwt_token:
            logger.error("无法获取有效的JWT token，自动更新终止")
            return
        
        containers = self.get_docker_list()
        if not containers:
            logger.error("无法获取容器列表，自动更新终止")
            return
        
        # 清理无标签且不在使用的镜像
        if self._delete_images:
            self.cleanup_unused_images(jwt_token)
        
        # 自动更新选中的容器
        update_count = 0
        for container_name in self._auto_update_list:
            for container in containers:
                if container["name"] == container_name and container.get("haveUpdate"):
                    if self.update_container(container, jwt_token):
                        update_count += 1
        
        if update_count > 0 and self._auto_update_notify:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="【DC助手-自动更新】",
                text=f"自动更新任务执行完成，共更新了{update_count}个容器"
            )

    def update_container(self, container: Dict[str, Any], jwt_token: str) -> bool:
        """更新单个容器"""
        container_name = container["name"]
        
        # 检查镜像标签
        using_image = container.get("usingImage", "")
        if not using_image or using_image.startswith("sha256:"):
            logger.warning(f"容器 {container_name} 镜像标签不正确，无法自动更新")
            if self._updatable_notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【DC助手-自动更新】",
                    text=f"容器 {container_name} 镜像标签不正确，无法自动更新"
                )
            return False
        
        # 执行更新
        try:
            url = f"{self._host}/api/container/{container['id']}/update"
            data = {
                "containerName": container_name,
                "imageNameAndTag": [using_image]
            }
            
            response = requests.post(
                url,
                json=data,
                headers={"Authorization": jwt_token},
                timeout=30,
                verify=False
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get("code") == 200 and result.get("msg") == "success":
                    logger.info(f"容器 {container_name} 更新任务创建成功")
                    
                    if self._auto_update_notify:
                        self.post_message(
                            mtype=NotificationType.Plugin,
                            title="【DC助手-自动更新】",
                            text=f"容器 {container_name} 更新任务创建成功"
                        )
                    
                    # 跟踪进度
                    if self._schedule_report and result.get("data", {}).get("taskID"):
                        self.track_update_progress(
                            result["data"]["taskID"],
                            container_name,
                            jwt_token
                        )
                    
                    return True
                else:
                    logger.error(f"容器 {container_name} 更新失败: {result.get('msg')}")
            else:
                logger.error(f"容器 {container_name} 更新HTTP错误: {response.status_code}")
        
        except Exception as e:
            logger.error(f"容器 {container_name} 更新异常: {str(e)}")
        
        return False

    def track_update_progress(self, task_id: str, container_name: str, jwt_token: str):
        """跟踪更新进度"""
        logger.info(f"开始跟踪容器 {container_name} 的更新进度")
        
        for i in range(self._intervallimit):
            try:
                time.sleep(self._interval)
                
                url = f"{self._host}/api/progress/{task_id}"
                response = requests.get(
                    url,
                    headers={"Authorization": jwt_token},
                    timeout=10,
                    verify=False
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get("code") == 200:
                        progress_msg = data.get("msg", "")
                        
                        self.post_message(
                            mtype=NotificationType.Plugin,
                            title="【DC助手-更新进度】",
                            text=f"容器 {container_name}\n进度: {progress_msg}"
                        )
                        
                        if progress_msg == "更新成功":
                            logger.info(f"容器 {container_name} 更新成功")
                            break
                        elif progress_msg == "更新失败":
                            logger.error(f"容器 {container_name} 更新失败")
                            break
                
                if i == self._intervallimit - 1:
                    logger.warning(f"容器 {container_name} 更新进度跟踪超时")
                    self.post_message(
                        mtype=NotificationType.Plugin,
                        title="【DC助手-更新进度】",
                        text=f"容器 {container_name}\n进度跟踪超时"
                    )
            
            except Exception as e:
                logger.error(f"跟踪容器 {container_name} 更新进度异常: {str(e)}")
                break

    def cleanup_unused_images(self, jwt_token: str):
        """清理未使用的镜像"""
        try:
            images = self.get_images_list()
            if not images:
                return
            
            cleaned_count = 0
            for image in images:
                if not image.get("inUsed") and image.get("tag"):
                    image_id = image.get("id")
                    if image_id and self.remove_image(image_id, jwt_token):
                        cleaned_count += 1
            
            if cleaned_count > 0:
                logger.info(f"清理了 {cleaned_count} 个未使用的镜像")
        
        except Exception as e:
            logger.error(f"清理镜像时发生异常: {str(e)}")

    def updatable(self):
        """更新通知"""
        logger.info("DC助手-更新通知-准备执行")
        
        if not self._update_cron or not self._updatable_list:
            logger.info("更新通知未配置或未选择容器")
            return
        
        containers = self.get_docker_list()
        if not containers:
            logger.error("无法获取容器列表，更新通知终止")
            return
        
        update_available = []
        tag_incorrect = []
        
        for container in containers:
            if container.get("haveUpdate") and container["name"] in self._updatable_list:
                using_image = container.get("usingImage", "")
                
                if using_image and not using_image.startswith("sha256:"):
                    update_available.append({
                        "name": container["name"],
                        "image": using_image,
                        "status": container.get("status", ""),
                        "running_time": container.get("runningTime", ""),
                        "create_time": container.get("createTime", "")
                    })
                else:
                    tag_incorrect.append({
                        "name": container["name"],
                        "image": using_image
                    })
        
        # 发送通知
        if update_available and self._updatable_notify:
            text = "您有容器可以更新啦！\n\n"
            for item in update_available:
                text += f"【{item['name']}】\n"
                text += f"当前镜像: {item['image']}\n"
                text += f"状态: {item['status']} {item['running_time']}\n"
                text += f"构建时间: {item['create_time']}\n\n"
            
            self.post_message(
                mtype=NotificationType.Plugin,
                title="【DC助手-更新通知】",
                text=text.strip()
            )
        
        if tag_incorrect and self._updatable_notify:
            text = "监测到以下容器镜像标签不正确，无法通过DC自动更新:\n\n"
            for item in tag_incorrect:
                text += f"【{item['name']}】\n"
                text += f"当前镜像: {item['image']}\n\n"
            
            self.post_message(
                mtype=NotificationType.Plugin,
                title="【DC助手-标签问题】",
                text=text.strip()
            )

    def backup(self):
        """备份"""
        logger.info("DC助手-备份-准备执行")
        
        jwt_token = self.get_valid_jwt()
        if not jwt_token:
            logger.error("无法获取有效的JWT token，备份终止")
            return
        
        try:
            backup_url = f"{self._host}/api/container/backup"
            response = requests.get(
                backup_url,
                headers={"Authorization": jwt_token},
                timeout=60,
                verify=False
            )
            
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == 200:
                    logger.info("DC助手-备份成功")
                    if self._backups_notify:
                        self.post_message(
                            mtype=NotificationType.Plugin,
                            title="【DC助手-备份成功】",
                            text="镜像备份成功！"
                        )
                else:
                    logger.error(f"DC助手-备份失败: {data.get('msg')}")
                    if self._backups_notify:
                        self.post_message(
                            mtype=NotificationType.Plugin,
                            title="【DC助手-备份失败】",
                            text=f"镜像备份失败！\n原因: {data.get('msg')}"
                        )
            else:
                logger.error(f"DC助手-备份HTTP错误: {response.status_code}")
        
        except Exception as e:
            logger.error(f"DC助手-备份异常: {str(e)}")
            if self._backups_notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【DC助手-备份异常】",
                    text=f"备份过程中发生异常:\n{str(e)}"
                )

    def get_docker_list(self) -> List[Dict[str, Any]]:
        """获取容器列表"""
        jwt_token = self.get_valid_jwt()
        if not jwt_token:
            logger.error("无法获取JWT token，获取容器列表失败")
            return []
        
        try:
            docker_url = f"{self._host}/api/containers"
            response = requests.get(
                docker_url,
                headers={"Authorization": jwt_token},
                timeout=10,
                verify=False
            )
            
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == 0:
                    return data.get("data", [])
                else:
                    logger.error(f"获取容器列表异常: {data.get('msg')}")
            else:
                logger.error(f"获取容器列表HTTP错误: {response.status_code}")
        
        except Exception as e:
            logger.error(f"获取容器列表异常: {str(e)}")
        
        return []

    def get_images_list(self) -> List[Dict[str, Any]]:
        """获取镜像列表"""
        jwt_token = self.get_valid_jwt()
        if not jwt_token:
            logger.error("无法获取JWT token，获取镜像列表失败")
            return []
        
        try:
            images_url = f"{self._host}/api/images"
            response = requests.get(
                images_url,
                headers={"Authorization": jwt_token},
                timeout=10,
                verify=False
            )
            
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == 200:
                    return data.get("data", [])
                else:
                    logger.error(f"获取镜像列表异常: {data.get('msg')}")
            else:
                logger.error(f"获取镜像列表HTTP错误: {response.status_code}")
        
        except Exception as e:
            logger.error(f"获取镜像列表异常: {str(e)}")
        
        return []

    def remove_image(self, image_id: str, jwt_token: str) -> bool:
        """删除镜像"""
        try:
            delete_url = f"{self._host}/api/image/{image_id}?force=false"
            response = requests.delete(
                delete_url,
                headers={"Authorization": jwt_token},
                timeout=30,
                verify=False
            )
            
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == 200:
                    logger.info(f"镜像 {image_id} 删除成功")
                    return True
                else:
                    logger.error(f"删除镜像异常: {data.get('msg')}")
            else:
                logger.error(f"删除镜像HTTP错误: {response.status_code}")
        
        except Exception as e:
            logger.error(f"删除镜像异常: {str(e)}")
        
        return False

    # 其他方法保持不变...
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """拼装插件配置页面"""
        # ... 原有的表单代码保持不变，但可以添加JWT验证的测试按钮
        
    def stop_service(self):
        """退出插件"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
                logger.info("DC助手定时任务已停止")
        except Exception as e:
            logger.error(f"退出插件失败：{str(e)}")