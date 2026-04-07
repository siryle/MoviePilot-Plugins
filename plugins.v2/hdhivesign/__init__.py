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

修改记录:
- v1.4.2: 增加指定账户签到历史记录清空按钮和对应API
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

    # 影巢站点配置（域名可配置）
    _base_url = "https://hdhive.com"
    _site_url = None  # 将在init_plugin中初始化
    _signin_api = None  # 将在init_plugin中初始化
    _user_info_api = None  # 将在init_plugin中初始化

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
                # 站点地址配置
                self._base_url = (config.get("base_url") or self._base_url or "").rstrip("/") or "https://hdhive.com"
                # 基于 base_url 统一构建接口地址
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
                            logger.info(f"加载了 {len(self._accounts)} 个账户配置")
                        else:
                            logger.error("账户配置格式错误，应为JSON数组")
                    except json.JSONDecodeError as e:
                        logger.error(f"解析账户配置失败: {str(e)}")
                else:
                    # 向后兼容：从旧配置中读取单个账户
                    old_cookie = config.get("cookie", "")
                    if old_cookie:
                        account = {
                            "cookie": old_cookie,
                            "enabled": True,
                            "name": f"账户1"
                        }
                        self._accounts.append(account)
                        logger.info("从旧配置创建了单个账户")
                
                logger.info(f"影巢签到插件已加载，配置：enabled={self._enabled}, notify={self._notify}, cron={self._cron}, 账户数={len(self._accounts)}")
            
            # 清理所有可能的延长重试任务
            self._clear_extended_retry_tasks()
            
            if self._onlyonce:
                logger.info("执行一次性签到")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._manual_trigger = True
                self._scheduler.add_job(func=self.sign_all, trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="影巢签到-所有账户")
                self._onlyonce = False
                self.update_config({
                    "onlyonce": False,
                    "enabled": self._enabled,
                    "notify": self._notify,
                    "cron": self._cron,
                    "base_url": self._base_url,
                    "max_retries": self._max_retries,
                    "retry_interval": self._retry_interval,
                    "history_days": self._history_days,
                    "accounts": json.dumps(self._accounts, ensure_ascii=False)
                })

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

        except Exception as e:
            logger.error(f"hdhivesign初始化错误: {str(e)}", exc_info=True)

    def sign_all(self):
        """
        执行所有账户的签到
        """
        if not self._accounts:
            logger.warning("没有配置账户，无法执行签到")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【❌ 影巢签到失败】",
                    text="❌ 没有配置账户，请在插件设置中添加账户"
                )
            return
        
        # 保存当前执行的触发类型
        self._current_trigger_type = "手动触发" if self._is_manual_trigger() else "定时触发"
        
        enabled_accounts = [acc for acc in self._accounts if acc.get("enabled", True)]
        if not enabled_accounts:
            logger.warning("没有启用的账户")
            return
        
        logger.info(f"开始执行 {len(enabled_accounts)} 个账户的签到")
        
        results = []
        for i, account in enumerate(enabled_accounts):
            account_name = account.get("name") or f"账户{i+1}"
            logger.info(f"=== 开始处理账户: {account_name} ===")
            
            # 执行单个账户签到
            result = self.sign_account(account, i)
            results.append({
                "account": account_name,
                "result": result
            })
            
            # 避免请求过于频繁
            if i < len(enabled_accounts) - 1:
                time.sleep(2)
        
        # 发送汇总通知
        if self._notify:
            self._send_summary_notification(results)

    def sign_account(self, account: Dict[str, Any], account_index: int = 0, retry_count: int = 0):
        """
        执行单个账户的签到
        """
        # 设置执行超时保护
        start_time = datetime.now()
        sign_timeout = 300  # 限制签到执行最长时间为5分钟
        
        account_name = account.get("name") or f"账户{account_index+1}"
        cookie = account.get("cookie", "")
        
        # 检查是否配置了Cookie
        if not cookie:
            logger.error(f"[{account_name}] 未配置Cookie，无法执行签到")
            sign_dict = {
                "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                "status": "签到失败: 未配置Cookie",
            }
            self._save_sign_history(sign_dict, account_index)
            
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title=f"【{account_name} 影巢签到失败】",
                    text=f"❌ {account_name}: 未配置Cookie，请在设置中添加Cookie"
                )
            return sign_dict
        
        # 根据重试情况记录日志
        log_prefix = f"[{account_name}]"
        if retry_count > 0:
            logger.debug(f"{log_prefix} 重试: 第{retry_count}次")
        
        notification_sent = False
        sign_dict = None
        sign_status = None
        
        try:
            # 检查今天是否已经签到成功
            if not self._is_manual_trigger() and self._is_already_signed_today(account_index):
                logger.info(f"{log_prefix} 根据历史记录，今日已成功签到，跳过本次执行")
                
                # 创建跳过记录
                sign_dict = {
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": "跳过: 今日已签到",
                }
                
                # 获取最后一次成功签到的记录信息
                history = self.get_data(f'sign_history_{account_index}') or []
                today = datetime.now().strftime('%Y-%m-%d')
                today_success = [
                    record for record in history 
                    if record.get("date", "").startswith(today) 
                    and record.get("status") in ["签到成功", "已签到"]
                ]
                
                # 添加最后成功签到记录的详细信息
                if today_success:
                    last_success = max(today_success, key=lambda x: x.get("date", ""))
                    sign_dict.update({
                        "message": last_success.get("message"),
                        "points": last_success.get("points"),
                        "days": last_success.get("days")
                    })
                
                # 更新用户信息
                try:
                    cookies = self._parse_cookie(cookie)
                    token = cookies.get('token')
                    if token:
                        self._fetch_user_info(cookies, token, account_index)
                except Exception:
                    pass
                
                return sign_dict
            
            logger.info(f"{log_prefix} 执行签到...")

            # 预拉取用户信息
            try:
                cookies = self._parse_cookie(cookie)
                token = cookies.get('token')
                if token:
                    logger.info(f"{log_prefix} 尝试预拉取用户信息用于页面展示")
                    self._fetch_user_info(cookies, token, account_index)
            except Exception:
                pass
            
            # 执行签到
            state, message = self._signin_base(cookie, account_index)
            
            if state:
                logger.debug(f"{log_prefix} 签到API消息: {message}")
                
                if "已经签到" in message or "签到过" in message:
                    sign_status = "已签到"
                else:
                    sign_status = "签到成功"
                
                logger.debug(f"{log_prefix} 签到状态: {sign_status}")

                # 计算连续签到天数
                today_str = datetime.now().strftime('%Y-%m-%d')
                last_date_str = self.get_data(f'last_success_date_{account_index}')
                consecutive_days = self.get_data(f'consecutive_days_{account_index}', 0)

                if last_date_str == today_str:
                    # 当天重复运行，天数不变
                    pass
                elif last_date_str == (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d'):
                    # 连续签到，天数+1
                    consecutive_days += 1
                else:
                    # 签到中断或首次签到，重置为1
                    consecutive_days = 1
                
                # 更新连续签到数据
                self.save_data(f'consecutive_days_{account_index}', consecutive_days)
                self.save_data(f'last_success_date_{account_index}', today_str)

                # 创建签到记录
                sign_dict = {
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": sign_status,
                    "message": message,
                    "days": consecutive_days,
                    "account": account_name
                }
                
                # 解析奖励积分
                points_match = re.search(r'获得 (\d+) 积分', message)
                sign_dict['points'] = int(points_match.group(1)) if points_match else "—"

                self._save_sign_history(sign_dict, account_index)
                self._send_sign_notification(sign_dict, account_index, account_name)
                return sign_dict
            else:
                # 签到失败
                logger.error(f"{log_prefix} 影巢签到失败: {message}")

                # 常规重试逻辑
                if retry_count < self._max_retries:
                    logger.info(f"{log_prefix} 将在{self._retry_interval}秒后进行第{retry_count+1}次重试...")
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title=f"【{account_name} 影巢签到重试】",
                            text=f"❗ {account_name}: 签到失败: {message}，{self._retry_interval}秒后将进行第{retry_count+1}次重试"
                        )
                    time.sleep(self._retry_interval)
                    return self.sign_account(account, account_index, retry_count + 1)
                
                # 所有重试都失败
                sign_dict = {
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": f"签到失败: {message}",
                    "message": message,
                    "account": account_name
                }
                self._save_sign_history(sign_dict, account_index)
                
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title=f"【❌ {account_name} 影巢签到失败】",
                        text=f"❌ {account_name}: 签到失败: {message}，所有重试均已失败"
                    )
                    notification_sent = True
                return sign_dict
        
        except requests.RequestException as req_exc:
            # 网络请求异常处理
            logger.error(f"{log_prefix} 网络请求异常: {str(req_exc)}")
            # 添加执行超时检查
            if (datetime.now() - start_time).total_seconds() > sign_timeout:
                logger.error(f"{log_prefix} 签到执行时间超过5分钟，执行超时")
                sign_dict = {
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": "签到失败: 执行超时",
                    "account": account_name
                }
                self._save_sign_history(sign_dict, account_index)
                
                if self._notify and not notification_sent:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title=f"【❌ {account_name} 影巢签到失败】",
                        text=f"❌ {account_name}: 签到执行超时，已强制终止，请检查网络或站点状态"
                    )
                    notification_sent = True
                
                return sign_dict
        except Exception as e:
            logger.error(f"{log_prefix} 影巢签到异常: {str(e)}", exc_info=True)
            sign_dict = {
                "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                "status": f"签到失败: {str(e)}",
                "account": account_name
            }
            self._save_sign_history(sign_dict, account_index)
            
            if self._notify and not notification_sent:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title=f"【❌ {account_name} 影巢签到失败】",
                    text=f"❌ {account_name}: 签到异常: {str(e)}"
                )
                notification_sent = True
            
            return sign_dict

    def _parse_cookie(self, cookie_str: str) -> Dict[str, str]:
        """
        解析Cookie字符串为字典，清理多余字符和编码以防止后端JWT解析失败
        """
        cookies = {}
        if cookie_str:
            for cookie_item in cookie_str.split(';'):
                if '=' in cookie_item:
                    name, value = cookie_item.strip().split('=', 1)
                    value = value.strip()
                    
                    # 1. 过滤双引号 (复制Cookie时易混入引号导致Token失效)
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                        
                    # 2. URL 解码 (将 %3D 等安全字符还原，防止 JWT Parse 报错)
                    value = urllib.parse.unquote(value)
                    
                    # 3. 过滤可能的 Bearer 前缀
                    if name == 'token' and value.startswith('Bearer '):
                        value = value[7:].strip()
                        
                    cookies[name] = value
        return cookies

    def _signin_base(self, cookie: str, account_index: int = 0) -> Tuple[bool, str]:
        """
        基于影巢API的签到实现
        """
        try:
            cookies = self._parse_cookie(cookie)
            
            if not cookie:
                return False, "未配置Cookie"

            token = cookies.get('token')
            csrf_token = cookies.get('csrf_access_token')

            if not token:
                return False, "Cookie中缺少'token'"

            user_id = None
            referer = self._site_url
            try:
                decoded_token = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
                user_id = decoded_token.get('sub')
                if user_id:
                    referer = f"{self._base_url}/user/{user_id}"
            except Exception as e:
                logger.warning(f"从Token中解析用户ID失败，将使用默认Referer: {e}")

            proxies = settings.PROXY
            ua = settings.USER_AGENT

            headers = {
                'User-Agent': ua,
                'Accept': 'application/json, text/plain, */*',
                'Origin': self._base_url,
                'Referer': referer,
                'Authorization': f'Bearer {token}',
            }
            if csrf_token:
                headers['x-csrf-token'] = csrf_token

            # 构建请求参数，如果环境支持 curl_cffi 则模拟浏览器指纹防止 WAF 拦截
            req_kwargs = {
                "url": self._signin_api,
                "headers": headers,
                "cookies": cookies,
                "proxies": proxies,
                "timeout": 30,
                "verify": False
            }
            if HAS_CURL_CFFI:
                req_kwargs["impersonate"] = "chrome"

            signin_res = requests.post(**req_kwargs)

            if signin_res is None:
                return False, '签到请求失败，响应为空，请检查代理或网络环境'

            try:
                signin_result = signin_res.json()
            except json.JSONDecodeError:
                logger.error(f"API响应JSON解析失败 (状态码 {signin_res.status_code}): {signin_res.text[:500]}")
                return False, f'签到API响应格式错误，状态码: {signin_res.status_code}'

            message = signin_result.get('message', '无明确消息')
            
            if signin_result.get('success'):
                try:
                    self._fetch_user_info(cookies, token, account_index)
                except Exception:
                    pass
                return True, message

            if "已经签到" in message or "签到过" in message:
                try:
                    self._fetch_user_info(cookies, token, account_index)
                except Exception:
                    pass
                return True, message 

            logger.error(f"签到失败, HTTP状态码: {signin_res.status_code}, 消息: {message}")
            return False, message

        except Exception as e:
            logger.error(f"签到流程发生未知异常", exc_info=True)
            return False, f'签到异常: {str(e)}'

    def _save_sign_history(self, sign_data, account_index: int = 0):
        """
        保存签到历史记录
        """
        try:
            # 读取现有历史
            history_key = f'sign_history_{account_index}'
            history = self.get_data(history_key) or []

            # 确保日期格式正确
            if "date" not in sign_data:
                sign_data["date"] = datetime.today().strftime('%Y-%m-%d %H:%M:%S')

            history.append(sign_data)

            # 清理旧记录
            retention_days = int(self._history_days)
            now = datetime.now()
            valid_history = []

            for record in history:
                try:
                    # 尝试将记录日期转换为datetime对象
                    record_date = datetime.strptime(record["date"], '%Y-%m-%d %H:%M:%S')
                    # 检查是否在保留期内
                    if (now - record_date).days < retention_days:
                        valid_history.append(record)
                except (ValueError, KeyError):
                    # 如果记录日期格式不正确，尝试修复
                    logger.warning(f"历史记录日期格式无效: {record.get('date', '无日期')}")
                    # 添加新的日期并保留记录
                    record["date"] = datetime.today().strftime('%Y-%m-%d %H:%M:%S')
                    valid_history.append(record)

            # 保存历史
            self.save_data(key=history_key, value=valid_history)
            logger.info(f"保存账户{account_index+1}的签到历史记录，当前共有 {len(valid_history)} 条记录")

        except Exception as e:
            logger.error(f"保存签到历史记录失败: {str(e)}", exc_info=True)

    def _fetch_user_info(self, cookies: Dict[str, str], token: str, account_index: int = 0) -> Optional[dict]:
        try:
            referer = self._site_url
            try:
                decoded_token = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
                user_id = decoded_token.get('sub')
                if user_id:
                    referer = f"{self._base_url}/user/{user_id}"
            except Exception:
                pass
            headers = {
                'User-Agent': settings.USER_AGENT,
                'Accept': 'application/json, text/plain, */*',
                'Origin': self._base_url,
                'Referer': referer,
                'Authorization': f'Bearer {token}',
            }
            
            req_kwargs = {
                "url": self._user_info_api,
                "headers": headers,
                "cookies": cookies,
                "proxies": settings.PROXY,
                "timeout": 30,
                "verify": False
            }
            if HAS_CURL_CFFI:
                req_kwargs["impersonate"] = "chrome"

            resp = requests.get(**req_kwargs)
            logger.info(f"拉取用户信息 API 状态码: {getattr(resp,'status_code','unknown')}")
            data = {}
            try:
                data = resp.json()
            except Exception:
                data = {}
            # 统一解析 response.data / detail / data 结构
            detail = (data.get('response') or {}).get('data') or data.get('detail') or data.get('data') or {}
            if not isinstance(detail, dict):
                detail = {}
            info = {
                'id': detail.get('id') or detail.get('member_id'),
                'nickname': detail.get('nickname') or detail.get('member_name'),
                'avatar_url': detail.get('avatar_url') or detail.get('gravatar_url'),
                'created_at': detail.get('created_at'),
                'points': ((detail.get('user_meta') or {}).get('points')),
                'signin_days_total': ((detail.get('user_meta') or {}).get('signin_days_total')),
                'warnings_nums': detail.get('warnings_nums'),
            }
            # 若 API 未返回完整信息，尝试 RSC 页面解析
            if not info.get('nickname') or info.get('points') is None or info.get('signin_days_total') is None:
                try:
                    rsc_headers = {
                        'User-Agent': settings.USER_AGENT,
                        'Accept': 'text/x-component',
                        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                        'Origin': self._base_url,
                        'Referer': referer,
                        'rsc': '1',
                    }
                    rsc_url = referer
                    
                    rsc_kwargs = {
                        "url": rsc_url,
                        "headers": rsc_headers,
                        "cookies": cookies,
                        "proxies": settings.PROXY,
                        "timeout": 30,
                        "verify": False
                    }
                    if HAS_CURL_CFFI:
                        rsc_kwargs["impersonate"] = "chrome"

                    rsc_resp = requests.get(**rsc_kwargs)
                    logger.info(f"RSC 用户页状态码: {getattr(rsc_resp,'status_code','unknown')}")
                    rsc_text = rsc_resp.text or ''
                    
                    m_nick = re.search(r'"nickname":"([^"]+)"', rsc_text)
                    m_points = re.search(r'"points":(\d+)', rsc_text)
                    m_days = re.search(r'"signin_days_total":(\d+)', rsc_text)
                    m_avatar = re.search(r'"avatar_url":"([^"]+)"', rsc_text)
                    m_created = re.search(r'"created_at":"([^"]+)"', rsc_text)
                    if m_nick:
                        info['nickname'] = m_nick.group(1)
                    if m_points:
                        info['points'] = int(m_points.group(1))
                    if m_days:
                        info['signin_days_total'] = int(m_days.group(1))
                    if m_avatar:
                        info['avatar_url'] = m_avatar.group(1)
                    if m_created:
                        info['created_at'] = m_created.group(1)
                    if (not info.get('nickname') or info.get('points') is None or info.get('signin_days_total') is None) and '"user":' in rsc_text:
                        user_json = self._extract_rsc_object(rsc_text, 'user')
                        if user_json:
                            try:
                                obj = json.loads(user_json)
                                info['id'] = obj.get('id') or info.get('id')
                                info['nickname'] = obj.get('nickname') or info.get('nickname')
                                info['avatar_url'] = obj.get('avatar_url') or info.get('avatar_url')
                                info['created_at'] = obj.get('created_at') or info.get('created_at')
                                meta = obj.get('user_meta') or {}
                                if isinstance(meta, dict):
                                    if meta.get('points') is not None:
                                        info['points'] = meta.get('points')
                                    if meta.get('signin_days_total') is not None:
                                        info['signin_days_total'] = meta.get('signin_days_total')
                            except Exception:
                                pass
                except Exception:
                    pass
            self.save_data(f'hdhive_user_info_{account_index}', info)
            return info
        except Exception as e:
            logger.warning(f"获取用户信息失败: {e}")
            return None

    def _extract_rsc_object(self, text: str, key: str) -> Optional[str]:
        try:
            marker = f'"{key}":'
            idx = text.find(marker)
            if idx == -1:
                return None
            brace_idx = text.find('{', idx + len(marker))
            if brace_idx == -1:
                return None
            depth = 0
            i = brace_idx
            in_str = False
            prev = ''
            while i < len(text):
                ch = text[i]
                if ch == '"' and prev != '\\':
                    in_str = not in_str
                if not in_str:
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            segment = text[brace_idx:i+1]
                            return segment
                prev = ch
                i += 1
            return None
        except Exception:
            return None

    def _send_sign_notification(self, sign_dict, account_index: int = 0, account_name: str = "账户"):
        """
        发送签到通知
        """
        if not self._notify:
            return

        status = sign_dict.get("status", "未知")
        message = sign_dict.get("message", "—")
        points = sign_dict.get("points", "—")
        days = sign_dict.get("days", "—")
        sign_time = sign_dict.get("date", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        user = self.get_data(f'hdhive_user_info_{account_index}') or {}
        nickname = user.get('nickname') or '—'
        user_points = user.get('points') if user.get('points') is not None else '—'
        signin_days_total = user.get('signin_days_total') if user.get('signin_days_total') is not None else '—'
        created_at = user.get('created_at') or '—'

        # 检查奖励信息是否为空
        info_missing = message == "—" and points == "—" and days == "—"

        # 获取触发方式
        trigger_type = self._current_trigger_type

        # 构建通知文本
        if "签到成功" in status:
            title = f"【✅ {account_name} 影巢签到成功】"

            if info_missing:
                text = (
                    f"📢 执行结果\n"
                    f"━━━━━━━━━━\n"
                    f"👤 账户：{account_name}\n"
                    f"🕐 时间：{sign_time}\n"
                    f"📍 方式：{trigger_type}\n"
                    f"✨ 状态：{status}\n"
                    f"⚠️ 详细信息获取失败，请手动查看\n"
                    f"━━━━━━━━━━\n"
                    f"👤 用户信息\n"
                    f"昵称：{nickname}\n"
                    f"积分：{user_points}\n"
                    f"累计签到天数（站点）：{signin_days_total}\n"
                    f"加入时间：{created_at}\n"
                    f"━━━━━━━━━━"
                )
            else:
                text = (
                    f"📢 执行结果\n"
                    f"━━━━━━━━━━\n"
                    f"👤 账户：{account_name}\n"
                    f"🕐 时间：{sign_time}\n"
                    f"📍 方式：{trigger_type}\n"
                    f"✨ 状态：{status}\n"
                    f"━━━━━━━━━━\n"
                    f"📊 签到信息\n"
                    f"💬 消息：{message}\n"
                    f"🎁 奖励：{points}\n"
                    f"📆 天数：{days}\n"
                    f"━━━━━━━━━━\n"
                    f"👤 用户信息\n"
                    f"昵称：{nickname}\n"
                    f"积分：{user_points}\n"
                    f"累计签到天数（站点）：{signin_days_total}\n"
                    f"加入时间：{created_at}\n"
                    f"━━━━━━━━━━"
                )
        elif "已签到" in status:
            title = f"【ℹ️ {account_name} 影巢重复签到】"

            if info_missing:
                text = (
                    f"📢 执行结果\n"
                    f"━━━━━━━━━━\n"
                    f"👤 账户：{account_name}\n"
                    f"🕐 时间：{sign_time}\n"
                    f"📍 方式：{trigger_type}\n"
                    f"✨ 状态：{status}\n"
                    f"ℹ️ 说明：今日已完成签到\n"
                    f"⚠️ 详细信息获取失败，请手动查看\n"
                    f"━━━━━━━━━━\n"
                    f"👤 用户信息\n"
                    f"昵称：{nickname}\n"
                    f"积分：{user_points}\n"
                    f"累计签到天数（站点）：{signin_days_total}\n"
                    f"加入时间：{created_at}\n"
                    f"━━━━━━━━━━"
                )
            else:
                text = (
                    f"📢 执行结果\n"
                    f"━━━━━━━━━━\n"
                    f"👤 账户：{account_name}\n"
                    f"🕐 时间：{sign_time}\n"
                    f"📍 方式：{trigger_type}\n"
                    f"✨ 状态：{status}\n"
                    f"ℹ️ 说明：今日已完成签到\n"
                    f"━━━━━━━━━━\n"
                    f"📊 签到信息\n"
                    f"💬 消息：{message}\n"
                    f"🎁 奖励：{points}\n"
                    f"📆 天数：{days}\n"
                    f"━━━━━━━━━━\n"
                    f"👤 用户信息\n"
                    f"昵称：{nickname}\n"
                    f"积分：{user_points}\n"
                    f"累计签到天数（站点）：{signin_days_total}\n"
                    f"加入时间：{created_at}\n"
                    f"━━━━━━━━━━"
                )
        else:
            title = f"【❌ {account_name} 影巢签到失败】"
            text = (
                f"📢 执行结果\n"
                f"━━━━━━━━━━\n"
                f"👤 账户：{account_name}\n"
                f"🕐 时间：{sign_time}\n"
                f"📍 方式：{trigger_type}\n"
                f"❌ 状态：{status}\n"
                f"━━━━━━━━━━\n"
                f"💡 可能的解决方法\n"
                f"• 检查Cookie是否有效\n"
                f"• 确认代理连接正常\n"
                f"• 查看站点是否正常访问\n"
                f"━━━━━━━━━━"
            )

        # 发送通知
        self.post_message(
            mtype=NotificationType.SiteMessage,
            title=title,
            text=text
        )

    def _send_summary_notification(self, results: List[Dict[str, Any]]):
        """
        发送多账户签到汇总通知
        """
        if not self._notify:
            return
        
        success_count = 0
        already_count = 0
        failed_count = 0
        
        details = []
        
        for result in results:
            account_name = result["account"]
            sign_dict = result["result"]
            
            if sign_dict:
                status = sign_dict.get("status", "")
                if "成功" in status:
                    success_count += 1
                    emoji = "✅"
                elif "已签到" in status or "跳过" in status:
                    already_count += 1
                    emoji = "ℹ️"
                else:
                    failed_count += 1
                    emoji = "❌"
                
                details.append(f"{emoji} {account_name}: {status}")
            else:
                failed_count += 1
                details.append(f"❌ {account_name}: 签到失败")
        
        total = len(results)
        title = f"【影巢签到汇总】成功:{success_count} 已签:{already_count} 失败:{failed_count}"
        
        text = (
            f"📊 影巢多账户签到汇总\n"
            f"━━━━━━━━━━\n"
            f"🕐 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"📍 方式：{self._current_trigger_type}\n"
            f"━━━━━━━━━━\n"
            f"📈 统计结果\n"
            f"✅ 成功签到：{success_count}/{total}\n"
            f"ℹ️ 今日已签：{already_count}/{total}\n"
            f"❌ 签到失败：{failed_count}/{total}\n"
            f"━━━━━━━━━━\n"
            f"📋 详细结果\n"
        )
        
        for detail in details:
            text += f"{detail}\n"
        
        text += f"━━━━━━━━━━"
        
        self.post_message(
            mtype=NotificationType.SiteMessage,
            title=title,
            text=text
        )

    def get_state(self) -> bool:
        logger.info(f"hdhivesign状态: {self._enabled}")
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            logger.info(f"注册定时服务: {self._cron}")
            return [{
                "id": "hdhivesign",
                "name": "影巢签到",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sign_all,
                "kwargs": {}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        返回插件配置的表单
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
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
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '开启通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
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
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'accounts',
                                            'label': '账户配置（JSON格式）',
                                            'placeholder': '请按JSON数组格式填写多个账户配置',
                                            'rows': 5,
                                            'hint': '每个账户必须包含: cookie和name，可设置enabled(是否启用)'
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
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '账户配置JSON示例：\n'
                                                    '[\n'
                                                    '  {\n'
                                                    '    "name": "主账户",\n'
                                                    '    "cookie": "token=xxx; csrf_access_token=yyy",\n'
                                                    '    "enabled": true\n'
                                                    '  },\n'
                                                    '  {\n'
                                                    '    "name": "备用账户",\n'
                                                    '    "cookie": "token=aaa; csrf_access_token=bbb",\n'
                                                    '    "enabled": true\n'
                                                    '  }\n'
                                                    ']\n'
                                                    '⚠️ 注意：本插件仅支持Cookie登录方式，必须提供有效的Cookie。'
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
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'base_url',
                                            'label': '站点地址',
                                            'placeholder': '例如：https://hdhive.online 或新域名',
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
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '签到周期'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'max_retries',
                                            'label': '最大重试次数',
                                            'type': 'number',
                                            'placeholder': '3'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'retry_interval',
                                            'label': '重试间隔(秒)',
                                            'type': 'number',
                                            'placeholder': '30'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'history_days',
                                            'label': '历史保留天数',
                                            'type': 'number',
                                            'placeholder': '30'
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
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'text': '【重要提示】\n'
                                                    '1. 本插件已移除自动登录功能，仅支持Cookie登录方式。\n'
                                                    '2. 获取Cookie：登录影巢站点，按F12打开开发者工具。\n'
                                                    '3. 切换到"应用(Application)" -> "Cookie"，或"网络(Network)"选项卡。\n'
                                                    '4. 找到发往API的请求，复制完整的Cookie字符串。\n'
                                                    '5. 确保Cookie中包含 `token` 字段，建议同时包含 `csrf_access_token`。\n'
                                                    '6. 粘贴到账户配置中，启用插件并保存。\n\n'
                                                    '⚠️ Cookie失效后需要手动更新，插件不会自动刷新Cookie。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "accounts": "",
            "base_url": "https://hdhive.com",
            "cron": "0 8 * * *",
            "max_retries": 3,
            "retry_interval": 30,
            "history_days": 30,
        }

    def get_page(self) -> List[dict]:
        """
        构建插件详情页面，展示所有账户的签到历史
        """
        if not self._accounts:
            return [{
                'component': 'VAlert',
                'props': {
                    'type': 'info', 'variant': 'tonal',
                    'text': '暂无账户配置，请在插件设置中添加账户。',
                    'class': 'mb-2'
                }
            }]
        
        content = []
        
        for i, account in enumerate(self._accounts):
            account_name = account.get("name") or f"账户{i+1}"
            enabled = account.get("enabled", True)
            
            # 获取用户信息
            user = self.get_data(f'hdhive_user_info_{i}') or {}
            consecutive_days = self.get_data(f'consecutive_days_{i}') or 0
            
            # 账户信息卡片
            info_card = []
            if user:
                avatar = user.get('avatar_url') or ''
                nickname = user.get('nickname') or '—'
                points = user.get('points') if user.get('points') is not None else '—'
                signin_days_total = user.get('signin_days_total') if user.get('signin_days_total') is not None else '—'
                created_at = user.get('created_at') or '—'
                
                status_color = "success" if enabled else "error"
                status_text = "启用" if enabled else "禁用"
                
                # 检查Cookie是否配置
                has_cookie = bool(account.get("cookie"))
                cookie_status_color = "success" if has_cookie else "error"
                cookie_status_text = "已配置" if has_cookie else "未配置"
                
                info_card = [{
                    'component': 'VCard',
                    'props': {'variant': 'outlined', 'class': 'mb-4'},
                    'content': [
                        {
                            'component': 'VCardTitle',
                            'props': {'class': 'd-flex align-center justify-space-between'},
                            'content': [
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'span',
                                            'props': {'class': 'text-h6'},
                                            'text': f'👤 {account_name}'
                                        },
                                        {
                                            'component': 'div',
                                            'props': {'class': 'text-caption mt-1'},
                                            'content': [
                                                {
                                                    'component': 'VChip',
                                                    'props': {
                                                        'color': status_color,
                                                        'size': 'small',
                                                        'class': 'mr-2'
                                                    },
                                                    'text': status_text
                                                },
                                                {
                                                    'component': 'VChip',
                                                    'props': {
                                                        'color': cookie_status_color,
                                                        'size': 'small',
                                                        'class': 'mr-2'
                                                    },
                                                    'text': f'Cookie: {cookie_status_text}'
                                                },
                                                f'加入时间：{created_at}'
                                            ]
                                        }
                                    ]
                                },
                                {
                                    'component': 'VAvatar',
                                    'props': {'size': 64},
                                    'content': [
                                        {
                                            'component': 'img',
                                            'props': {
                                                'src': avatar,
                                                'alt': nickname,
                                                'style': 'object-fit: cover;'
                                            }
                                        }
                                    ]
                                }
                            ]
                        },
                        {'component': 'VDivider'},
                        {
                            'component': 'VCardText',
                            'content': [
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {
                                            'component': 'VCol',
                                            'props': {'cols': 12, 'md': 3},
                                            'content': [
                                                {
                                                    'component': 'VChip',
                                                    'props': {
                                                        'variant': 'elevated',
                                                        'color': 'primary',
                                                        'class': 'mb-2'
                                                    },
                                                    'text': f'用户：{nickname}'
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'VCol',
                                            'props': {'cols': 12, 'md': 3},
                                            'content': [
                                                {
                                                    'component': 'VChip',
                                                    'props': {
                                                        'variant': 'elevated',
                                                        'color': 'amber-darken-2',
                                                        'class': 'mb-2'
                                                    },
                                                    'text': f'积分：{points}'
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'VCol',
                                            'props': {'cols': 12, 'md': 3},
                                            'content': [
                                                {
                                                    'component': 'VChip',
                                                    'props': {
                                                        'variant': 'elevated',
                                                        'color': 'success',
                                                        'class': 'mb-2'
                                                    },
                                                    'text': f'累计签到（站点）：{signin_days_total}'
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'VCol',
                                            'props': {'cols': 12, 'md': 3},
                                            'content': [
                                                {
                                                    'component': 'VChip',
                                                    'props': {
                                                        'variant': 'elevated',
                                                        'color': 'cyan-darken-2',
                                                        'class': 'mb-2'
                                                    },
                                                    'text': f'连续签到（插件）：{consecutive_days}'
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }]
            
            # 签到历史表格
            historys = self.get_data(f'sign_history_{i}') or []
            if historys:
                historys = sorted(historys, key=lambda x: x.get("date", ""), reverse=True)
                
                history_rows = []
                for history in historys[:20]:  # 只显示最近20条记录
                    status = history.get("status", "未知")
                    if "成功" in status or "已签到" in status:
                        status_color = "success"
                    elif "失败" in status:
                        status_color = "error"
                    else:
                        status_color = "info"
                    
                    history_rows.append({
                        'component': 'tr',
                        'content': [
                            {
                                'component': 'td',
                                'props': {'class': 'text-caption'},
                                'text': history.get("date", "")
                            },
                            {
                                'component': 'td',
                                'content': [{
                                    'component': 'VChip',
                                    'props': {
                                        'color': status_color,
                                        'size': 'small',
                                        'variant': 'outlined'
                                    },
                                    'text': status
                                }]
                            },
                            {'component': 'td', 'text': history.get('message', '—')},
                            {'component': 'td', 'text': str(history.get('points', '—'))},
                            {'component': 'td', 'text': str(history.get('days', '—'))},
                        ]
                    })
                
                history_table = [{
                    'component': 'VCard',
                    'props': {'variant': 'outlined', 'class': 'mb-4'},
                    'content': [
                        {
                            'component': 'VCardTitle',
                            'props': {'class': 'd-flex align-center justify-space-between'},
                            'content': [
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'span',
                                            'props': {'class': 'text-h6'},
                                            'text': f'📊 {account_name} 签到历史'
                                        }
                                    ]
                                },
                                {
                                    'component': 'VBtn',
                                    'props': {
                                        'color': 'error',
                                        'size': 'small',
                                        'variant': 'outlined',
                                        'title': '点击清空历史数据，随后需手动刷新该页面',
                                        'href': f'/api/plugin/hdhivesign/clear_history?account_index={i}',
                                        'target': '_blank'
                                    },
                                    'text': '🗑️ 清空记录'
                                }
                            ]
                        },
                        {
                            'component': 'VCardText',
                            'content': [{
                                'component': 'VTable',
                                'props': {'hover': True, 'density': 'compact'},
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [{
                                            'component': 'tr',
                                            'content': [
                                                {'component': 'th', 'text': '时间'},
                                                {'component': 'th', 'text': '状态'},
                                                {'component': 'th', 'text': '详情'},
                                                {'component': 'th', 'text': '奖励积分'},
                                                {'component': 'th', 'text': '连续天数'}
                                            ]
                                        }]
                                    },
                                    {'component': 'tbody', 'content': history_rows}
                                ]
                            }]
                        }
                    ]
                }]
            else:
                history_table = [{
                    'component': 'VAlert',
                    'props': {
                        'type': 'info', 'variant': 'tonal',
                        'text': f'{account_name} 暂无签到记录，请等待下一次自动签到或手动触发一次。',
                        'class': 'mb-4'
                    }
                }]
            
            content.extend(info_card)
            content.extend(history_table)
        
        return content

    def get_api(self) -> List[Dict[str, Any]]:
        """
        向系统注册 API 端点
        """
        return [{
            "path": "/clear_history",
            "endpoint": self.clear_history_api,
            "methods": ["GET"],
            "summary": "清理指定账户的签到历史记录"
        }]

    def clear_history_api(self, account_index: int = 0):
        """
        清理历史记录的API处理函数
        """
        try:
            from fastapi.responses import HTMLResponse
            self.save_data(f'sign_history_{account_index}', [])
            html_content = f"""
            <html>
                <head>
                    <meta charset="utf-8">
                    <title>清理成功</title>
                    <script>
                        alert('账户 {account_index + 1} 历史记录已成功清空！\\n请关闭此页面并刷新插件页面查看。');
                        window.close();
                    </script>
                </head>
                <body>
                    <h2 style="text-align: center; margin-top: 50px;">历史记录已清空</h2>
                    <p style="text-align: center;">您可以关闭此标签页并手动刷新插件管理页面。</p>
                </body>
            </html>
            """
            return HTMLResponse(content=html_content)
        except Exception as e:
            logger.error(f"清理历史记录失败: {str(e)}")
            return {"success": False, "message": f"清理失败: {str(e)}"}

    def stop_service(self):
        """
        停止服务
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止影巢签到服务失败: {str(e)}")

    def _is_manual_trigger(self) -> bool:
        """
        判断是否为手动触发
        """
        return getattr(self, '_manual_trigger', False)

    def _clear_extended_retry_tasks(self):
        """
        清理所有延长重试任务
        """
        try:
            if self._scheduler:
                jobs = self._scheduler.get_jobs()
                for job in jobs:
                    if "延长重试" in job.name:
                        self._scheduler.remove_job(job.id)
                        logger.info(f"清理延长重试任务: {job.name}")
        except Exception as e:
            logger.warning(f"清理延长重试任务失败: {str(e)}")

    def _has_running_extended_retry(self) -> bool:
        """
        检查是否有正在运行的延长重试任务
        """
        try:
            if self._scheduler:
                jobs = self._scheduler.get_jobs()
                for job in jobs:
                    if "延长重试" in job.name:
                        return True
            return False
        except Exception:
            return False

    def _is_already_signed_today(self, account_index: int = 0) -> bool:
        """
        检查今天是否已经签到成功
        """
        history = self.get_data(f'sign_history_{account_index}') or []
        if not history:
            return False
        today = datetime.now().strftime('%Y-%m-%d')
        # 查找今日是否有成功签到记录
        return any(
            record.get("date", "").startswith(today)
            and record.get("status") in ["签到成功", "已签到"]
            for record in history
        )
