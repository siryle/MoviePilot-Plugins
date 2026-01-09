"""
DockerCopilotHelper - 版本兼容层
"""
import sys
from typing import Dict, Any
from app.core.config import settings


class VersionAdapter:
    """版本适配器"""
    
    @staticmethod
    def is_v2() -> bool:
        """检查是否为V2版本"""
        return hasattr(settings, 'VERSION_FLAG') and settings.VERSION_FLAG == "v2"
    
    @staticmethod
    def adapt_config(config: Dict[str, Any]) -> Dict[str, Any]:
        """适配配置格式"""
        if VersionAdapter.is_v2():
            # V2版本使用新的配置字段名
            mapping = {
                "updatecron": "update_cron",
                "updatablelist": "updatable_list",
                "updatablenotify": "updatable_notify",
                "autoupdatecron": "auto_update_cron",
                "autoupdatelist": "auto_update_list",
                "autoupdatenotify": "auto_update_notify",
                "schedulereport": "schedule_report",
                "deleteimages": "delete_images",
                "backupcron": "backup_cron",
                "backupsnotify": "backups_notify"
            }
            
            adapted = {}
            for old_key, new_key in mapping.items():
                if old_key in config:
                    adapted[new_key] = config[old_key]
            
            # 保留其他字段
            for key, value in config.items():
                if key not in mapping:
                    adapted[key] = value
            
            return adapted
        
        # V1版本保持原样
        return config
    
    @staticmethod
    def get_plugin_base():
        """获取插件基类"""
        if VersionAdapter.is_v2():
            from app.plugin import Plugin
            return Plugin
        else:
            from app.plugins import _PluginBase
            return _PluginBase


def create_compatible_plugin():
    """创建兼容版本的插件"""
    PluginBase = VersionAdapter.get_plugin_base()
    
    if VersionAdapter.is_v2():
        # V2版本
        from . import DockerCopilotHelper as V2Plugin
        return V2Plugin
    else:
        # V1兼容版本
        class V1CompatiblePlugin(PluginBase):
            """V1兼容版本"""
            # 复制V1版本的所有属性和方法
            # 这里可以复用原始的V1代码，或进行适配
            
            def __init__(self):
                # 初始化V1版本
                # 可以调用V2版本的适配逻辑
                pass
            
            # ... 其他V1兼容方法
        
        return V1CompatiblePlugin