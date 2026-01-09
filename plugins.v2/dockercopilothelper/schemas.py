"""
DockerCopilotHelper - 数据模型定义
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from datetime import datetime


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
class DockerImage:
    """Docker镜像信息"""
    id: str
    tag: Optional[str] = None
    inUsed: bool = False


@dataclass
class UpdateTask:
    """更新任务信息"""
    taskID: str
    containerName: str
    imageName: str
    status: str = "pending"
    progress: str = "0%"
    start_time: datetime = field(default_factory=datetime.now)


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


@dataclass
class DockerCopilotResponse:
    """DockerCopilot API响应"""
    code: int
    msg: str
    data: Optional[Dict[str, Any]] = None