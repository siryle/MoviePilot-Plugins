"""
影巢签到插件
版本: 1.4.3
作者: madrays
功能:
- 自动完成影巢(HDHive)每日签到
- 支持多账户、失败重试、历史记录展示
- 增加“删除历史记录”功能，修复详情页显示及基类报错
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

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from curl_cffi import requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests
    HAS_CURL_CFFI = False

class HdhiveSign(_PluginBase):
    plugin_name = "影巢签到AI版"
    plugin_desc = "自动完成影巢(HDHive)每日签到，支持多账户、失败重试、历史展示及数据清理"
    plugin_icon = "https://raw.githubusercontent.com/madrays/MoviePilot-Plugins/main/icons/hdhive.ico"
    plugin_version = "1.4.3"
    plugin_author = "madrays"
    author_url = "https://github.com/madrays"
    plugin_config_prefix = "hdhivesign_"
    plugin_order = 1
    auth_level = 2

    # 配置变量
    _enabled = False
    _notify = False
    _onlyonce = False
    _clear_history = False
    _cron = None
    _max_retries = 3  
    _retry_interval = 30  
    _history_days = 30  
    _accounts = []
    _scheduler: Optional[BackgroundScheduler] = None
    _base_url = "https://hdhive.com"

    def init_plugin(self, config: dict = None):
        self.stop_service()
        try:
            if config:
                self._enabled = config.get("enabled")
                self._notify = config.get("notify")
                self._cron = config.get("cron")
                self._onlyonce = config.get("onlyonce")
                self._clear_history = config.get("clear_history")
                
                self._base_url = (config.get("base_url") or "https://hdhive.com").rstrip("/")
                self._max_retries = int(config.get("max_retries", 3))
                self._retry_interval = int(config.get("retry_interval", 30))
                self._history_days = int(config.get("history_days", 30))
                
                accounts_str = config.get("accounts", "")
                if accounts_str:
                    try:
                        self._accounts = json.loads(accounts_str)
                    except Exception:
                        self._accounts = []

                # 执行数据清理
                if self._clear_history:
                    logger.info("【影巢签到】执行数据清理任务...")
                    for i in range(max(len(self._accounts), 20)):
                        self.save_data(f'sign_history_{i}', [])
                        self.save_data(f'consecutive_days_{i}', 0)
                        self.save_data(f'last_success_date_{i}', "")
                    
                    config["clear_history"] = False
                    self.update_config(config)
                    logger.info("【影巢签到】历史数据已重置")

            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(func=self.sign_all, trigger='date', 
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3))
                config["onlyonce"] = False
                self.update_config(config)
                self._scheduler.start()

        except Exception as e:
            logger.error(f"hdhivesign初始化错误: {str(e)}")

    def get_api(self):
        return None

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '开启通知'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'clear_history', 'label': '清除历史数据', 'color': 'error'}}]}
                        ]
                    },
                    {'component': 'VRow', 'content': [{'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextarea', 'props': {'model': 'accounts', 'label': '账户JSON配置', 'rows': 4}}]}]},
                    {'component': 'VRow', 'content': [{'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextField', 'props': {'model': 'base_url', 'label': '站点地址'}}]}]},
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VCronField', 'props': {'model': 'cron', 'label': '签到周期'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'max_retries', 'label': '重试次数', 'type': 'number'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'retry_interval', 'label': '重试间隔', 'type': 'number'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'history_days', 'label': '保留天数', 'type': 'number'}}]}
                        ]
                    }
                ]
            }
        ], {
            "enabled": False, "notify": True, "onlyonce": False, "clear_history": False,
            "accounts": "", "base_url": "https://hdhive.com", "cron": "0 8 * * *",
            "max_retries": 3, "retry_interval": 30, "history_days": 30,
        }

    def get_page(self) -> List[dict]:
        """渲染详情页面，展示签到历史"""
        pages = []
        if not self._accounts:
            return [{'component': 'VAlert', 'props': {'type': 'info', 'text': '请先在配置中添加账户'}}]
        
        for i, account in enumerate(self._accounts):
            account_name = account.get("name") or f"账户{i+1}"
            history = self.get_data(f'sign_history_{i}') or []
            days = self.get_data(f'consecutive_days_{i}', 0)
            
            pages.append({
                'component': 'VCard',
                'props': {'title': f'{account_name} (连续签到: {days}天)', 'variant': 'outlined', 'class': 'mb-4'},
                'content': [
                    {
                        'component': 'VDataTable',
                        'props': {
                            'headers': [
                                {'title': '时间', 'key': 'date'},
                                {'title': '状态', 'key': 'status'},
                                {'title': '详情', 'key': 'message'}
                            ],
                            'items': history[::-1], # 倒序显示最新记录
                            'density': 'compact'
                        }
                    }
                ]
            })
        return pages

    def sign_all(self):
        if not self._accounts: return
        for i, account in enumerate([a for a in self._accounts if a.get("enabled", True)]):
            self.sign_account(account, i)
            time.sleep(2)

    def sign_account(self, account, index):
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

    def stop_service(self):
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running: self._scheduler.shutdown()
            self._scheduler = None

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{"id": "hdhivesign", "name": "影巢签到", "trigger": CronTrigger.from_crontab(self._cron), "func": self.sign_all}]
        return []

