"""
影巢签到插件
版本: 1.4.3
作者: madrays
功能:
- 自动完成影巢(HDHive)每日签到
- 支持多账户同时签到
- 支持签到失败重试
- 保存签到历史记录
- 提供详细的签到通知
- 默认使用代理访问
- 仅支持Cookie登录方式

修改记录:
- v1.4.3: 采用本地模式实现历史记录清理（通过设置开关触发）
- v1.4.2: 增加指定账户签到历史记录清空按钮和对应API（因环境兼容性已在1.4.3移除）
- v1.4.1: 去除账号密码登录方式，仅保留Cookie登录，简化配置
- v1.4.0: 添加多账户支持，每个账户独立配置和记录
- v1.3.0: 域名改为可配置，统一API拼接(Referer/Origin/接口)，精简日志
- v1.2.0: 添加自动登录功能（已移除）
- v1.1.0: 优化签到逻辑和通知
- v1.0.0: 初始版本，基于影巢网站结构实现自动签到
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
    plugin_version = "1.4.3"
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
    _cron = None
    _max_retries = 3  # 最大重试次数
    _retry_interval = 30  # 重试间隔(秒)
    _history_days = 30  # 历史保留天数
    _manual_trigger = False
    # 账户配置列表
    _accounts = []
    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    _current_trigger_type = None  # 保存当前执行的触发类型

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
                # --- 本地模式清除历史逻辑 ---
                if config.get("clear_history"):
                    logger.info("【影巢签到】检测到清空历史指令，正在清理所有账户数据...")
                    # 清理前10个潜在账户的数据（覆盖绝大多数用户）
                    for i in range(10):
                        self.save_data(f'sign_history_{i}', [])
                        self.save_data(f'consecutive_days_{i}', 0)
                        self.save_data(f'last_success_date_{i}', "")
                        self.save_data(f'hdhive_user_info_{i}', {})
                    
                    # 自动关闭开关防止下次重复清理
                    config['clear_history'] = False
                    self.update_config(config)
                    logger.info("【影巢签到】历史记录已清空，开关已重置。")

                self._enabled = config.get("enabled")
                self._notify = config.get("notify")
                self._cron = config.get("cron")
                self._onlyonce = config.get("onlyonce")
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
                            logger.error("账户配置格式错误，应为JSON数组")
                    except json.JSONDecodeError as e:
                        logger.error(f"解析账户配置失败: {str(e)}")
                
                logger.info(f"影巢签到插件已加载，配置：enabled={self._enabled}, 账户数={len(self._accounts)}")
            
            self._clear_extended_retry_tasks()
            
            if self._onlyonce:
                logger.info("执行一次性签到")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._manual_trigger = True
                self._scheduler.add_job(func=self.sign_all, trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="影巢签到-所有账户")
                self._onlyonce = False
                config['onlyonce'] = False
                self.update_config(config)

                if self._scheduler.get_jobs():
                    self._scheduler.start()

        except Exception as e:
            logger.error(f"hdhivesign初始化错误: {str(e)}", exc_info=True)

    def sign_all(self):
        if not self._accounts:
            logger.warning("没有配置账户，无法执行签到")
            return
        
        self._current_trigger_type = "手动触发" if self._is_manual_trigger() else "定时触发"
        enabled_accounts = [acc for acc in self._accounts if acc.get("enabled", True)]
        
        results = []
        for i, account in enumerate(enabled_accounts):
            account_name = account.get("name") or f"账户{i+1}"
            result = self.sign_account(account, i)
            results.append({"account": account_name, "result": result})
            if i < len(enabled_accounts) - 1:
                time.sleep(2)
        
        if self._notify:
            self._send_summary_notification(results)

    def sign_account(self, account: Dict[str, Any], account_index: int = 0, retry_count: int = 0):
        start_time = datetime.now()
        account_name = account.get("name") or f"账户{account_index+1}"
        cookie = account.get("cookie", "")
        
        if not cookie:
            return {"date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'), "status": "签到失败: 未配置Cookie"}
        
        try:
            if not self._is_manual_trigger() and self._is_already_signed_today(account_index):
                return {"date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'), "status": "跳过: 今日已签到"}
            
            # 执行签到
            state, message = self._signin_base(cookie, account_index)
            
            if state:
                sign_status = "已签到" if ("已经签到" in message or "签到过" in message) else "签到成功"
                today_str = datetime.now().strftime('%Y-%m-%d')
                last_date_str = self.get_data(f'last_success_date_{account_index}')
                consecutive_days = self.get_data(f'consecutive_days_{account_index}', 0)

                if last_date_str != today_str:
                    if last_date_str == (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d'):
                        consecutive_days += 1
                    else:
                        consecutive_days = 1
                
                self.save_data(f'consecutive_days_{account_index}', consecutive_days)
                self.save_data(f'last_success_date_{account_index}', today_str)

                sign_dict = {
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": sign_status,
                    "message": message,
                    "days": consecutive_days,
                    "account": account_name
                }
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
            logger.error(f"签到异常: {str(e)}")
            return {"date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'), "status": "签到异常"}

    def _parse_cookie(self, cookie_str: str) -> Dict[str, str]:
        cookies = {}
        if cookie_str:
            for cookie_item in cookie_str.split(';'):
                if '=' in cookie_item:
                    name, value = cookie_item.strip().split('=', 1)
                    value = urllib.parse.unquote(value.strip().strip('"'))
                    if name == 'token' and value.startswith('Bearer '):
                        value = value[7:].strip()
                    cookies[name] = value
        return cookies

    def _signin_base(self, cookie: str, account_index: int = 0) -> Tuple[bool, str]:
        try:
            cookies = self._parse_cookie(cookie)
            token = cookies.get('token')
            if not token: return False, "Cookie缺少token"
            
            headers = {
                'User-Agent': settings.USER_AGENT,
                'Origin': self._base_url,
                'Authorization': f'Bearer {token}',
            }
            if cookies.get('csrf_access_token'):
                headers['x-csrf-token'] = cookies.get('csrf_access_token')

            req_kwargs = {"url": self._signin_api, "headers": headers, "cookies": cookies, "proxies": settings.PROXY, "timeout": 30, "verify": False}
            if HAS_CURL_CFFI: req_kwargs["impersonate"] = "chrome"

            signin_res = requests.post(**req_kwargs)
            res_json = signin_res.json()
            message = res_json.get('message', '无消息')
            
            if res_json.get('success') or "已经签到" in message or "签到过" in message:
                self._fetch_user_info(cookies, token, account_index)
                return True, message
            return False, message
        except Exception as e:
            return False, str(e)

    def _save_sign_history(self, sign_data, account_index: int = 0):
        history_key = f'sign_history_{account_index}'
        history = self.get_data(history_key) or []
        history.append(sign_data)
        # 仅保留最近的天数记录
        now = datetime.now()
        history = [r for r in history if (now - datetime.strptime(r["date"], '%Y-%m-%d %H:%M:%S')).days < self._history_days]
        self.save_data(key=history_key, value=history)

    def _fetch_user_info(self, cookies: Dict[str, str], token: str, account_index: int = 0):
        try:
            headers = {'User-Agent': settings.USER_AGENT, 'Authorization': f'Bearer {token}'}
            req_kwargs = {"url": self._user_info_api, "headers": headers, "cookies": cookies, "proxies": settings.PROXY, "timeout": 30, "verify": False}
            if HAS_CURL_CFFI: req_kwargs["impersonate"] = "chrome"
            resp = requests.get(**req_kwargs)
            data = resp.json()
            detail = (data.get('response') or {}).get('data') or data.get('detail') or data.get('data') or {}
            info = {
                'nickname': detail.get('nickname'),
                'avatar_url': detail.get('avatar_url'),
                'points': (detail.get('user_meta') or {}).get('points'),
                'signin_days_total': (detail.get('user_meta') or {}).get('signin_days_total'),
                'created_at': detail.get('created_at'),
            }
            self.save_data(f'hdhive_user_info_{account_index}', info)
        except: pass

    def _send_sign_notification(self, sign_dict, account_index: int = 0, account_name: str = "账户"):
        if not self._notify: return
        status = sign_dict.get("status", "未知")
        text = f"👤 账户：{account_name}\n✨ 状态：{status}\n💬 消息：{sign_dict.get('message', '—')}\n🎁 积分：{sign_dict.get('points', '—')}"
        self.post_message(mtype=NotificationType.SiteMessage, title=f"【影巢签到】{account_name}", text=text)

    def _send_summary_notification(self, results):
        title = f"【影巢签到汇总】"
        text = "\n".join([f"{r['account']}: {r['result'].get('status')}" for r in results])
        self.post_message(mtype=NotificationType.SiteMessage, title=title, text=text)

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{"id": "hdhivesign", "name": "影巢签到", "trigger": CronTrigger.from_crontab(self._cron), "func": self.sign_all}]
        return []

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
                            {
                                'component': 'VCol', 
                                'props': {'cols': 12, 'md': 3}, 
                                'content': [
                                    {
                                        'component': 'VSwitch', 
                                        'props': {
                                            'model': 'clear_history', 
                                            'label': '⚠️ 清空历史记录',
                                            'color': 'error',
                                            'hint': '开启并点击保存后将清除所有签到记录'
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextarea', 'props': {'model': 'accounts', 'label': '账户配置（JSON数组）', 'rows': 5}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'base_url', 'label': '站点地址'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VCronField', 'props': {'model': 'cron', 'label': '签到周期'}}]}
                        ]
                    },
                ]
            }
        ], {
            "enabled": False, "notify": True, "onlyonce": False, "clear_history": False,
            "accounts": "[]", "base_url": "https://hdhive.com", "cron": "0 8 * * *",
            "max_retries": 3, "retry_interval": 30, "history_days": 30,
        }

    def get_page(self) -> List[dict]:
        if not self._accounts:
            return [{'component': 'VAlert', 'props': {'type': 'info', 'text': '暂无账户配置。'}}]
        
        content = []
        for i, account in enumerate(self._accounts):
            account_name = account.get("name") or f"账户{i+1}"
            user = self.get_data(f'hdhive_user_info_{i}') or {}
            historys = self.get_data(f'sign_history_{i}') or []
            
            # 账户简要信息
            content.append({
                'component': 'VCard',
                'props': {'variant': 'outlined', 'class': 'mb-4'},
                'content': [
                    {'component': 'VCardTitle', 'text': f"👤 {account_name} ({user.get('nickname', '未知')})"},
                    {'component': 'VCardText', 'text': f"积分: {user.get('points', '—')} | 连续签到: {self.get_data(f'consecutive_days_{i}') or 0} 天"}
                ]
            })
            
            # 历史表格
            if historys:
                history_rows = []
                for h in sorted(historys, key=lambda x: x.get("date", ""), reverse=True)[:10]:
                    history_rows.append({
                        'component': 'tr',
                        'content': [
                            {'component': 'td', 'text': h.get("date")},
                            {'component': 'td', 'text': h.get("status")},
                            {'component': 'td', 'text': str(h.get("points", "—"))}
                        ]
                    })
                content.append({
                    'component': 'VTable',
                    'props': {'density': 'compact', 'class': 'mb-6'},
                    'content': [
                        {'component': 'thead', 'content': [{'component': 'tr', 'content': [{'component': 'th', 'text': '时间'}, {'component': 'th', 'text': '状态'}, {'component': 'th', 'text': '积分'}]}]},
                        {'component': 'tbody', 'content': history_rows}
                    ]
                })
        return content

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.shutdown()
                self._scheduler = None
        except: pass

    def _is_manual_trigger(self) -> bool:
        return getattr(self, '_manual_trigger', False)

    def _clear_extended_retry_tasks(self):
        try:
            if self._scheduler:
                for job in self._scheduler.get_jobs():
                    if "延长重试" in job.name: self._scheduler.remove_job(job.id)
        except: pass

    def _is_already_signed_today(self, account_index: int = 0) -> bool:
        history = self.get_data(f'sign_history_{account_index}') or []
        today = datetime.now().strftime('%Y-%m-%d')
        return any(r.get("date", "").startswith(today) and "成功" in r.get("status", "") for r in history)
