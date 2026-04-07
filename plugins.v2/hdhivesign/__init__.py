"""
影巢签到插件
版本: 1.4.3
作者: madrays
功能:
- 自动完成影巢(HDHive)每日签到
- 支持多账户、失败重试、历史记录展示
- 修复详情页显示及基类抽象方法报错
- 增加“删除历史记录”功能
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

# 动态加载 curl_cffi 以绕过 WAF
try:
    from curl_cffi import requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests
    HAS_CURL_CFFI = False

class HdhiveSign(_PluginBase):
    # 插件基本信息
    plugin_name = "影巢签到AI版"
    plugin_desc = "自动完成影巢(HDHive)每日签到，支持多账户、失败重试、历史记录展示及数据清理"
    plugin_icon = "https://raw.githubusercontent.com/madrays/MoviePilot-Plugins/main/icons/hdhive.ico"
    plugin_version = "1.4.3"
    plugin_author = "madrays"
    author_url = "https://github.com/madrays"
    plugin_config_prefix = "hdhivesign_"
    plugin_order = 1
    auth_level = 2

    # 私有属性
    _enabled = False
    _notify = False
    _onlyonce = False
    _clear_history = False
    _cron = None
    _max_retries = 3  
    _retry_interval = 30  
    _history_days = 30  
    _manual_trigger = False
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

                # 执行历史记录清除
                if self._clear_history:
                    logger.info("【影巢签到】正在执行数据清理...")
                    for i in range(max(len(self._accounts), 20)):
                        self.save_data(f'sign_history_{i}', [])
                        self.save_data(f'consecutive_days_{i}', 0)
                        self.save_data(f'last_success_date_{i}', "")
                        self.save_data(f'hdhive_user_info_{i}', {})
                    
                    config["clear_history"] = False
                    self.update_config(config)
                    logger.info("【影巢签到】所有账户历史数据已重置")

            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._manual_trigger = True
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
                    {'component': 'VRow', 'content': [{'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextarea', 'props': {'model': 'accounts', 'label': '账户配置(JSON)', 'rows': 4}}]}]},
                    {'component': 'VRow', 'content': [{'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextField', 'props': {'model': 'base_url', 'label': '站点地址'}}]}]},
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VCronField', 'props': {'model': 'cron', 'label': '签到周期'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'max_retries', 'label': '重试次数', 'type': 'number'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'retry_interval', 'label': '重试间隔(秒)', 'type': 'number'}}]},
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
        pages = []
        if not self._accounts:
            return [{'component': 'VAlert', 'props': {'type': 'info', 'text': '请先配置账户'}}]
        
        for i, account in enumerate(self._accounts):
            name = account.get("name") or f"账户{i+1}"
            history = self.get_data(f'sign_history_{i}') or []
            days = self.get_data(f'consecutive_days_{i}', 0)
            
            pages.append({
                'component': 'VCard',
                'props': {'title': f'{name} (连续: {days}天)', 'variant': 'outlined', 'class': 'mb-4'},
                'content': [
                    {'component': 'VDataTable', 'props': {'headers': [{'title': '时间', 'key': 'date'}, {'title': '状态', 'key': 'status'}, {'title': '详情', 'key': 'message'}], 'items': history[::-1], 'density': 'compact'}}
                ]
            })
        return pages

    def sign_all(self):
        if not self._accounts: return
        enabled_accounts = [acc for acc in self._accounts if acc.get("enabled", True)]
        for i, account in enumerate(enabled_accounts):
            self.sign_account(account, i)
            if i < len(enabled_accounts) - 1: time.sleep(2)

    def sign_account(self, account: Dict[str, Any], account_index: int = 0, retry_count: int = 0):
        name = account.get("name") or f"账户{account_index+1}"
        cookie = account.get("cookie", "")
        if not cookie: return {"status": "未配置Cookie"}

        if not self._is_manual_trigger() and self._is_already_signed_today(account_index):
            return {"status": "跳过: 今日已签到"}

        logger.info(f"【{name}】开始签到...")
        try:
            state, message = self._signin_base(cookie, account_index)
            if state:
                status = "已签到" if "已经签到" in message else "签到成功"
                today = datetime.now().strftime('%Y-%m-%d')
                last_date = self.get_data(f'last_success_date_{account_index}')
                days = self.get_data(f'consecutive_days_{account_index}', 0)
                
                if last_date != today:
                    days = (days + 1) if last_date == (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d') else 1
                
                self.save_data(f'consecutive_days_{account_index}', days)
                self.save_data(f'last_success_date_{account_index}', today)
                
                sign_dict = {"date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "status": status, "message": message}
                self._save_history(sign_dict, account_index)
                return sign_dict
            else:
                if retry_count < self._max_retries:
                    time.sleep(self._retry_interval)
                    return self.sign_account(account, account_index, retry_count + 1)
                res = {"date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "status": "失败", "message": message}
                self._save_history(res, account_index)
                return res
        except Exception as e:
            logger.error(f"【{name}】签到异常: {str(e)}")
            return {"status": "异常"}

    def _signin_base(self, cookie: str, account_index: int) -> Tuple[bool, str]:
        cookies = self._parse_cookie(cookie)
        token = cookies.get('token')
        if not token: return False, "Cookie中缺少token"
        
        headers = {
            'User-Agent': settings.USER_AGENT,
            'Authorization': f'Bearer {token}',
            'Origin': self._base_url,
            'Referer': f'{self._base_url}/'
        }
        
        api_url = f"{self._base_url}/api/customer/user/checkin"
        req_kwargs = {"url": api_url, "headers": headers, "cookies": cookies, "timeout": 30, "verify": False, "proxies": settings.PROXY}
        if HAS_CURL_CFFI: req_kwargs["impersonate"] = "chrome"

        try:
            res = requests.post(**req_kwargs)
            res_json = res.json()
            msg = res_json.get('message', '未知响应')
            return res_json.get('success', False) or "已经签到" in msg, msg
        except Exception as e:
            return False, str(e)

    def _parse_cookie(self, cookie_str: str) -> Dict[str, str]:
        cookies = {}
        for item in cookie_str.split(';'):
            if '=' in item:
                k, v = item.strip().split('=', 1)
                cookies[k] = urllib.parse.unquote(v.strip('"'))
        return cookies

    def _save_history(self, data, index):
        history = self.get_data(f'sign_history_{index}') or []
        history.append(data)
        # 仅保留最近 N 天
        cutoff = datetime.now() - timedelta(days=self._history_days)
        history = [r for r in history if datetime.strptime(r['date'], '%Y-%m-%d %H:%M:%S') > cutoff]
        self.save_data(f'sign_history_{index}', history)

    def _is_manual_trigger(self) -> bool:
        return getattr(self, '_manual_trigger', False)

    def _is_already_signed_today(self, index: int) -> bool:
        history = self.get_data(f'sign_history_{index}') or []
        today = datetime.now().strftime('%Y-%m-%d')
        return any(r.get("date", "").startswith(today) and (r.get("status") in ["签到成功", "已签到"]) for r in history)

    def stop_service(self):
        if self._scheduler:
            try:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running: self._scheduler.shutdown()
            except Exception: pass
            self._scheduler = None

    def get_state(self) -> bool: return self._enabled
    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{"id": "hdhivesign", "name": "影巢签到", "trigger": CronTrigger.from_crontab(self._cron), "func": self.sign_all}]
        return []
