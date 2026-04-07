"""
影巢签到插件
版本: 1.4.2
作者: madrays
功能:
- 自动完成影巢(HDHive)每日签到
- 支持多账户同时签到
- 支持签到失败重试
- 保存签到历史记录
- 提供详细的签到通知
- 默认使用代理访问
- 仅支持Cookie登录方式
- 支持手动清除插件保存的历史记录

修改记录:
- v1.4.2: 增加“清除历史记录”功能，优化初始化逻辑
- v1.4.1: 去除账号密码登录方式，仅保留Cookie登录，简化配置
- v1.4.0: 添加多账户支持，每个账户独立配置和记录
"""
import time
import re
import json
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional, Union

import jwt
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.plugins import _PluginBase
from app.log import logger
from app.schemas import NotificationType
from app.utils.http import RequestUtils

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 动态加载 curl_cffi 以绕过 WAF，如果未安装则回退使用原生 requests
try:
    from curl_cffi import requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests
    HAS_CURL_CFFI = False

class HdhiveSign(_PluginBase):
    # 插件名称
    plugin_name = "影巢签到AI版"
    # 插件描述
    plugin_desc = "自动完成影巢(HDHive)每日签到，支持多账户、失败重试和历史记录"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/madrays/MoviePilot-Plugins/main/icons/hdhive.ico"
    # 插件版本
    plugin_version = "1.4.2"
    # 插件作者
    plugin_author = "madrays"
    # 作者主页
    author_url = "https://github.com/madrays"
    # 插件配置项ID前缀
    plugin_config_prefix = "hdhivesign_"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    _notify = False
    _onlyonce = False
    _clear_history = False # 新增：清除历史标志
    _cron = None
    _max_retries = 3  
    _retry_interval = 30  
    _history_days = 30  
    _manual_trigger = False
    # 账户配置列表
    _accounts = []
    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    _current_trigger_type = None  

    # 影巢站点配置
    _base_url = "https://hdhive.com"
    _site_url = None  
    _signin_api = None  
    _user_info_api = None  

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        logger.info("============= hdhivesign 初始化 =============")
        try:
            if config:
                self._enabled = config.get("enabled")
                self._notify = config.get("notify")
                self._cron = config.get("cron")
                self._onlyonce = config.get("onlyonce")
                self._clear_history = config.get("clear_history") # 读取清除历史开关
                
                # 站点地址配置
                self._base_url = (config.get("base_url") or self._base_url or "").rstrip("/") or "https://hdhive.com"
                self._site_url = f"{self._base_url}/"
                self._signin_api = f"{self._base_url}/api/customer/user/checkin"
                self._user_info_api = f"{self._base_url}/api/customer/user/info"
                self._max_retries = int(config.get("max_retries", 3))
                self._retry_interval = int(config.get("retry_interval", 30))
                self._history_days = int(config.get("history_days", 30))
                
                # 解析账户配置
                self._accounts = []
                accounts_str = config.get("accounts", "")
                if accounts_str:
                    try:
                        accounts_list = json.loads(accounts_str)
                        if isinstance(accounts_list, list):
                            self._accounts = accounts_list
                        else:
                            logger.error("账户配置格式错误")
                    except json.JSONDecodeError as e:
                        logger.error(f"解析账户配置失败: {str(e)}")
                
                # --- 新增：执行历史记录清除逻辑 ---
                if self._clear_history:
                    logger.info("检测到清除历史记录指令，正在清理数据...")
                    # 循环清理所有可能的账户索引（覆盖当前账户数+额外冗余）
                    max_idx = max(len(self._accounts), 10)
                    for i in range(max_idx + 5):
                        self.save_data(f'sign_history_{i}', [])
                        self.save_data(f'consecutive_days_{i}', 0)
                        self.save_data(f'last_success_date_{i}', "")
                        self.save_data(f'hdhive_user_info_{i}', {})
                    
                    logger.info("所有账户的历史数据已重置")
                    # 重置开关状态并更新配置
                    self._clear_history = False
                    config["clear_history"] = False
                    self.update_config(config)

                logger.info(f"影巢插件加载：enabled={self._enabled}, 账户数={len(self._accounts)}")
            
            self._clear_extended_retry_tasks()
            
            if self._onlyonce:
                logger.info("执行一次性签到")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._manual_trigger = True
                self._scheduler.add_job(func=self.sign_all, trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="影巢签到-所有账户")
                self._onlyonce = False
                # 同步更新配置，确保立即运行标志复位
                config["onlyonce"] = False
                self.update_config(config)

                if self._scheduler.get_jobs():
                    self._scheduler.start()

        except Exception as e:
            logger.error(f"hdhivesign初始化错误: {str(e)}", exc_info=True)

    # ... [此处保持 sign_all, sign_account, _parse_cookie 等中间方法不变] ...
    # (为了篇幅，这里略过未变动的方法，实际合并时请保留原代码中这部分内容)

    def sign_all(self):
        """执行所有账户的签到"""
        if not self._accounts:
            logger.warning("没有配置账户，无法执行签到")
            return
        self._current_trigger_type = "手动触发" if self._is_manual_trigger() else "定时触发"
        enabled_accounts = [acc for acc in self._accounts if acc.get("enabled", True)]
        if not enabled_accounts: return
        
        results = []
        for i, account in enumerate(enabled_accounts):
            result = self.sign_account(account, i)
            results.append({"account": account.get("name") or f"账户{i+1}", "result": result})
            if i < len(enabled_accounts) - 1: time.sleep(2)
        
        if self._notify: self._send_summary_notification(results)

    def sign_account(self, account: Dict[str, Any], account_index: int = 0, retry_count: int = 0):
        start_time = datetime.now()
        sign_timeout = 300
        account_name = account.get("name") or f"账户{account_index+1}"
        cookie = account.get("cookie", "")
        if not cookie:
            sign_dict = {"date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'), "status": "签到失败: 未配置Cookie"}
            self._save_sign_history(sign_dict, account_index)
            return sign_dict
        
        log_prefix = f"[{account_name}]"
        try:
            if not self._is_manual_trigger() and self._is_already_signed_today(account_index):
                return {"date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'), "status": "跳过: 今日已签到"}
            
            state, message = self._signin_base(cookie, account_index)
            if state:
                sign_status = "已签到" if ("已经签到" in message or "签到过" in message) else "签到成功"
                today_str = datetime.now().strftime('%Y-%m-%d')
                last_date_str = self.get_data(f'last_success_date_{account_index}')
                consecutive_days = self.get_data(f'consecutive_days_{account_index}', 0)
                if last_date_str != today_str:
                    consecutive_days = (consecutive_days + 1) if last_date_str == (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d') else 1
                self.save_data(f'consecutive_days_{account_index}', consecutive_days)
                self.save_data(f'last_success_date_{account_index}', today_str)

                sign_dict = {"date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'), "status": sign_status, "message": message, "days": consecutive_days, "account": account_name}
                points_match = re.search(r'获得 (\d+) 积分', message)
                sign_dict['points'] = int(points_match.group(1)) if points_match else "—"
                self._save_sign_history(sign_dict, account_index)
                self._send_sign_notification(sign_dict, account_index, account_name)
                return sign_dict
            else:
                if retry_count < self._max_retries:
                    time.sleep(self._retry_interval)
                    return self.sign_account(account, account_index, retry_count + 1)
                sign_dict = {"date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'), "status": f"签到失败: {message}", "account": account_name}
                self._save_sign_history(sign_dict, account_index)
                return sign_dict
        except Exception as e:
            logger.error(f"{log_prefix} 异常: {str(e)}")
            return {"status": "异常"}

    def _parse_cookie(self, cookie_str: str) -> Dict[str, str]:
        cookies = {}
        if cookie_str:
            for cookie_item in cookie_str.split(';'):
                if '=' in cookie_item:
                    name, value = cookie_item.strip().split('=', 1)
                    value = value.strip()
                    if value.startswith('"') and value.endswith('"'): value = value[1:-1]
                    value = urllib.parse.unquote(value)
                    if name == 'token' and value.startswith('Bearer '): value = value[7:].strip()
                    cookies[name] = value
        return cookies

    def _signin_base(self, cookie: str, account_index: int = 0) -> Tuple[bool, str]:
        try:
            cookies = self._parse_cookie(cookie)
            token = cookies.get('token')
            if not token: return False, "Cookie缺少token"
            headers = {'User-Agent': settings.USER_AGENT, 'Authorization': f'Bearer {token}', 'Origin': self._base_url}
            req_kwargs = {"url": self._signin_api, "headers": headers, "cookies": cookies, "proxies": settings.PROXY, "timeout": 30, "verify": False}
            if HAS_CURL_CFFI: req_kwargs["impersonate"] = "chrome"
            signin_res = requests.post(**req_kwargs)
            res_json = signin_res.json()
            msg = res_json.get('message', '')
            if res_json.get('success') or "已经签到" in msg:
                self._fetch_user_info(cookies, token, account_index)
                return True, msg
            return False, msg
        except Exception as e: return False, str(e)

    def _save_sign_history(self, sign_data, account_index: int = 0):
        history = self.get_data(f'sign_history_{account_index}') or []
        history.append(sign_data)
        valid_history = [r for r in history if (datetime.now() - datetime.strptime(r["date"], '%Y-%m-%d %H:%M:%S')).days < self._history_days]
        self.save_data(f'sign_history_{account_index}', valid_history)

    def _fetch_user_info(self, cookies, token, account_index):
        # 原有获取用户信息逻辑...
        pass

    def _send_sign_notification(self, sign_dict, account_index, account_name):
        # 原有通知逻辑...
        pass

    def _send_summary_notification(self, results):
        # 原有汇总通知逻辑...
        pass

    def get_state(self) -> bool: return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{"id": "hdhivesign", "name": "影巢签到", "trigger": CronTrigger.from_crontab(self._cron), "func": self.sign_all}]
        return []

    # --- 修改后的表单：增加了清除历史记录开关 ---
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '开启通知'}}]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch', 
                                        'props': {
                                            'model': 'clear_history', 
                                            'label': '清除历史记录',
                                            'color': 'error'
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
                                'props': {'cols': 12},
                                'content': [{'component': 'VTextarea', 'props': {'model': 'accounts', 'label': '账户配置（JSON格式）', 'rows': 5}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [{'component': 'VTextField', 'props': {'model': 'base_url', 'label': '站点地址'}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VCronField', 'props': {'model': 'cron', 'label': '签到周期'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'max_retries', 'label': '最大重试次数', 'type': 'number'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'retry_interval', 'label': '重试间隔(秒)', 'type': 'number'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'history_days', 'label': '历史保留天数', 'type': 'number'}}]}
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "clear_history": False, # 默认关闭
            "accounts": "",
            "base_url": "https://hdhive.com",
            "cron": "0 8 * * *",
            "max_retries": 3,
            "retry_interval": 30,
            "history_days": 30,
        }

    # ... [此处保持 get_page, stop_service 等剩余方法不变] ...
    # (篇幅原因略过，保持原样即可)
    def get_page(self) -> List[dict]:
        # 原有页面展示逻辑...
        if not self._accounts:
            return [{'component': 'VAlert', 'props': {'type': 'info', 'text': '暂无账户配置'}}]
        # ... 原代码逻辑 ...
        return []

    def stop_service(self):
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running: self._scheduler.shutdown()
            self._scheduler = None

    def _is_manual_trigger(self) -> bool: return getattr(self, '_manual_trigger', False)
    def _clear_extended_retry_tasks(self): pass
    def _is_already_signed_today(self, account_index: int = 0) -> bool:
        history = self.get_data(f'sign_history_{account_index}') or []
        today = datetime.now().strftime('%Y-%m-%d')
        return any(r.get("date", "").startswith(today) and r.get("status") in ["签到成功", "已签到"] for r in history)
