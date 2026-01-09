"""
DockerCopilotHelper - 服务封装
"""
import jwt
import time
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta

from app.utils.http import RequestUtils
from app.log import logger
from app.schemas.types import NotificationType

from .schemas import (
    DockerContainer, DockerImage, UpdateTask,
    DockerCopilotResponse, PluginConfig
)


class DockerCopilotAPIClient:
    """DockerCopilot API客户端封装"""
    
    def __init__(self, host: str, secret_key: str):
        self.host = host.rstrip('/')
        self.secret_key = secret_key
        self._jwt_token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None
        
    def _get_jwt_token(self) -> str:
        """获取JWT令牌"""
        # 检查令牌是否有效
        if (self._jwt_token and self._token_expiry and 
            self._token_expiry > datetime.now()):
            return self._jwt_token
            
        # 生成新令牌（28天有效期）
        payload = {
            "exp": int(time.time()) + 28 * 24 * 60 * 60,
            "iat": int(time.time())
        }
        self._jwt_token = jwt.encode(payload, self.secret_key, algorithm="HS256")
        self._token_expiry = datetime.now() + timedelta(days=27)  # 提前一天过期
        
        logger.debug(f"DockerCopilot 生成新的JWT令牌，有效期至: {self._token_expiry}")
        return self._jwt_token
    
    @property
    def headers(self) -> Dict[str, str]:
        """获取请求头"""
        return {
            "Authorization": f"Bearer {self._get_jwt_token()}",
            "Content-Type": "application/json"
        }
    
    def _make_request(self, method: str, endpoint: str, **kwargs) -> DockerCopilotResponse:
        """发送HTTP请求"""
        url = f"{self.host}{endpoint}"
        
        try:
            if method.upper() == "GET":
                response = RequestUtils(headers=self.headers).get_res(url, **kwargs)
            elif method.upper() == "POST":
                response = RequestUtils(headers=self.headers).post_res(url, **kwargs)
            elif method.upper() == "DELETE":
                response = RequestUtils(headers=self.headers).delete_res(url, **kwargs)
            else:
                raise ValueError(f"不支持的HTTP方法: {method}")
            
            if response is None:
                return DockerCopilotResponse(
                    code=500,
                    msg="请求失败，无响应"
                )
                
            response_data = response.json()
            return DockerCopilotResponse(
                code=response_data.get("code", 500),
                msg=response_data.get("msg", "未知错误"),
                data=response_data.get("data")
            )
            
        except Exception as e:
            logger.error(f"DockerCopilot API请求失败: {str(e)}")
            return DockerCopilotResponse(
                code=500,
                msg=f"请求异常: {str(e)}"
            )
    
    def get_containers(self) -> List[DockerContainer]:
        """获取容器列表"""
        response = self._make_request("GET", "/api/containers")
        if response.code == 0 and response.data:
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
                for container in response.data
            ]
        return []
    
    def get_images(self) -> List[DockerImage]:
        """获取镜像列表"""
        response = self._make_request("GET", "/api/images")
        if response.code == 200 and response.data:
            return [
                DockerImage(
                    id=image.get("id", ""),
                    tag=image.get("tag"),
                    inUsed=image.get("inUsed", False)
                )
                for image in response.data
            ]
        return []
    
    def update_container(self, container_id: str, container_name: str, 
                        image_name: str) -> Optional[UpdateTask]:
        """更新容器"""
        response = self._make_request(
            "POST",
            f"/api/container/{container_id}/update",
            json={
                "containerName": container_name,
                "imageNameAndTag": [image_name]
            }
        )
        
        if response.code == 200 and response.data:
            return UpdateTask(
                taskID=response.data.get("taskID"),
                containerName=container_name,
                imageName=image_name
            )
        return None
    
    def get_update_progress(self, task_id: str) -> str:
        """获取更新进度"""
        response = self._make_request("GET", f"/api/progress/{task_id}")
        if response.code == 200:
            return response.msg
        return "未知状态"
    
    def backup_containers(self) -> bool:
        """备份容器"""
        response = self._make_request("GET", "/api/container/backup")
        return response.code == 200
    
    def remove_image(self, image_id: str) -> bool:
        """删除镜像"""
        response = self._make_request("DELETE", f"/api/image/{image_id}?force=false")
        return response.code == 200