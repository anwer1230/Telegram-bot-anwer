import os
import json
import uuid
import time
import logging
import asyncio
import threading
import queue
import re
from threading import Lock
from flask import Flask, session, request, render_template, jsonify, redirect
from flask_socketio import SocketIO, emit, join_room, leave_room
from telethon import TelegramClient, events, functions
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError, PhoneCodeInvalidError, PasswordHashInvalidError, FloodWaitError, UserAlreadyParticipantError, InviteHashExpiredError, InviteHashInvalidError
from telethon.sessions import StringSession
import socket

# تحميل متغيرات البيئة من ملف .env
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ تم تحميل متغيرات البيئة من ملف .env")
except ImportError:
    print("⚠️ مكتبة dotenv غير متوفرة - سيتم استخدام متغيرات البيئة المتاحة فقط")

# تكوين السجلات المحسن
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('telegram_monitoring.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# إنشاء التطبيق
app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", os.urandom(24))

# إعداد SocketIO
socketio = SocketIO(
    app, 
    cors_allowed_origins="*",
    async_mode='threading',
    ping_timeout=60, 
    ping_interval=25,
    logger=False, 
    engineio_logger=False,
    allow_upgrades=True,
    transports=['polling', 'websocket']
)

# إعدادات النظام
SESSIONS_DIR = "sessions"
if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

# نظام المستخدمين الخمسة المحددين مسبقاً
PREDEFINED_USERS = {
    "user_1": {
        "id": "user_1",
        "name": "المستخدم الأول",
        "icon": "fas fa-user",
        "color": "#007bff"
    },
    "user_2": {
        "id": "user_2", 
        "name": "المستخدم الثاني",
        "icon": "fas fa-user-tie",
        "color": "#28a745"
    },
    "user_3": {
        "id": "user_3",
        "name": "المستخدم الثالث", 
        "icon": "fas fa-user-graduate",
        "color": "#ffc107"
    },
    "user_4": {
        "id": "user_4",
        "name": "المستخدم الرابع",
        "icon": "fas fa-user-cog",
        "color": "#dc3545"
    },
    "user_5": {
        "id": "user_5",
        "name": "المستخدم الخامس",
        "icon": "fas fa-user-astronaut", 
        "color": "#6f42c1"
    }
}

# معالجات الأخطاء الشاملة
@app.errorhandler(404)
def not_found_error(error):
    try:
        return jsonify({"error": "Page not found"}), 404
    except Exception as e:
        logger.error(f"Error in 404 handler: {str(e)}")
        return jsonify({"error": "Page not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal server error: {str(error)}")
    try:
        return render_template('index.html', 
                              settings={}, 
                              connection_status='disconnected',
                              app_title="مركز سرعة انجاز 📚للخدمات الطلابية والاكاديمية"), 500
    except Exception as e:
        logger.error(f"Error in 500 handler: {str(e)}")
        return jsonify({"error": "Page not found"}), 500

@app.errorhandler(Exception)
def handle_exception(e):
    logger.error(f"Unhandled exception: {str(e)}")
    try:
        return render_template('index.html', 
                              settings={}, 
                              connection_status='disconnected',
                              app_title="مركز سرعة انجاز 📚للخدمات الطلابية والاكاديمية"), 500
    except Exception as template_error:
        logger.error(f"Error in exception handler: {str(template_error)}")
        return jsonify({"error": "Server error"}), 500

# معالج أخطاء Socket.IO
@socketio.on_error_default
def default_error_handler(e):
    logger.error(f"Socket.IO error: {str(e)}")
    try:
        return False
    except Exception:
        pass



USERS = {}
USERS_LOCK = Lock()

# بيانات Telegram API - مع التحقق من مصادر متعددة
API_ID = os.environ.get('API_ID') or os.environ.get('TELEGRAM_API_ID') or '22043994'
API_HASH = os.environ.get('API_HASH') or os.environ.get('TELEGRAM_API_HASH') or '56f64582b363d367280db96586b97801'

# التحقق من صحة القيم
try:
    API_ID = int(API_ID)
    if API_ID and API_HASH and len(API_HASH) > 10:
        logger.info(f"✅ تم تحميل بيانات Telegram API بنجاح - API_ID: {API_ID}")
    else:
        logger.warning("⚠️ بيانات Telegram API غير مكتملة - يرجى التحقق من الإعدادات")
except (ValueError, TypeError):
    logger.error("❌ API_ID يجب أن يكون رقم صحيح")
    API_ID = None

if not API_ID or not API_HASH:
    logger.warning("⚠️ لم يتم إعداد TELEGRAM_API_ID و TELEGRAM_API_HASH - وظائف التليجرام لن تعمل")

# نظام الروابط المؤقتة
TEMP_LINKS = {}
TEMP_LINKS_LOCK = Lock()

def generate_temp_token():
    """توليد رمز مؤقت فريد"""
    return str(uuid.uuid4()).replace('-', '')[:16]

def create_temp_link(duration_hours):
    """إنشاء رابط مؤقت"""
    token = generate_temp_token()
    expiry_time = time.time() + (duration_hours * 3600)

    with TEMP_LINKS_LOCK:
        TEMP_LINKS[token] = {
            'created_at': time.time(),
            'expires_at': expiry_time,
            'duration_hours': duration_hours,
            'is_active': True
        }

    return token

def is_temp_link_valid(token):
    """التحقق من صحة الرابط المؤقت"""
    if not token:
        return False

    with TEMP_LINKS_LOCK:
        if token not in TEMP_LINKS:
            return False

        link_data = TEMP_LINKS[token]
        current_time = time.time()

        if current_time > link_data['expires_at']:
            link_data['is_active'] = False
            return False

        return link_data['is_active']

def get_temp_link_info(token):
    """الحصول على معلومات الرابط المؤقت"""
    with TEMP_LINKS_LOCK:
        return TEMP_LINKS.get(token, None)

# =========================== 
# نظام Queue للتنبيهات المحسن
# ===========================
class AlertQueue:
    """نظام queue متقدم لإدارة التنبيهات"""

    def __init__(self):
        self.queue = queue.Queue()
        self.running = False
        self.thread = None

    def start(self):
        """بدء معالج التنبيهات"""
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._process_alerts, daemon=True)
            self.thread.start()
            logger.info("Alert queue processor started")

    def stop(self):
        """إيقاف معالج التنبيهات"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)

    def add_alert(self, user_id, alert_data):
        """إضافة تنبيه جديد للقائمة"""
        try:
            self.queue.put({
                'user_id': user_id,
                'alert_data': alert_data,
                'timestamp': time.time()
            }, timeout=1)
        except queue.Full:
            logger.warning(f"Alert queue full for user {user_id}")

    def _process_alerts(self):
        """معالجة التنبيهات بشكل مستمر"""
        while self.running:
            try:
                alert = self.queue.get(timeout=1)
                self._send_alert(alert)
                self.queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error processing alert: {str(e)}")

    def _send_alert(self, alert):
        """إرسال التنبيه للمستخدم"""
        user_id = alert['user_id']
        alert_data = alert['alert_data']

        try:
            # إرسال للواجهة
            socketio.emit('new_alert', alert_data, to=user_id)
            socketio.emit('log_update', {
                "message": f"🚨 تنبيه فوري: '{alert_data['keyword']}' في {alert_data['group']}"
            }, to=user_id)

            # إرسال للرسائل المحفوظة
            self._send_to_saved_messages(user_id, alert_data)

            # إرسال نسخة لمجموعة Admin
            self._send_to_admin_group(user_id, alert_data)

        except Exception as e:
            logger.error(f"Failed to send alert for user {user_id}: {str(e)}")

    def _send_to_saved_messages(self, user_id, alert_data):
        """إرسال التنبيه للرسائل المحفوظة"""
        try:
            with USERS_LOCK:
                if user_id in USERS:
                    client_manager = USERS[user_id].get('client_manager')
                    if client_manager and client_manager.client:
                        notification_msg = f"""🚨 تنبيه فوري - مراقبة شاملة للحساب

📝 الكلمة المراقبة: {alert_data['keyword']}
📊 المصدر: {alert_data['group']}
👤 المرسل: {alert_data.get('sender', 'غير معروف')}
🕐 وقت الرسالة: {alert_data.get('message_time', '')}
🔗 معرف الرسالة: {alert_data.get('message_id', '')}

💬 نص الرسالة:
{alert_data.get('message', '')[:500]}{'...' if len(alert_data.get('message', '')) > 500 else ''}

--- تنبيه فوري من المراقبة الشاملة اللحظية لكامل الحساب"""

                        # تشغيل في thread منفصل لضمان عدم التأخير
                        def send_alert_async():
                            try:
                                if hasattr(client_manager, 'run_coroutine'):
                                    client_manager.run_coroutine(
                                        client_manager.client.send_message('me', notification_msg)
                                    )
                                    logger.info(f"✅ Alert sent to saved messages for user {user_id}")
                            except Exception as send_error:
                                logger.error(f"❌ Failed to send alert message: {str(send_error)}")

                        # تشغيل في thread منفصل
                        threading.Thread(target=send_alert_async, daemon=True).start()

        except Exception as e:
            logger.error(f"Failed to send to saved messages: {str(e)}")

    def _escape_html(self, text):
        """تهريب نص HTML لتجنب كسر التنسيق"""
        if not text:
            return ""
        return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    def _send_to_admin_group(self, user_id, alert_data):
        """إرسال نسخة من التنبيه لمجموعة Admin مع روابط حية"""
        try:
            # رابط مجموعة Admin
            ADMIN_GROUP = "https://t.me/+FRhxJ_9OV-4zZGRk"

            # الحصول على اسم المستخدم
            user_name = PREDEFINED_USERS.get(user_id, {}).get('name', user_id)

            # استخراج معلومات المجموعة والرسالة مع التحقق من النوع
            group_name = alert_data.get('group', 'غير معروف') or 'غير معروف'
            message_id = alert_data.get('message_id', '') or ''
            chat_id = alert_data.get('chat_id', '') or ''
            sender_name = alert_data.get('sender', 'غير معروف') or 'غير معروف'
            sender_id = alert_data.get('sender_id', '') or ''
            keyword = alert_data.get('keyword', '') or ''
            message_text = (alert_data.get('message', '') or '')[:300]

            # إنشاء روابط حية بشكل آمن
            # رابط المجموعة
            group_username = alert_data.get('group_username', '') or ''
            if group_username and group_username.strip():
                # إزالة @ إذا كانت موجودة
                clean_username = group_username[1:] if group_username.startswith('@') else group_username
                group_link = f"https://t.me/{clean_username}"
                group_display = f'<a href="{group_link}">{self._escape_html(group_name)}</a>'
            else:
                group_display = self._escape_html(group_name)

            # رابط الرسالة الأصلية
            message_link = None
            if chat_id and message_id:
                if group_username:
                    # مجموعة عامة
                    clean_username = group_username[1:] if group_username.startswith('@') else group_username
                    message_link = f"https://t.me/{clean_username}/{message_id}"
                elif str(chat_id).startswith('-100'):
                    # قناة/مجموعة خاصة
                    channel_id = str(chat_id)[4:]
                    message_link = f"https://t.me/c/{channel_id}/{message_id}"

            if message_link:
                message_display = f'<a href="{message_link}">اضغط للانتقال</a>'
            else:
                message_display = "رسالة خاصة"

            # رابط المرسل
            sender_username = alert_data.get('sender_username', '') or ''
            if sender_username and sender_username.strip():
                clean_sender = sender_username[1:] if sender_username.startswith('@') else sender_username
                sender_link = f"https://t.me/{clean_sender}"
                sender_display = f'<a href="{sender_link}">{self._escape_html(sender_name)}</a>'
            else:
                sender_display = self._escape_html(sender_name)

            # تهريب نص الرسالة
            safe_message_text = self._escape_html(message_text)

            # بناء رسالة التنبيه لـ Admin بصيغة HTML
            admin_notification = f"""🚨 <b>تنبيه جديد من نظام المراقبة</b>

👤 <b>المستخدم:</b> {self._escape_html(user_name)}
🔑 <b>الكلمة المراقبة:</b> {self._escape_html(keyword)}

📊 <b>المصدر:</b> {group_display}
👥 <b>المرسل:</b> {sender_display}
🔗 <b>الرسالة:</b> {message_display}

💬 <b>نص الرسالة:</b>
{safe_message_text}{'...' if len(alert_data.get('message', '')) > 300 else ''}

⏰ <b>وقت التنبيه:</b> {alert_data.get('message_time', time.strftime('%H:%M:%S'))}

---
📱 تنبيه تلقائي من مركز سرعة انجاز"""

            # البحث عن عميل متصل لإرسال الرسالة
            with USERS_LOCK:
                # محاولة الإرسال من حساب المستخدم الحالي
                if user_id in USERS:
                    client_manager = USERS[user_id].get('client_manager')
                    if client_manager and client_manager.client:
                        def send_to_admin_async():
                            try:
                                if hasattr(client_manager, 'run_coroutine'):
                                    # محاولة الانضمام للمجموعة أولاً إذا لم يكن عضواً
                                    try:
                                        from telethon.tl.functions.messages import ImportChatInviteRequest
                                        invite_hash = "FRhxJ_9OV-4zZGRk"  # Hash من الرابط

                                        # محاولة الانضمام (سيتم تجاهله إذا كان عضواً)
                                        try:
                                            client_manager.run_coroutine(
                                                client_manager.client(ImportChatInviteRequest(invite_hash))
                                            )
                                            logger.info(f"✅ Joined Admin group for user {user_id}")
                                        except Exception as join_error:
                                            # قد يكون عضواً بالفعل - نتابع الإرسال
                                            if "INVITE_HASH_INVALID" not in str(join_error):
                                                logger.debug(f"Join attempt: {str(join_error)}")
                                    except Exception as import_error:
                                        logger.debug(f"Import error: {str(import_error)}")

                                    # إرسال الرسالة مع parse_mode='html'
                                    client_manager.run_coroutine(
                                        client_manager.client.send_message(
                                            ADMIN_GROUP,
                                            admin_notification,
                                            parse_mode='html',
                                            link_preview=False
                                        )
                                    )
                                    logger.info(f"✅ Alert sent to Admin group from user {user_id}")
                            except Exception as send_error:
                                logger.error(f"❌ Failed to send to Admin group: {str(send_error)}")
                                # محاولة من مستخدم آخر إذا فشل
                                self._try_send_from_other_user(admin_notification, user_id)

                        threading.Thread(target=send_to_admin_async, daemon=True).start()
                        return

                # إذا لم يكن المستخدم الحالي متصلاً، جرب من مستخدم آخر
                self._try_send_from_other_user(admin_notification, user_id)

        except Exception as e:
            logger.error(f"Failed to send alert to Admin group: {str(e)}")

    def _try_send_from_other_user(self, message, exclude_user_id):
        """محاولة الإرسال من مستخدم آخر متصل"""
        try:
            with USERS_LOCK:
                for uid, user_data in USERS.items():
                    if uid != exclude_user_id:
                        client_manager = user_data.get('client_manager')
                        if client_manager and client_manager.client:
                            def send_async():
                                try:
                                    if hasattr(client_manager, 'run_coroutine'):
                                        client_manager.run_coroutine(
                                            client_manager.client.send_message(
                                                "https://t.me/+FRhxJ_9OV-4zZGRk",
                                                message,
                                                parse_mode='html',
                                                link_preview=False
                                            )
                                        )
                                        logger.info(f"✅ Alert sent to Admin group from backup user {uid}")
                                except Exception as e:
                                    logger.error(f"❌ Backup send failed from {uid}: {str(e)}")

                            threading.Thread(target=send_async, daemon=True).start()
                            return
        except Exception as e:
            logger.error(f"Failed to send from backup user: {str(e)}")

# إنشاء نظام التنبيهات العالمي
alert_queue = AlertQueue()

# =========================== 
# إدارة الجلسات والإعدادات
# ===========================
def save_settings(user_id, settings):
    """حفظ إعدادات المستخدم"""
    try:
        path = os.path.join(SESSIONS_DIR, f"{user_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=4)
        return True
    except Exception as e:
        logger.error(f"Error saving settings for {user_id}: {str(e)}")
        return False

def load_settings(user_id):
    """تحميل إعدادات المستخدم"""
    try:
        path = os.path.join(SESSIONS_DIR, f"{user_id}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}
    except Exception as e:
        logger.error(f"Error loading settings for {user_id}: {str(e)}")
        return {}

def load_all_sessions():
    """تحميل جميع الجلسات الموجودة"""
    logger.info("Loading existing sessions...")
    session_count = 0

    with USERS_LOCK:
        try:
            for filename in os.listdir(SESSIONS_DIR):
                if filename.endswith('.json'):
                    user_id = filename.split('.')[0]
                    settings = load_settings(user_id)

                    if settings and 'phone' in settings:
                        USERS[user_id] = {
                            'client_manager': None,
                            'settings': settings,
                            'thread': None,
                            'is_running': False,
                            'stats': {"sent": 0, "errors": 0},
                            'connected': False,
                            'authenticated': False,
                            'awaiting_code': False,
                            'awaiting_password': False,
                            'phone_code_hash': None,
                            'monitoring_active': False,
                            'event_handlers_registered': False
                        }
                        session_count += 1
                        logger.info(f"✓ Loaded session for {user_id}")

        except Exception as e:
            logger.error(f"Error loading sessions: {str(e)}")

    logger.info(f"Loaded {session_count} sessions successfully")
    return session_count

# =========================== 
# مدير التليجرام المحسن مع Event Handlers
# ===========================
class TelegramClientManager:
    """مدير عملاء التليجرام المحسن مع Event Handlers"""

    def __init__(self, user_id):
        self.user_id = user_id
        self.client = None
        self.loop = None
        self.thread = None
        self.stop_flag = threading.Event()
        self.is_ready = threading.Event()
        self.event_handlers_registered = False
        self.monitored_keywords = []
        self.monitored_groups = []

    def start_client_thread(self):
        """بدء thread منفصل للعميل"""
        if self.thread and self.thread.is_alive():
            return

        self.stop_flag.clear()
        self.is_ready.clear()
        self.thread = threading.Thread(target=self._run_client_loop, daemon=True)
        self.thread.start()

        # انتظار حتى يصبح العميل جاهزاً
        if not self.is_ready.wait(timeout=30):
            raise Exception("Client initialization timeout")

    def _run_client_loop(self):
        """تشغيل event loop للعميل"""
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            session_file = os.path.join(SESSIONS_DIR, f"{self.user_id}_session.session")
            if API_ID and API_HASH:
                self.client = TelegramClient(session_file, int(API_ID), API_HASH)
            else:
                logger.error("API_ID or API_HASH not set")
                return

            self.loop.run_until_complete(self._client_main())

        except Exception as e:
            logger.error(f"Client thread error for {self.user_id}: {str(e)}")
        finally:
            if self.loop:
                self.loop.close()

    async def _client_main(self):
        """الوظيفة الرئيسية للعميل"""
        try:
            if self.client:
                await self.client.connect()
                self.is_ready.set()

                # تسجيل event handlers
                await self._register_event_handlers()

                # الحفاظ على الاتصال
                while not self.stop_flag.is_set():
                    await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Client main error: {str(e)}")
        finally:
            if self.client:
                await self.client.disconnect()

    async def _register_event_handlers(self):
        """تسجيل event handlers للرسائل الجديدة"""
        try:
            if self.event_handlers_registered or not self.client:
                return

            @self.client.on(events.NewMessage)
            async def new_message_handler(event):
                await self._handle_new_message(event)

            self.event_handlers_registered = True
            logger.info(f"Event handlers registered for user {self.user_id}")

        except Exception as e:
            logger.error(f"Failed to register event handlers: {str(e)}")

    async def _handle_new_message(self, event):
        """معالجة الرسائل الجديدة الواردة - مراقبة شاملة لكامل الحساب مع رد تلقائي محسن"""
        try:
            message = event.message
            if not message.text:
                return

            # الحصول على معلومات المحادثة
            chat = await event.get_chat()
            chat_username = getattr(chat, 'username', None)
            chat_title = getattr(chat, 'title', None)

            # تحديد معرف المجموعة/المحادثة
            group_identifier = None
            if chat_username:
                group_identifier = f"@{chat_username}"
            elif chat_title:
                group_identifier = chat_title
            elif hasattr(chat, 'first_name'):
                # محادثة شخصية
                group_identifier = f"محادثة مع {chat.first_name}"
            else:
                group_identifier = f"محادثة {chat.id}"

            # ⚠️ إزالة فحص المجموعات المحددة - مراقبة شاملة لكل شيء
            # مراقبة كامل المجموعات والمحادثات بدون استثناء

            # فحص الكلمات المفتاحية في كل رسالة
            if self.monitored_keywords:  # إذا كان هناك كلمات مراقبة
                message_lower = message.text.lower()
                matched_keyword = None
                
                for keyword in self.monitored_keywords:
                    keyword_lower = keyword.lower().strip()
                    if keyword_lower and keyword_lower in message_lower:
                        matched_keyword = keyword
                        await self._trigger_keyword_alert(message, keyword, group_identifier, event)
                        
                        # تطبيق الرد التلقائي إذا كان مفعل
                        await self._handle_auto_reply(event, keyword, group_identifier)
                        break  # رد واحد فقط لكل رسالة
            else:
                # إذا لم تكن هناك كلمات محددة، راقب كل الرسائل
                await self._trigger_keyword_alert(message, "رسالة جديدة", group_identifier, event)

        except Exception as e:
            logger.error(f"Error handling new message: {str(e)}")

    async def _trigger_keyword_alert(self, message, keyword, group_identifier, event):
        """تشغيل تنبيه الكلمة المفتاحية"""
        try:
            # الحصول على معلومات المرسل
            sender_name = "غير معروف"
            sender_username = ""
            sender_id = ""
            try:
                sender = await event.get_sender()
                if sender:
                    sender_name = getattr(sender, 'first_name', '') or getattr(sender, 'username', '') or str(sender.id)
                    sender_username = getattr(sender, 'username', '')
                    sender_id = str(sender.id) if sender else ""
            except:
                pass

            # الحصول على معلومات المجموعة
            chat = await event.get_chat()
            chat_id = str(chat.id) if chat else ""
            group_username = getattr(chat, 'username', '')

            # إنشاء بيانات التنبيه
            alert_data = {
                "keyword": keyword,
                "group": group_identifier,
                "message": message.text[:200] + "..." if len(message.text) > 200 else message.text,
                "timestamp": time.strftime('%H:%M:%S'),
                "sender": sender_name,
                "sender_username": sender_username,
                "sender_id": sender_id,
                "message_time": time.strftime('%H:%M:%S', time.localtime(message.date.timestamp())),
                "message_id": message.id,
                "chat_id": chat_id,
                "group_username": group_username,
                "full_message": message.text
            }

            # إضافة التنبيه للقائمة بأولوية عالية
            alert_queue.add_alert(self.user_id, alert_data)

            # إرسال فوري للواجهة أيضاً
            try:
                socketio.emit('new_alert', alert_data, to=self.user_id)
                socketio.emit('log_update', {
                    "message": f"🚨 تنبيه فوري: '{keyword}' في {group_identifier} من {sender_name}"
                }, to=self.user_id)
                logger.info(f"✅ Immediate alert sent to interface for user {self.user_id}")
            except Exception as emit_error:
                logger.error(f"❌ Failed to emit immediate alert: {str(emit_error)}")

            logger.info(f"✅ Keyword alert triggered for user {self.user_id}: '{keyword}' in {group_identifier}")

        except Exception as e:
            logger.error(f"❌ Error triggering keyword alert: {str(e)}")

    async def _handle_auto_reply(self, event, keyword, group_identifier):
        """معالجة الرد التلقائي للكلمات المفتاحية"""
        try:
            # الحصول على إعدادات الرد التلقائي من المستخدم
            with USERS_LOCK:
                if self.user_id in USERS:
                    settings = USERS[self.user_id].get('settings', {})
                    auto_reply_enabled = settings.get('auto_reply_enabled', False)
                    auto_replies = settings.get('auto_replies', {})
                    
                    if not auto_reply_enabled:
                        return
                        
                    # البحث عن رد مناسب للكلمة المفتاحية
                    reply_text = None
                    keyword_lower = keyword.lower().strip()
                    
                    # البحث المباشر أولاً
                    if keyword_lower in auto_replies:
                        reply_text = auto_replies[keyword_lower]
                    else:
                        # البحث الجزئي في حالة عدم وجود تطابق مباشر
                        for stored_keyword, stored_reply in auto_replies.items():
                            if stored_keyword.lower() in keyword_lower or keyword_lower in stored_keyword.lower():
                                reply_text = stored_reply
                                break
                    
                    if reply_text and reply_text.strip():
                        # إضافة تأخير عشوائي لتجنب كشف البوت
                        import random
                        await asyncio.sleep(random.uniform(1, 3))
                        
                        # إرسال الرد
                        await self.client.send_message(event.chat_id, reply_text)
                        
                        # تسجيل الرد في السجل
                        socketio.emit('log_update', {
                            "message": f"🤖 رد تلقائي تم إرساله: '{keyword}' → '{reply_text[:50]}...' في {group_identifier}"
                        }, to=self.user_id)
                        
                        logger.info(f"Auto reply sent for keyword '{keyword}' in {group_identifier}")
                        
        except Exception as e:
            logger.error(f"Error in auto reply: {str(e)}")
            socketio.emit('log_update', {
                "message": f"❌ خطأ في الرد التلقائي: {str(e)}"
            }, to=self.user_id)

    def update_monitoring_settings(self, keywords, groups):
        """تحديث إعدادات المراقبة - فقط الكلمات المفتاحية (المجموعات للإرسال فقط)"""
        self.monitored_keywords = [k.strip() for k in keywords if k.strip()]
        # ⚠️ لا نحفظ مجموعات المراقبة - نراقب كل شيء
        # نحفظ مجموعات الإرسال منفصلة في الإعدادات العادية

        logger.info(f"Updated monitoring settings for {self.user_id}: {len(self.monitored_keywords)} keywords - مراقبة شاملة لكامل الحساب")

    def run_coroutine(self, coro):
        """تشغيل coroutine في event loop الخاص بالعميل"""
        if not self.loop:
            raise Exception("Event loop not initialized")

        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=30)

    def stop(self):
        """إيقاف العميل"""
        self.stop_flag.set()
        if self.thread:
            self.thread.join(timeout=5)

def get_all_users_operations_status():
    """الحصول على حالة العمليات لجميع المستخدمين"""
    operations_status = {}

    with USERS_LOCK:
        for user_id, user_data in USERS.items():
            if user_id in PREDEFINED_USERS:
                operations_status[user_id] = {
                    'name': PREDEFINED_USERS[user_id]['name'],
                    'connected': user_data.get('connected', False),
                    'authenticated': user_data.get('authenticated', False),
                    'is_running': user_data.get('is_running', False),
                    'monitoring_active': user_data.get('monitoring_active', False),
                    'stats': user_data.get('stats', {"sent": 0, "errors": 0})
                }

    return operations_status

def notify_user_about_background_operations(user_id):
    """إشعار المستخدم بالعمليات التي تعمل في الخلفية"""
    try:
        active_operations = []

        with USERS_LOCK:
            for uid, user_data in USERS.items():
                if uid != user_id and uid in PREDEFINED_USERS:
                    if user_data.get('is_running', False) or user_data.get('monitoring_active', False):
                        active_operations.append({
                            'user_name': PREDEFINED_USERS[uid]['name'],
                            'operations': []
                        })

                        if user_data.get('monitoring_active', False):
                            active_operations[-1]['operations'].append('مراقبة نشطة')
                        if user_data.get('is_running', False):
                            active_operations[-1]['operations'].append('إرسال مجدول')

        if active_operations:
            operations_text = []
            for op in active_operations:
                operations_text.append(f"• {op['user_name']}: {', '.join(op['operations'])}")

            socketio.emit('log_update', {
                "message": f"📊 العمليات النشطة في الخلفية:\n" + "\n".join(operations_text)
            }, to=user_id)

    except Exception as e:
        logger.error(f"Error notifying about background operations: {str(e)}")

def update_monitoring_settings(self, keywords, groups):
    """تحديث إعدادات المراقبة - فقط الكلمات المفتاحية (المجموعات للإرسال فقط)"""
    self.monitored_keywords = [k.strip() for k in keywords if k.strip()]
    # ⚠️ لا نحفظ مجموعات المراقبة - نراقب كل شيء
    # نحفظ مجموعات الإرسال منفصلة في الإعدادات العادية

    logger.info(f"Updated monitoring settings for {self.user_id}: {len(self.monitored_keywords)} keywords - مراقبة شاملة لكامل الحساب")

def run_coroutine(self, coro):
    """تشغيل coroutine في event loop الخاص بالعميل"""
    if not self.loop:
        raise Exception("Event loop not initialized")

    future = asyncio.run_coroutine_threadsafe(coro, self.loop)
    return future.result(timeout=30)

def stop(self):
    """إيقاف العميل"""
    self.stop_flag.set()
    if self.thread:
        self.thread.join(timeout=5)

# =========================== 
# مدير التليجرام الرئيسي
# ===========================
class TelegramManager:
    """مدير عملاء التليجرام"""

    def __init__(self):
        self.client_managers = {}

    def get_client_manager(self, user_id):
        """الحصول على مدير العميل للمستخدم"""
        if user_id not in self.client_managers:
            self.client_managers[user_id] = TelegramClientManager(user_id)
        return self.client_managers[user_id]

    def setup_client(self, user_id, phone_number):
        """إعداد عميل التليجرام"""
        try:
            if not API_ID or not API_HASH:
                socketio.emit('log_update', {
                    "message": "❌ لم يتم إعداد بيانات Telegram API"
                }, to=user_id)
                return {
                    "status": "error", 
                    "message": "❌ بيانات API غير متوفرة - يرجى إضافة TELEGRAM_API_ID و TELEGRAM_API_HASH في الأسرار"
                }

            # التأكد من عدم وجود ملفات جلسة قديمة لرقم هاتف مختلف
            session_file = os.path.join(SESSIONS_DIR, f"{user_id}_session.session")
            if os.path.exists(session_file):
                try:
                    os.remove(session_file)
                    logger.info(f"Removed old session file for user {user_id}")
                except Exception as e:
                    logger.warning(f"Could not remove old session file: {e}")

            socketio.emit('log_update', {
                "message": "🔄 جاري إعداد العميل..."
            }, to=user_id)

            client_manager = self.get_client_manager(user_id)
            client_manager.start_client_thread()

            socketio.emit('log_update', {
                "message": "📡 فحص حالة التصريح..."
            }, to=user_id)

            is_authorized = client_manager.run_coroutine(
                client_manager.client.is_user_authorized()
            )

            if not is_authorized:
                socketio.emit('log_update', {
                    "message": f"📱 إرسال كود التحقق إلى: {phone_number}"
                }, to=user_id)

                sent = client_manager.run_coroutine(
                    client_manager.client.send_code_request(phone_number)
                )

                with USERS_LOCK:
                    if user_id in USERS:
                        USERS[user_id]['awaiting_code'] = True
                        USERS[user_id]['phone_code_hash'] = sent.phone_code_hash
                        USERS[user_id]['client_manager'] = client_manager
                        USERS[user_id]['connected'] = True

                # إرسال إشعار تحديث حالة تسجيل الدخول
                socketio.emit('login_status', {
                    "logged_in": False,
                    "connected": True,
                    "awaiting_code": True,
                    "awaiting_password": False,
                    "is_running": False
                }, to=user_id)

                socketio.emit('log_update', {
                    "message": "✅ تم إرسال كود التحقق - تحقق من رسائل تيليجرام"
                }, to=user_id)

                return {
                    "status": "code_required", 
                    "message": "📱 تم إرسال كود التحقق"
                }
            else:
                with USERS_LOCK:
                    if user_id in USERS:
                        USERS[user_id]['client_manager'] = client_manager
                        USERS[user_id]['connected'] = True
                        USERS[user_id]['authenticated'] = True
                        USERS[user_id]['awaiting_code'] = False
                        USERS[user_id]['awaiting_password'] = False

                # إرسال إشعار نجح تسجيل الدخول
                socketio.emit('login_status', {
                    "logged_in": True,
                    "connected": True,
                    "awaiting_code": False,
                    "awaiting_password": False,
                    "is_running": False
                }, to=user_id)

                socketio.emit('connection_status', {
                    "status": "connected"
                }, to=user_id)

                return {"status": "success", "message": "✅ تم تسجيل الدخول"}

        except Exception as e:
            error_message = str(e)
            logger.error(f"Setup error for {user_id}: {error_message}")

            # معالجة خاصة لخطأ ResendCodeRequest
            if "ResendCodeRequest" in error_message or "all available options" in error_message:
                socketio.emit('log_update', {
                    "message": "⚠️ تم استنفاد محاولات إرسال الكود. يرجى الانتظار قليلاً ثم المحاولة مرة أخرى"
                }, to=user_id)
                return {"status": "error", "message": "⚠️ يرجى الانتظار قبل طلب كود جديد"}

            socketio.emit('log_update', {
                "message": f"❌ خطأ في الإعداد: {error_message}"
            }, to=user_id)
            return {"status": "error", "message": f"❌ خطأ: {error_message}"}

    def verify_code(self, user_id, code):
        """التحقق من كود التحقق"""
        try:
            with USERS_LOCK:
                if user_id not in USERS or not USERS[user_id].get('awaiting_code'):
                    return {"status": "error", "message": "❌ لم يتم طلب كود التحقق"}

                client_manager = USERS[user_id].get('client_manager')
                phone_code_hash = USERS[user_id].get('phone_code_hash')
                phone = USERS[user_id]['settings']['phone']

            if not client_manager or not phone_code_hash:
                return {"status": "error", "message": "❌ بيانات الجلسة مفقودة"}

            try:
                user = client_manager.run_coroutine(
                    client_manager.client.sign_in(phone, code, phone_code_hash=phone_code_hash)
                )

                with USERS_LOCK:
                    USERS[user_id]['connected'] = True
                    USERS[user_id]['authenticated'] = True
                    USERS[user_id]['awaiting_code'] = False
                    USERS[user_id]['awaiting_password'] = False

                # إرسال تحديث حالة تسجيل الدخول
                socketio.emit('login_status', {
                    "logged_in": True,
                    "connected": True,
                    "awaiting_code": False,
                    "awaiting_password": False,
                    "is_running": False
                }, to=user_id)

                socketio.emit('connection_status', {
                    "status": "connected"
                }, to=user_id)

                return {"status": "success", "message": "✅ تم التحقق بنجاح"}

            except SessionPasswordNeededError:
                with USERS_LOCK:
                    USERS[user_id]['awaiting_code'] = False
                    USERS[user_id]['awaiting_password'] = True

                # إرسال تحديث حالة تسجيل الدخول
                socketio.emit('login_status', {
                    "logged_in": False,
                    "connected": True,
                    "awaiting_code": False,
                    "awaiting_password": True,
                    "is_running": False
                }, to=user_id)

                return {
                    "status": "password_required", 
                    "message": "🔒 يرجى إدخال كلمة مرور التحقق بخطوتين"
                }

        except PhoneCodeInvalidError:
            return {"status": "error", "message": "❌ كود التحقق غير صحيح"}
        except PhoneCodeExpiredError:
            return {"status": "error", "message": "❌ انتهت صلاحية كود التحقق"}
        except Exception as e:
            logger.error(f"Code verification error: {str(e)}")
            return {"status": "error", "message": f"❌ خطأ: {str(e)}"}

    def verify_password(self, user_id, password):
        """التحقق من كلمة المرور"""
        try:
            with USERS_LOCK:
                if user_id not in USERS or not USERS[user_id].get('awaiting_password'):
                    return {"status": "error", "message": "❌ لم يتم طلب كلمة المرور"}

                client_manager = USERS[user_id].get('client_manager')

            if not client_manager:
                return {"status": "error", "message": "❌ بيانات الجلسة مفقودة"}

            try:
                await_result = client_manager.run_coroutine(
                    client_manager.client.sign_in(password=password)
                )

                with USERS_LOCK:
                    USERS[user_id]['connected'] = True
                    USERS[user_id]['authenticated'] = True
                    USERS[user_id]['awaiting_password'] = False

                # إرسال تحديث حالة تسجيل الدخول بعد كلمة المرور
                socketio.emit('login_status', {
                    'logged_in': True,
                    'connected': True,
                    'awaiting_code': False,
                    'awaiting_password': False
                }, to=user_id)

                return {"status": "success", "message": "✅ تم التحقق بنجاح"}

            except PasswordHashInvalidError:
                return {"status": "error", "message": "❌ كلمة المرور غير صحيحة"}

        except Exception as e:
            logger.error(f"Password verification error: {str(e)}")
            return {"status": "error", "message": f"❌ خطأ: {str(e)}"}

    def send_message_async(self, user_id, entity, message):
        """إرسال رسالة"""
        try:
            with USERS_LOCK:
                if user_id not in USERS:
                    raise Exception("المستخدم غير موجود - يرجى تسجيل الدخول أولاً")

                client_manager = USERS[user_id].get('client_manager')
                if not client_manager:
                    raise Exception("لم يتم تسجيل الدخول - يرجى تسجيل الدخول في التليجرام أولاً")

                if not client_manager.client:
                    raise Exception("عميل التليجرام غير مُهيأ - يرجى إعادة تسجيل الدخول")

            try:
                is_authorized = client_manager.run_coroutine(
                    client_manager.client.is_user_authorized()
                )

                if not is_authorized:
                    raise Exception("جلسة التليجرام منتهية الصلاحية - يرجى إعادة تسجيل الدخول")
            except Exception as auth_error:
                raise Exception(f"خطأ في التحقق من التصريح: {str(auth_error)}")

            try:
                entity_obj = client_manager.run_coroutine(
                    client_manager.client.get_entity(entity)
                )
            except:
                if not entity.startswith('@') and not entity.startswith('https://'):
                    entity = '@' + entity
                entity_obj = client_manager.run_coroutine(
                    client_manager.client.get_entity(entity)
                )

            result = client_manager.run_coroutine(
                client_manager.client.send_message(entity_obj, message)
            )

            return {"success": True, "message_id": result.id}

        except Exception as e:
            logger.error(f"Send message error: {str(e)}")
            raise Exception(str(e))

    def send_media_async(self, user_id, entity, image_files):
        """إرسال الصور فقط"""
        try:
            with USERS_LOCK:
                if user_id not in USERS:
                    raise Exception("المستخدم غير موجود")

                client_manager = USERS[user_id].get('client_manager')

            if not client_manager:
                raise Exception("العميل غير متصل")

            is_authorized = client_manager.run_coroutine(
                client_manager.client.is_user_authorized()
            )

            if not is_authorized:
                raise Exception("العميل غير مصرح")

            try:
                entity_obj = client_manager.run_coroutine(
                    client_manager.client.get_entity(entity)
                )
            except:
                if not entity.startswith('@') and not entity.startswith('https://'):
                    entity = '@' + entity
                entity_obj = client_manager.run_coroutine(
                    client_manager.client.get_entity(entity)
                )

            # إرسال كل صورة منفصلة
            results = []
            for img_file in image_files:
                try:
                    result = client_manager.run_coroutine(
                        client_manager.client.send_file(
                            entity_obj, 
                            img_file['path'],
                            caption=f"📷 {img_file['name']}"
                        )
                    )
                    results.append(result.id)
                except Exception as img_error:
                    logger.error(f"Error sending image {img_file['name']}: {str(img_error)}")
                    raise img_error

            return {"success": True, "message_ids": results}

        except Exception as e:
            logger.error(f"Send media error: {str(e)}")
            raise Exception(str(e))

    def send_message_with_media_async(self, user_id, entity, message, image_files):
        """إرسال رسالة مع صور - طريقة محسنة ومُصلحة"""
        try:
            with USERS_LOCK:
                if user_id not in USERS:
                    raise Exception("المستخدم غير موجود")

                client_manager = USERS[user_id].get('client_manager')

            if not client_manager:
                raise Exception("العميل غير متصل")

            is_authorized = client_manager.run_coroutine(
                client_manager.client.is_user_authorized()
            )

            if not is_authorized:
                raise Exception("العميل غير مصرح")

            try:
                entity_obj = client_manager.run_coroutine(
                    client_manager.client.get_entity(entity)
                )
            except:
                if not entity.startswith('@') and not entity.startswith('https://'):
                    entity = '@' + entity
                entity_obj = client_manager.run_coroutine(
                    client_manager.client.get_entity(entity)
                )

            results = []

            # إرسال الصور مع الرسالة النصية
            if image_files and len(image_files) > 0:
                # طريقة محسنة: إرسال جميع الصور مع النص كرسالة واحدة
                try:
                    # تحضير مسارات الصور
                    image_paths = []
                    for img_file in image_files:
                        if os.path.exists(img_file['path']):
                            image_paths.append(img_file['path'])
                        else:
                            logger.warning(f"Image file not found: {img_file['path']}")

                    if image_paths:
                        # إرسال كل الصور مع النص كرسالة واحدة
                        if len(image_paths) == 1:
                            # صورة واحدة فقط
                            media_result = client_manager.run_coroutine(
                                client_manager.client.send_file(
                                    entity_obj, 
                                    image_paths[0],
                                    caption=message if message else "📷"
                                )
                            )
                            results.append(media_result.id)
                            logger.info(f"Successfully sent single image with message to {entity}")
                        else:
                            # عدة صور - إرسال النص أولاً ثم الصور أسفله واحدة تلو الأخرى
                            # إرسال النص أولاً إذا كان موجوداً
                            if message and message.strip():
                                text_result = client_manager.run_coroutine(
                                    client_manager.client.send_message(entity_obj, message)
                                )
                                results.append(text_result.id)

                            # إرسال الصور واحدة تلو الأخرى أسفل الرسالة
                            for i, img_path in enumerate(image_paths):
                                try:
                                    media_result = client_manager.run_coroutine(
                                        client_manager.client.send_file(
                                            entity_obj, 
                                            img_path,
                                            caption=f"📷 صورة {i+1} من {len(image_paths)}"
                                        )
                                    )
                                    results.append(media_result.id)
                                    logger.info(f"Sent image {i+1}/{len(image_paths)} to {entity}")
                                except Exception as img_error:
                                    logger.error(f"Error sending individual image {i+1}: {str(img_error)}")
                                    continue

                            logger.info(f"Successfully sent message + {len(image_paths)} images to {entity}")

                except Exception as media_error:
                    logger.error(f"Error in media sending process: {str(media_error)}")
                    # كحل أخير، أرسل النص فقط
                    if message and message.strip():
                        text_result = client_manager.run_coroutine(
                            client_manager.client.send_message(entity_obj, message)
                        )
                        results.append(text_result.id)
                        logger.info(f"Sent text only due to media error: {str(media_error)}")
            else:
                # إذا لم تكن هناك صور، أرسل الرسالة النصية فقط
                if message and message.strip():
                    text_result = client_manager.run_coroutine(
                        client_manager.client.send_message(entity_obj, message)
                    )
                    results.append(text_result.id)
                    logger.info(f"Successfully sent text message to {entity}")

            return {"success": True, "message_ids": results}

        except Exception as e:
            logger.error(f"Send message with media error: {str(e)}")
            raise Exception(str(e))


# إنشاء مدير التليجرام
telegram_manager = TelegramManager()

# =========================== 
# نظام المراقبة المحسن مع Event Handlers
# ===========================
def monitoring_worker(user_id):
    """مهمة المراقبة المحسنة مع Event Handlers وحماية من التوقف"""
    logger.info(f"Starting enhanced monitoring worker with event handlers for user {user_id}")

    try:
        with USERS_LOCK:
            if user_id not in USERS:
                logger.error(f"No user data found for {user_id}")
                return

            USERS[user_id]['monitoring_active'] = True
            USERS[user_id]['last_heartbeat'] = time.time()
            client_manager = USERS[user_id].get('client_manager')
            settings = USERS[user_id]['settings']

        if not client_manager:
            logger.error(f"No client manager for user {user_id}")
            return

        # تحديث إعدادات المراقبة في العميل
        watch_words = settings.get('watch_words', [])
        send_groups = settings.get('groups', [])  # مجموعات الإرسال فقط

        if hasattr(client_manager, 'update_monitoring_settings'):
            client_manager.update_monitoring_settings(watch_words, send_groups)
        else:
            logger.warning(f"Client manager for {user_id} does not have update_monitoring_settings method.")

        # إرسال إشعار بدء المراقبة
        auto_reply_status = "مُفعل" if settings.get('auto_reply_enabled', False) else "مُعطل"
        if watch_words:
            socketio.emit('log_update', {
                "message": f"🚀 بدأت المراقبة الشاملة الفورية - {len(watch_words)} كلمة مراقبة في كامل الحساب | الرد التلقائي: {auto_reply_status} | الإرسال لـ {len(send_groups)} مجموعة"
            }, to=user_id)
        else:
            socketio.emit('log_update', {
                "message": f"🚀 بدأت المراقبة الشاملة لكامل الرسائل في الحساب | الرد التلقائي: {auto_reply_status} | الإرسال لـ {len(send_groups)} مجموعة"
            }, to=user_id)

        # الحفاظ على المراقبة نشطة مع نظام مراقبة محسن
        consecutive_errors = 0
        max_consecutive_errors = 3
        last_activity_check = time.time()
        heartbeat_interval = 30  # إرسال heartbeat كل 30 ثانية

        while True:
            with USERS_LOCK:
                if user_id not in USERS or not USERS[user_id].get('is_running', False):
                    logger.info(f"Stopping monitoring for user {user_id} as is_running is False")
                    break

                user_data = USERS[user_id].copy()
                USERS[user_id]['monitoring_active'] = True
                USERS[user_id]['last_heartbeat'] = time.time()

            try:
                current_time = time.time()

                # فحص حالة العميل والاتصال
                if client_manager and client_manager.client:
                    try:
                        # فحص دوري لحالة الاتصال
                        is_connected = client_manager.run_coroutine(
                            client_manager.client.is_user_authorized()
                        )
                        
                        if not is_connected:
                            logger.warning(f"Client not authorized for user {user_id}, attempting reconnection...")
                            socketio.emit('log_update', {
                                "message": "⚠️ فقدان الاتصال - محاولة إعادة الاتصال..."
                            }, to=user_id)
                            # محاولة إعادة الاتصال
                            client_manager.start_client_thread()
                            
                    except Exception as conn_error:
                        logger.warning(f"Connection check failed for user {user_id}: {str(conn_error)}")

                # تنفيذ الإرسال المجدول إذا كان مطلوب
                settings = user_data.get('settings', {})
                send_type = settings.get('send_type', 'manual')

                if send_type == 'scheduled':
                    interval_seconds = int(settings.get('interval_seconds', 3600))
                    last_send = user_data.get('last_scheduled_send', 0)

                    if current_time - last_send >= interval_seconds:
                        logger.info(f"Executing scheduled send for user {user_id}")
                        execute_scheduled_messages(user_id, settings)

                        with USERS_LOCK:
                            if user_id in USERS:
                                USERS[user_id]['last_scheduled_send'] = current_time

                consecutive_errors = 0

                # إرسال إشارة حياة محسنة
                if current_time - last_activity_check >= heartbeat_interval:
                    status_info = {
                        'timestamp': time.strftime('%H:%M:%S'),
                        'status': 'active',
                        'type': 'event_driven_monitoring',
                        'keywords_active': bool(watch_words),
                        'event_handlers': True,
                        'auto_reply_enabled': settings.get('auto_reply_enabled', False),
                        'uptime': int(current_time - USERS[user_id].get('monitoring_start_time', current_time))
                    }

                    socketio.emit('heartbeat', status_info, to=user_id)
                    last_activity_check = current_time

                    # تسجيل نشاط دوري كل 5 دقائق
                    if int(current_time) % 300 == 0:
                        socketio.emit('log_update', {
                            "message": f"✅ المراقبة نشطة - آخر فحص: {time.strftime('%H:%M:%S')}"
                        }, to=user_id)

            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Monitoring cycle error for {user_id}: {str(e)}")

                socketio.emit('log_update', {
                    "message": f"⚠️ خطأ في المراقبة ({consecutive_errors}/{max_consecutive_errors}): {str(e)[:100]}"
                }, to=user_id)

                if consecutive_errors >= max_consecutive_errors:
                    socketio.emit('log_update', {
                        "message": f"❌ إيقاف مؤقت للمراقبة - محاولة إعادة التشغيل خلال 30 ثانية..."
                    }, to=user_id)
                    
                    # انتظار 30 ثانية ثم محاولة إعادة التشغيل
                    time.sleep(30)
                    consecutive_errors = 0
                    
                    socketio.emit('log_update', {
                        "message": f"🔄 إعادة تشغيل المراقبة..."
                    }, to=user_id)
                    continue

            # فترة انتظار مناسبة
            time.sleep(5)  # تقليل الفترة لاستجابة أسرع

    except Exception as e:
        logger.error(f"Monitoring worker top-level error for {user_id}: {str(e)}")
        socketio.emit('log_update', {
            "message": f"❌ خطأ رئيسي في المراقبة: {str(e)}"
        }, to=user_id)
    finally:
        with USERS_LOCK:
            if user_id in USERS:
                USERS[user_id]['is_running'] = False
                USERS[user_id]['monitoring_active'] = False
                USERS[user_id]['thread'] = None

        socketio.emit('log_update', {
            "message": "⏹ تم إيقاف نظام المراقبة المحسن"
        }, to=user_id)

        socketio.emit('heartbeat', {
            'timestamp': time.strftime('%H:%M:%S'),
            'status': 'stopped'
        }, to=user_id)

        logger.info(f"Enhanced monitoring worker ended for user {user_id}")

def execute_scheduled_messages(user_id, settings):
    """تنفيذ الإرسال المجدول"""
    groups = settings.get('groups', [])
    message = settings.get('message', '')

    if not groups or not message:
        return

    try:
        socketio.emit('log_update', {
            "message": f"📅 تنفيذ الإرسال المجدول إلى {len(groups)} مجموعة"
        }, to=user_id)

        successful = 0
        failed = 0

        for i, group in enumerate(groups, 1):
            try:
                result = telegram_manager.send_message_async(user_id, group, message)

                socketio.emit('log_update', {
                    "message": f"✅ [{i}/{len(groups)}] إرسال مجدول نجح إلى: {group}"
                }, to=user_id)

                successful += 1
                with USERS_LOCK:
                    if user_id in USERS:
                        USERS[user_id]['stats']['sent'] += 1

                if i < len(groups):
                    time.sleep(3)

            except Exception as e:
                error_msg = str(e)
                logger.error(f"Scheduled send error to {group}: {error_msg}")

                socketio.emit('log_update', {
                    "message": f"❌ [{i}/{len(groups)}] إرسال مجدول فشل إلى {group}"
                }, to=user_id)

                failed += 1
                with USERS_LOCK:
                    if user_id in USERS:
                        USERS[user_id]['stats']['errors'] += 1

        socketio.emit('log_update', {
            "message": f"📊 انتهى الإرسال المجدول: ✅ {successful} نجح | ❌ {failed} فشل"
        }, to=user_id)

    except Exception as e:
        logger.error(f"Scheduled messages error: {str(e)}")

# =========================== 
# أحداث Socket.IO
# ===========================
@socketio.on('connect')
def handle_connect():
    try:
        # إذا لم يكن هناك user_id، نستخدم المستخدم الأول كافتراضي
        if 'user_id' not in session:
            session['user_id'] = "user_1"  # المستخدم الافتراضي
            session.permanent = True

        user_id = session['user_id']

        # التأكد من أن المستخدم ضمن المستخدمين المحددين مسبقاً
        if user_id not in PREDEFINED_USERS:
            user_id = "user_1"  # الافتراضي إذا لم يكن مستخدماً صحيحاً
            session['user_id'] = user_id

        join_room(user_id)
        logger.info(f"User {user_id} ({PREDEFINED_USERS[user_id]['name']}) connected via socket")

        # إرسال إشارة اتصال فورية مع معلومات المستخدم
        emit('connection_confirmed', {
            'status': 'connected',
            'user_id': user_id,
            'user_name': PREDEFINED_USERS[user_id]['name'],
            'timestamp': time.strftime('%H:%M:%S')
        })

        # إرسال قائمة المستخدمين المتاحين
        emit('users_list', {
            'current_user': user_id,
            'users': PREDEFINED_USERS
        })

        # إشعار بالعمليات النشطة في الخلفية
        notify_user_about_background_operations(user_id)

        # إرسال حالة جميع المستخدمين
        all_status = get_all_users_operations_status()
        emit('all_users_status', all_status)

    except Exception as e:
        logger.error(f"Connection error: {str(e)}")
        emit('connection_error', {'message': str(e)})

# دالة Socket.IO للتبديل بين المستخدمين - محسنة
@socketio.on('switch_user')
def handle_switch_user(data):
    """التبديل إلى مستخدم مختلف"""
    try:
        new_user_id = data.get('user_id')

        if not new_user_id or new_user_id not in PREDEFINED_USERS:
            emit('error', {'message': 'مستخدم غير صحيح'})
            return

        # مغادرة الغرفة القديمة بأمان
        old_user_id = session.get('user_id', 'user_1')
        try:
            leave_room(old_user_id)
        except Exception as leave_error:
            logger.warning(f"Error leaving room {old_user_id}: {str(leave_error)}")

        # تحديث الجلسة
        session['user_id'] = new_user_id
        session.permanent = True

        # الانضمام للغرفة الجديدة بأمان
        try:
            join_room(new_user_id)
        except Exception as join_error:
            logger.warning(f"Error joining room {new_user_id}: {str(join_error)}")

        logger.info(f"User switched from {old_user_id} to {new_user_id}")

        # إرسال تأكيد التبديل
        emit('user_switched', {
            'current_user': new_user_id,
            'user_name': PREDEFINED_USERS[new_user_id]['name'],
            'message': f"تم التبديل إلى {PREDEFINED_USERS[new_user_id]['name']}"
        })

        # إرسال حالة المستخدم الجديد
        try:
            with USERS_LOCK:
                if new_user_id in USERS:
                    user_data = USERS[new_user_id]
                    connected = user_data.get('connected', False)
                    authenticated = user_data.get('authenticated', False)
                    awaiting_code = user_data.get('awaiting_code', False)
                    awaiting_password = user_data.get('awaiting_password', False)
                    is_running = user_data.get('is_running', False)

                    emit('connection_status', {
                        "status": "connected" if connected else "disconnected"
                    })

                    emit('login_status', {
                        "logged_in": authenticated,
                        "connected": connected,
                        "awaiting_code": awaiting_code,
                        "awaiting_password": awaiting_password,
                        "is_running": is_running
                    })

                    # إرسال إعدادات المستخدم
                    settings = load_settings(new_user_id)
                    emit('user_settings', settings)
                else:
                    # إرسال حالة افتراضية للمستخدم الجديد
                    emit('connection_status', {"status": "disconnected"})
                    emit('login_status', {
                        "logged_in": False,
                        "connected": False,
                        "awaiting_code": False,
                        "awaiting_password": False,
                        "is_running": False
                    })
        except Exception as status_error:
            logger.error(f"Error sending user status: {str(status_error)}")

    except Exception as e:
        logger.error(f"Error switching user: {str(e)}")
        emit('error', {'message': f'خطأ في التبديل: {str(e)}'})

    # إرسال حالة الاتصال فوراً
    with USERS_LOCK:
        if user_id in USERS:
            connected = USERS[user_id].get('connected', False)
            authenticated = USERS[user_id].get('authenticated', False)
            awaiting_code = USERS[user_id].get('awaiting_code', False)
            awaiting_password = USERS[user_id].get('awaiting_password', False)
            is_running = USERS[user_id].get('is_running', False)

            emit('connection_status', {
                "status": "connected" if connected else "disconnected"
            })

            emit('login_status', {
                "logged_in": authenticated,
                "connected": connected,
                "awaiting_code": awaiting_code,
                "awaiting_password": awaiting_password,
                "is_running": is_running
            })

    emit('console_log', {
        "message": f"[{time.strftime('%H:%M:%S')}] INFO: Socket connected"
    })

    # إرسال رسالة ترحيب
    emit('log_update', {
        "message": f"🔄 تم الاتصال بالخادم - {time.strftime('%H:%M:%S')}"
    })


@socketio.on('disconnect')
def handle_disconnect(data=None):
    if 'user_id' in session:
        user_id = session['user_id']
        leave_room(user_id)
        logger.info(f"User {user_id} disconnected from socket")

# =========================== 
# المسارات الأساسية
# ===========================
@app.route("/")
@app.route("/temp/<token>")
def index(token=None):
    # التحقق من الرابط المؤقت
    is_temp_access = False
    temp_info = None

    if token:
        if is_temp_link_valid(token):
            is_temp_access = True
            temp_info = get_temp_link_info(token)
            logger.info(f"Valid temporary access with token: {token}")
        else:
            # رابط مؤقت غير صالح أو منتهي الصلاحية
            return render_template('temp_expired.html', 
                                 app_title="مركز سرعة انجاز 📚للخدمات الطلابية والاكاديمية",
                                 whatsapp_link="https://wa.me/+966510349663")

    # إنشاء أو التحقق من user_id مع نظام المستخدمين الخمسة
    if 'user_id' not in session:
        session['user_id'] = "user_1"  # المستخدم الافتراضي
        session.permanent = True
    elif session['user_id'] not in PREDEFINED_USERS:
        # إذا كان المستخدم غير صالح، استخدم الافتراضي
        session['user_id'] = "user_1"

    user_id = session['user_id']

    # تحميل إعدادات المستخدم الحالي (قد تكون فارغة للمستخدمين الجدد)
    settings = load_settings(user_id)
    connection_status = "disconnected"

    # التأكد من وجود بيانات المستخدم في الذاكرة
    with USERS_LOCK:
        if user_id not in USERS:
            # إنشاء بيانات افتراضية للمستخدم إذا لم تكن موجودة
            USERS[user_id] = {
                'client_manager': None,
                'settings': settings,
                'thread': None,
                'is_running': False,
                'stats': {"sent": 0, "errors": 0},
                'connected': False,
                'authenticated': False,
                'awaiting_code': False,
                'awaiting_password': False,
                'phone_code_hash': None,
                'monitoring_active': False,
                'event_handlers_registered': False
            }

        # الحصول على حالة الاتصال للمستخدم الحالي
        user_data = USERS[user_id]
        connected = user_data.get('connected', False)
        connection_status = "connected" if connected else "disconnected"

    # إضافة عنوان التطبيق
    app_title = "مركز سرعة انجاز 📚للخدمات الطلابية والاكاديمية"
    whatsapp_link = "https://wa.me/+966510349663"

    # إضافة معلومات المستخدم الحالي والمستخدمين المتاحين
    current_user = PREDEFINED_USERS[user_id]

    response = render_template('index.html',
                          settings=settings,
                          connection_status=connection_status,
                          app_title=app_title,
                          whatsapp_link=whatsapp_link,
                          current_user=current_user,
                          predefined_users=PREDEFINED_USERS,
                          is_temp_access=is_temp_access,
                          temp_info=temp_info,
                          temp_token=token)

    # إنشاء response object مع headers لمنع التخزين المؤقت
    from flask import make_response
    resp = make_response(response)
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'

    return resp

@app.route("/fresh")
def fresh():
    """مسار جديد لتجاوز أي مشاكل في التخزين المؤقت"""
    from flask import make_response
    html = """<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
    <meta charset="UTF-8">
    <title>🚀 التطبيق يعمل بنجاح!</title>
    <style>
        body { font-family: Arial, sans-serif; text-align: center; padding: 50px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
        .success { font-size: 2em; margin: 20px 0; }
        .message { font-size: 1.2em; margin: 10px 0; }
        .btn { background: #28a745; color: white; padding: 15px 30px; text-decoration: none; border-radius: 8px; font-size: 1.1em; display: inline-block; margin: 10px; }
        .btn:hover { background: #218838; color: white; }
    </style>
</head>
<body>
    <div class="success">✅ التطبيق يعمل بشكل مثالي!</div>
    <div class="message">🎉 مركز سرعة انجاز للخدمات الطلابية والأكاديمية</div>
    <div class="message">📱 نظام مراقبة التليجرام الذكي</div>
    <a href="/" class="btn">🏠 الانتقال للتطبيق الرئيسي</a>
    <script>
        setTimeout(function() {
            window.location.href = '/';
        }, 3000);
    </script>
</body>
</html>"""

    resp = make_response(html)
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'

    return resp

# معالجات heartbeat
@socketio.on('heartbeat')
def handle_heartbeat(data):
    try:
        user_id = session.get('user_id')
        if user_id:
            emit('heartbeat_response', {
                'timestamp': time.time(),
                'server_time': time.strftime('%H:%M:%S')
            })
    except Exception as e:
        logger.error(f"Heartbeat error: {str(e)}")

@app.route('/static/<path:filename>')
def static_files(filename):
    """خدمة الملفات الثابتة بما في ذلك manifest وأيقونات PWA"""
    return app.send_static_file(filename)

@app.route('/sw.js')
def service_worker():
    """خدمة Service Worker من الجذر للـ PWA"""
    return app.send_static_file('sw.js')

# =========================== 
# API Routes - نفس الكود الأصلي مع إضافات تحسين
# ===========================

@app.route("/api", methods=["GET", "HEAD"])
def api_health():
    """نقطة نهاية صحة النظام - لمنع أخطاء 404 من heartbeat"""
    try:
        if request.method == "HEAD":
            return "", 200
        return jsonify({"status": "ok", "timestamp": time.time(), "message": "Server is running"})
    except Exception as e:
        logger.error(f"Error in api health check: {str(e)}")
        if request.method == "HEAD":
            return "", 500
        return jsonify({"status": "error", "message": "Server error"}), 500
@app.route("/api/save_login", methods=["POST"])
def api_save_login():
    data = request.json

    if not data or not data.get('phone'):
        return jsonify({
            "success": False, 
            "message": "❌ يرجى إدخال رقم الهاتف"
        })

    new_phone = data.get('phone')

    # استخدام نظام المستخدمين الخمسة المحددين مسبقاً
    if 'user_id' not in session or session['user_id'] not in PREDEFINED_USERS:
        session['user_id'] = "user_1"  # المستخدم الافتراضي
        session.permanent = True

    user_id = session['user_id']
    current_settings = load_settings(user_id)

    # إذا تغير رقم الهاتف للمستخدم الحالي، أوقف الجلسة القديمة
    if current_settings.get('phone') and current_settings.get('phone') != new_phone:
        logger.info(f"Phone number changed for {user_id} from {current_settings.get('phone')} to {new_phone}")

        # إيقاف الجلسة الحالية إذا كانت نشطة
        with USERS_LOCK:
            if user_id in USERS:
                if USERS[user_id].get('is_running'):
                    USERS[user_id]['is_running'] = False

                client_manager = USERS[user_id].get('client_manager')
                if client_manager:
                    client_manager.stop()

                del USERS[user_id]

        socketio.emit('log_update', {
            "message": f"🔄 تم تحديث رقم الهاتف لـ {PREDEFINED_USERS[user_id]['name']}"
        }, to=user_id)

    settings = {
        'phone': new_phone,
        'password': data.get('password', ''),
        'login_time': time.time()
    }

    if not save_settings(user_id, settings):
        return jsonify({
            "success": False, 
            "message": "❌ فشل في حفظ البيانات"
        })

    try:
        socketio.emit('log_update', {
            "message": f"🔄 بدء عملية تسجيل الدخول لـ {PREDEFINED_USERS[user_id]['name']}..."
        }, to=user_id)

        # تحديث أو إنشاء الجلسة للمستخدم الحالي فقط
        with USERS_LOCK:
            if user_id not in USERS:
                USERS[user_id] = {
                    'client_manager': None,
                    'settings': settings,
                    'thread': None,
                    'is_running': False,
                    'stats': {"sent": 0, "errors": 0},
                    'connected': False,
                    'authenticated': False,
                    'awaiting_code': False,
                    'awaiting_password': False,
                    'phone_code_hash': None,
                    'monitoring_active': False,
                    'event_handlers_registered': False
                }
            else:
                # تحديث الإعدادات فقط إذا كان المستخدم موجود
                USERS[user_id]['settings'] = settings

        result = telegram_manager.setup_client(user_id, settings['phone'])

        if result["status"] == "success":
            socketio.emit('log_update', {
                "message": "✅ تم تسجيل الدخول بنجاح"
            }, to=user_id)

            socketio.emit('connection_status', {
                "status": "connected"
            }, to=user_id)

            # إرسال تحديث حالة تسجيل الدخول للواجهة
            socketio.emit('login_status', {
                "logged_in": True,
                "connected": True,
                "awaiting_code": False,
                "awaiting_password": False,
                "is_running": False
            }, to=user_id)

            return jsonify({
                "success": True, 
                "message": "✅ تم تسجيل الدخول"
            })

        elif result["status"] == "code_required":
            socketio.emit('log_update', {
                "message": "📱 تم إرسال كود التحقق"
            }, to=user_id)

            return jsonify({
                "success": True, 
                "message": "📱 تم إرسال كود التحقق", 
                "code_required": True
            })

        else:
            error_message = result.get('message', 'خطأ غير معروف')
            socketio.emit('log_update', {
                "message": f"❌ {error_message}"
            }, to=user_id)

            return jsonify({
                "success": False, 
                "message": f"❌ {error_message}"
            })

    except Exception as e:
        logger.error(f"Login error for user {user_id}: {str(e)}")
        socketio.emit('log_update', {
            "message": f"❌ خطأ: {str(e)}"
        }, to=user_id)

        return jsonify({
            "success": False, 
            "message": f"❌ خطأ: {str(e)}"
        })

@app.route("/api/verify_code", methods=["POST"])
def api_verify_code():
    # التأكد من وجود user_id في الجلسة
    if 'user_id' not in session:
        return jsonify({
            "success": False, 
            "message": "❌ الجلسة غير صالحة، يرجى إعادة تحميل الصفحة"
        })

    user_id = session['user_id']
    data = request.json

    if not data:
        return jsonify({
            "success": False, 
            "message": "❌ لم يتم إرسال البيانات"
        })

    code = data.get('code')
    password = data.get('password')

    if not code and not password:
        return jsonify({
            "success": False, 
            "message": "❌ يرجى إدخال الكود أو كلمة المرور"
        })

    try:
        if code:
            result = telegram_manager.verify_code(user_id, code)
        else:
            result = telegram_manager.verify_password(user_id, password)

        if result["status"] == "success":
            socketio.emit('log_update', {
                "message": "✅ تم التحقق بنجاح"
            }, to=user_id)

            socketio.emit('connection_status', {
                "status": "connected"
            }, to=user_id)

            return jsonify({
                "success": True, 
                "message": "✅ تم التحقق بنجاح"
            })

        elif result["status"] == "password_required":
            return jsonify({
                "success": True, 
                "message": result["message"], 
                "password_required": True
            })

        else:
            error_message = result.get('message', 'فشل التحقق')
            socketio.emit('log_update', {
                "message": f"❌ {error_message}"
            }, to=user_id)

            return jsonify({
                "success": False, 
                "message": f"❌ {error_message}"
            })

    except Exception as e:
        socketio.emit('log_update', {
            "message": f"❌ خطأ في التحقق: {str(e)}"
        }, to=user_id)

        return jsonify({
            "success": False, 
            "message": f"❌ خطأ: {str(e)}"
        })

@app.route("/api/save_settings", methods=["POST"])
def api_save_settings():
    # التأكد من وجود user_id في الجلسة
    if 'user_id' not in session:
        return jsonify({
            "success": False, 
            "message": "❌ الجلسة غير صالحة، يرجى إعادة تحميل الصفحة"
        })

    user_id = session['user_id']
    data = request.json

    if not data:
        return jsonify({
            "success": False, 
            "message": "❌ لم يتم إرسال البيانات"
        })

    current_settings = load_settings(user_id)
    
    # معالجة إعدادات الرد التلقائي مع التحقق الآمن
    auto_replies = {}
    if 'auto_replies' in data and isinstance(data['auto_replies'], list):
        for entry in data['auto_replies']:
            if isinstance(entry, dict):
                keyword = (entry.get('keyword', '') or '').strip()
                reply = (entry.get('reply', '') or '').strip()
                if keyword and reply:
                    auto_replies[keyword.lower()] = reply

    current_settings.update({
        'message': data.get('message', ''),
        'groups': [g.strip() for g in data.get('groups', '').split('\n') if g.strip()],
        'interval_seconds': int(data.get('interval_seconds', 3600)),
        'watch_words': [w.strip() for w in data.get('watch_words', '').split('\n') if w.strip()],
        'send_type': data.get('send_type', 'manual'),
        'scheduled_time': data.get('scheduled_time', ''),
        'max_retries': int(data.get('max_retries', 5)),
        'auto_reconnect': data.get('auto_reconnect', False),
        'auto_reply_enabled': data.get('auto_reply_enabled', False),
        'auto_replies': auto_replies
    })

    if save_settings(user_id, current_settings):
        with USERS_LOCK:
            if user_id in USERS:
                USERS[user_id]['settings'] = current_settings
                # تحديث إعدادات المراقبة في العميل
                client_manager = USERS[user_id].get('client_manager')
                if client_manager and hasattr(client_manager, 'update_monitoring_settings'):
                    client_manager.update_monitoring_settings(
                        current_settings.get('watch_words', []),
                        current_settings.get('groups', [])
                    )

        auto_reply_msg = "مُفعل" if current_settings.get('auto_reply_enabled', False) else "مُعطل"
        socketio.emit('log_update', {
            "message": f"✅ تم حفظ الإعدادات بنجاح - الرد التلقائي: {auto_reply_msg}"
        }, to=user_id)

        return jsonify({
            "success": True, 
            "message": "✅ تم حفظ الإعدادات"
        })
    else:
        return jsonify({
            "success": False, 
            "message": "❌ فشل في حفظ الإعدادات"
        })

@app.route("/api/user_logout", methods=["POST"])
def api_user_logout():
    """تسجيل الخروج وإنهاء جلسة التليجرام مع الحفاظ على هوية المستخدم"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({
            "success": False,
            "message": "❌ لا توجد جلسة نشطة"
        })

    try:
        logger.info(f"User {user_id} logging out...")

        with USERS_LOCK:
            if user_id in USERS:
                # إيقاف العميل والمراقبة
                client_manager = USERS[user_id].get('client_manager')
                if client_manager:
                    try:
                        # إيقاف المراقبة أولاً
                        if USERS[user_id].get('is_running'):
                            USERS[user_id]['is_running'] = False

                        # قطع الاتصال وإيقاف العميل
                        if hasattr(client_manager, 'client') and client_manager.client:
                            client_manager.client.disconnect()
                            logger.info(f"Client disconnected for user {user_id}")

                        # إيقاف thread إذا كان يعمل
                        if hasattr(client_manager, 'stop'):
                            client_manager.stop()

                    except Exception as e:
                        logger.error(f"خطأ في إغلاق العميل للمستخدم {user_id}: {e}")

                # حذف بيانات المستخدم من الذاكرة
                del USERS[user_id]
                logger.info(f"User data removed from memory for {user_id}")

        # مسح ملفات جلسة التليجرام
        session_file = os.path.join(SESSIONS_DIR, f"{user_id}_session.session")
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
                logger.info(f"Session file removed for {user_id}")
            except Exception as e:
                logger.error(f"خطأ في حذف ملف الجلسة: {e}")

        # مسح إعدادات المستخدم (اختياري - قد تريد الاحتفاظ بها)
        settings_file = os.path.join(SESSIONS_DIR, f"{user_id}.json")
        if os.path.exists(settings_file):
            try:
                # لا نحذف الإعدادات، نفرغ البيانات الحساسة فقط
                settings = load_settings(user_id)
                settings.update({
                    'phone': '',
                    'authenticated': False,
                    'connected': False
                })
                save_settings(user_id, settings)
                logger.info(f"Settings cleared for {user_id}")
            except Exception as e:
                logger.error(f"خطأ في مسح الإعدادات: {e}")

        # إرسال إشعار مسح الجلسة
        socketio.emit('log_update', {
            "message": "🚪 تم تسجيل الخروج وإنهاء جلسة التليجرام"
        }, to=user_id)

        socketio.emit('connection_status', {
            "status": "disconnected"
        }, to=user_id)

        socketio.emit('login_status', {
            "logged_in": False,
            "connected": False,
            "awaiting_code": False,
            "awaiting_password": False,
            "is_running": False
        }, to=user_id)

        # لا نمسح session.clear() بل نحتفظ بهوية المستخدم
        # session.clear()  - لا نستخدم هذا في النظام الجديد

        logger.info(f"User {user_id} logged out successfully")

        return jsonify({
            "success": True,
            "message": "✅ تم تسجيل الخروج وإنهاء جلسة التليجرام بنجاح"
        })

    except Exception as e:
        logger.error(f"خطأ في تسجيل الخروج للمستخدم {user_id}: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"❌ خطأ في تسجيل الخروج: {str(e)}"
        })

@app.route("/api/switch_user", methods=["POST"])
def api_switch_user():
    """التبديل إلى مستخدم آخر مع الحفاظ على استمرارية العمليات لجميع المستخدمين"""
    try:
        data = request.get_json()
        new_user_id = data.get('user_id')

        if not new_user_id or new_user_id not in PREDEFINED_USERS:
            return jsonify({
                "success": False,
                "message": "❌ مستخدم غير صحيح"
            })

        old_user_id = session.get('user_id', 'user_1')

        # الحفاظ على العمليات المستمرة للمستخدم القديم
        # لا نوقف العمليات الجارية، فقط نحفظ الإعدادات
        if old_user_id in USERS:
            current_settings = USERS[old_user_id].get('settings', {})
            if current_settings:
                save_settings(old_user_id, current_settings)
                logger.info(f"✅ Settings saved for user {old_user_id} - Operations continue running")

        # التأكد من وجود بيانات المستخدم الجديد
        with USERS_LOCK:
            if new_user_id not in USERS:
                # تحميل الإعدادات المحفوظة للمستخدم الجديد
                saved_settings = load_settings(new_user_id)

                # إنشاء بيانات للمستخدم الجديد مع الإعدادات المحفوظة
                USERS[new_user_id] = {
                    'client_manager': None,
                    'settings': saved_settings,
                    'thread': None,
                    'is_running': False,
                    'stats': {"sent": 0, "errors": 0},
                    'connected': False,
                    'authenticated': False,
                    'awaiting_code': False,
                    'awaiting_password': False,
                    'phone_code_hash': None,
                    'monitoring_active': False,
                    'event_handlers_registered': False
                }

                # التحقق من وجود جلسة محفوظة للمستخدم الجديد
                session_file = os.path.join(SESSIONS_DIR, f"{new_user_id}_session.session")
                if os.path.exists(session_file) and saved_settings.get('phone'):
                    USERS[new_user_id]['connected'] = True
                    USERS[new_user_id]['authenticated'] = True
                    logger.info(f"Found existing session for user {new_user_id}")
            else:
                # إعادة تحميل الإعدادات للمستخدم الموجود
                saved_settings = load_settings(new_user_id)
                USERS[new_user_id]['settings'].update(saved_settings)

        # تحديث الجلسة فقط للواجهة
        session['user_id'] = new_user_id
        session.permanent = True

        logger.info(f"✅ User switched from {old_user_id} to {new_user_id} - All operations remain active")

        # عرض حالة العمليات المستمرة
        active_operations_summary = get_all_users_operations_status()

        # إرسال الإعدادات الخاصة بالمستخدم الجديد
        socketio.emit('user_settings', USERS[new_user_id]['settings'], to=new_user_id)

        return jsonify({
            "success": True,
            "message": f"✅ تم التبديل إلى {PREDEFINED_USERS[new_user_id]['name']} - العمليات مستمرة للجميع",
            "user": {
                "id": new_user_id,
                "name": PREDEFINED_USERS[new_user_id]['name'],
                "icon": PREDEFINED_USERS[new_user_id]['icon'],
                "color": PREDEFINED_USERS[new_user_id]['color']
            },
            "settings": USERS[new_user_id]['settings'],
            "active_operations": active_operations_summary
        })

    except Exception as e:
        logger.error(f"Error in user switching API: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"❌ خطأ في التبديل: {str(e)}"
        })

@app.route("/api/start_monitoring", methods=["POST"])
def api_start_monitoring():
    # التأكد من وجود user_id في الجلسة
    if 'user_id' not in session:
        return jsonify({
            "success": False, 
            "message": "❌ الجلسة غير صالحة، يرجى إعادة تحميل الصفحة"
        })

    user_id = session['user_id']

    with USERS_LOCK:
        if user_id not in USERS:
            return jsonify({
                "success": False, 
                "message": "❌ لم يتم إعداد الحساب"
            })

        if not USERS[user_id].get('authenticated'):
            return jsonify({
                "success": False, 
                "message": "❌ يجب تسجيل الدخول أولاً"
            })

        if USERS[user_id]['is_running']:
            return jsonify({
                "success": False, 
                "message": "✅ النظام يعمل بالفعل"
            })

        USERS[user_id]['is_running'] = True

    socketio.emit('log_update', {
        "message": "🚀 بدء تشغيل نظام المراقبة المحسن مع Event Handlers..."
    }, to=user_id)

    try:
        monitoring_thread = threading.Thread(
            target=monitoring_worker, 
            args=(user_id,), 
            daemon=True
        )
        monitoring_thread.start()

        with USERS_LOCK:
            USERS[user_id]['thread'] = monitoring_thread

        # إرسال تحديث حالة المراقبة للواجهة
        socketio.emit('monitoring_status', {
            "monitoring_active": True,
            "status": "running",
            "is_running": True
        }, to=user_id)

        # إرسال تحديث الأزرار
        socketio.emit('update_monitoring_buttons', {
            "is_running": True
        }, to=user_id)

        return jsonify({
            "success": True, 
            "message": "🚀 بدأت المراقبة المحسنة مع Event Handlers"
        })

    except Exception as e:
        logger.error(f"Failed to start monitoring for {user_id}: {str(e)}")

        with USERS_LOCK:
            USERS[user_id]['is_running'] = False

        return jsonify({
            "success": False, 
            "message": f"❌ فشل في بدء المراقبة: {str(e)}"
        })

@app.route("/api/stop_monitoring", methods=["POST"])
def api_stop_monitoring():
    # التأكد من وجود user_id في الجلسة
    if 'user_id' not in session:
        return jsonify({
            "success": False, 
            "message": "❌ الجلسة غير صالحة، يرجى إعادة تحميل الصفحة"
        })

    user_id = session['user_id']

    with USERS_LOCK:
        if user_id in USERS and USERS[user_id]['is_running']:
            USERS[user_id]['is_running'] = False
            socketio.emit('log_update', {
                "message": "⏹ إيقاف نظام المراقبة..."
            }, to=user_id)

            # إرسال تحديث حالة المراقبة للواجهة
            socketio.emit('monitoring_status', {
                "monitoring_active": False,
                "status": "stopped",
                "is_running": False
            }, to=user_id)

            # إرسال تحديث الأزرار
            socketio.emit('update_monitoring_buttons', {
                "is_running": False
            }, to=user_id)

            return jsonify({
                "success": True, 
                "message": "⏹ تم إيقاف المراقبة"
            })

    return jsonify({
        "success": False, 
        "message": "❌ النظام غير مشغل"
    })

@app.route("/api/send_now", methods=["POST"])
def api_send_now():
    # التأكد من وجود user_id في الجلسة
    if 'user_id' not in session:
        return jsonify({
            "success": False, 
            "message": "❌ الجلسة غير صالحة، يرجى إعادة تحميل الصفحة"
        })

    user_id = session['user_id']

    with USERS_LOCK:
        if user_id not in USERS:
            return jsonify({
                "success": False, 
                "message": "❌ لم يتم إعداد الحساب"
            })

        if not USERS[user_id].get('authenticated'):
            return jsonify({
                "success": False, 
                "message": "❌ يجب تسجيل الدخول أولاً"
            })

    # قراءة البيانات من الطلب المرسل من JavaScript
    data = request.get_json()
    if not data:
        return jsonify({
            "success": False, 
            "message": "❌ لا توجد بيانات مرسلة"
        })

    message = data.get('message', '').strip()
    groups = data.get('groups', '').strip()
    images = data.get('images', [])

    # التحقق من وجود محتوى للإرسال
    if not message and not images:
        return jsonify({
            "success": False, 
            "message": "❌ يجب كتابة رسالة أو رفع صورة للإرسال"
        })

    if not groups:
        return jsonify({
            "success": False, 
            "message": "❌ يجب تحديد المجموعات للإرسال إليها"
        })

    # تحويل النص إلى قائمة مجموعات
    groups_list = [g.strip() for g in groups.replace('\n', ',').split(',') if g.strip()]

    if not groups_list:
        return jsonify({
            "success": False, 
            "message": "❌ يجب تحديد مجموعة واحدة على الأقل"
        })

    # تحضير الصور إذا وجدت
    image_files = []
    if images:
        try:
            import base64
            import tempfile

            for img_data in images:
                # استخراج البيانات من Base64
                base64_data = img_data['data'].split(',')[1]  # إزالة البادئة
                image_bytes = base64.b64decode(base64_data)

                # إنشاء ملف مؤقت
                temp_file = tempfile.NamedTemporaryFile(delete=False, 
                                                     suffix=f".{img_data['type'].split('/')[-1]}")
                temp_file.write(image_bytes)
                temp_file.flush()

                image_files.append({
                    'path': temp_file.name,
                    'name': img_data['name'],
                    'type': img_data['type']
                })

            socketio.emit('log_update', {
                "message": f"📷 تم تحضير {len(image_files)} صورة للإرسال"
            }, to=user_id)

        except Exception as e:
            logger.error(f"Error processing images: {str(e)}")
            return jsonify({
                "success": False,
                "message": f"❌ خطأ في معالجة الصور: {str(e)}"
            })

    content_type = "رسالة"
    if images and message:
        content_type = f"رسالة مع {len(images)} صورة"
    elif images:
        content_type = f"{len(images)} صورة"

    socketio.emit('log_update', {
        "message": f"🚀 بدء الإرسال الفوري: {content_type} إلى {len(groups_list)} مجموعة"
    }, to=user_id)

    def send_messages_with_images():
        try:
            successful = 0
            failed = 0

            for i, group in enumerate(groups_list, 1):
                try:
                    if images and message:
                        # إرسال الصور مع النص
                        result = telegram_manager.send_message_with_media_async(
                            user_id, group, message, image_files
                        )
                    elif images:
                        # إرسال الصور فقط
                        result = telegram_manager.send_media_async(
                            user_id, group, image_files
                        )
                    else:
                        # إرسال النص فقط
                        result = telegram_manager.send_message_async(user_id, group, message)

                    socketio.emit('log_update', {
                        "message": f"✅ [{i}/{len(groups_list)}] نجح إلى: {group}"
                    }, to=user_id)

                    successful += 1
                    with USERS_LOCK:
                        if user_id in USERS:
                            USERS[user_id]['stats']['sent'] += 1

                    socketio.emit('stats_update', USERS[user_id]['stats'], to=user_id)

                    if i < len(groups_list):
                        time.sleep(3)

                except Exception as e:
                    error_msg = str(e)
                    if "banned" in error_msg.lower():
                        error_type = "محظور"
                    elif "private" in error_msg.lower():
                        error_type = "خاص/محدود"
                    elif "can't write" in error_msg.lower():
                        error_type = "غير مسموح"
                    else:
                        error_type = "خطأ"

                    logger.error(f"Send error to {group}: {error_msg}")
                    socketio.emit('log_update', {
                        "message": f"❌ [{i}/{len(groups_list)}] فشل إلى {group}: {error_type}"
                    }, to=user_id)

                    failed += 1
                    with USERS_LOCK:
                        if user_id in USERS:
                            USERS[user_id]['stats']['errors'] += 1

                    socketio.emit('stats_update', USERS[user_id]['stats'], to=user_id)

            # ملخص نهائي
            socketio.emit('log_update', {
                "message": f"📊 انتهى الإرسال: ✅ {successful} نجح | ❌ {failed} فشل"
            }, to=user_id)

        except Exception as e:
            logger.error(f"Send thread error: {str(e)}")
        finally:
            # تنظيف الملفات المؤقتة
            for img_file in image_files:
                try:
                    if os.path.exists(img_file['path']):
                        os.unlink(img_file['path'])
                        logger.info(f"Cleaned up temp file: {img_file['name']}")
                except Exception as e:
                    logger.error(f"Error cleaning temp file {img_file.get('name', 'unknown')}: {str(e)}")

    threading.Thread(target=send_messages_with_images, daemon=True).start()

    return jsonify({
        "success": True, 
        "message": f"🚀 بدأ إرسال {content_type} لـ {len(groups_list)} مجموعة"
    })

@app.route("/api/get_stats", methods=["GET"])
def api_get_stats():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"sent": 0, "errors": 0})

    with USERS_LOCK:
        if user_id in USERS:
            return jsonify(USERS[user_id]['stats'])

    return jsonify({"sent": 0, "errors": 0})

@app.route("/api/get_login_status", methods=["GET"])
def api_get_login_status():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"logged_in": False, "connected": False})

    with USERS_LOCK:
        if user_id in USERS:
            # التحقق من وجود جلسة محفوظة وعميل متصل
            user_data = USERS[user_id]
            client_manager = user_data.get('client_manager')
            authenticated = user_data.get('authenticated', False)
            connected = user_data.get('connected', False)

            # تحقق إضافي من وجود جلسة محفوظة إذا لم يكن authenticated
            if not authenticated and 'settings' in user_data and 'phone' in user_data['settings']:
                session_file = os.path.join(SESSIONS_DIR, f"{user_id}_session.session")
                if os.path.exists(session_file):
                    # يوجد ملف جلسة محفوظ، اعتبر المستخدم مسجل دخول
                    authenticated = True
                    connected = True
                    # تحديث حالة المستخدم
                    USERS[user_id]['authenticated'] = True
                    USERS[user_id]['connected'] = True

            return jsonify({
                "logged_in": authenticated, 
                "connected": connected,
                "is_running": user_data.get('is_running', False)
            })

    return jsonify({"logged_in": False, "connected": False, "is_running": False})

@app.route("/api/get_user_info", methods=["GET"])
def api_get_user_info():
    """جلب معلومات المستخدم الحالي"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"success": False, "message": "غير مسجل دخول"})

    with USERS_LOCK:
        if user_id in USERS and 'settings' in USERS[user_id]:
            settings = USERS[user_id]['settings']
            return jsonify({
                "success": True,
                "phone": settings.get('phone', ''),
                "name": settings.get('name', ''),
                "user_id": user_id[:8] + "..."  # عرض جزء من معرف المستخدم للأمان
            })

    return jsonify({"success": False, "message": "لم يتم العثور على معلومات المستخدم"})

@app.route("/api/reset_login", methods=["POST"])
def api_reset_login():
    """إعادة تعيين جلسة تسجيل الدخول للمستخدم الحالي"""
    user_id = session.get('user_id', 'user_1')

    if user_id not in PREDEFINED_USERS:
        return jsonify({
            "success": False,
            "message": "❌ مستخدم غير صحيح"
        })

    try:
        logger.info(f"Resetting login for user {user_id}")

        with USERS_LOCK:
            if user_id in USERS:
                # إيقاف المراقبة إذا كانت تعمل
                if USERS[user_id].get('is_running', False):
                    USERS[user_id]['is_running'] = False

                # إيقاف العميل
                client_manager = USERS[user_id].get('client_manager')
                if client_manager:
                    try:
                        if hasattr(client_manager, 'stop'):
                            client_manager.stop()
                        if hasattr(client_manager, 'client') and client_manager.client:
                            client_manager.client.disconnect()
                        logger.info(f"Client stopped and disconnected for user {user_id}")
                    except Exception as e:
                        logger.error(f"Error stopping client for {user_id}: {e}")

                # حذف بيانات المستخدم من الذاكرة
                del USERS[user_id]
                logger.info(f"User data removed from memory for {user_id}")

        # مسح ملف جلسة التليجرام
        session_file = os.path.join(SESSIONS_DIR, f"{user_id}_session.session")
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
                logger.info(f"Session file removed for {user_id}")
            except Exception as e:
                logger.error(f"Failed to remove session file for {user_id}: {str(e)}")

        # مسح إعدادات المستخدم (اختياري - قد تريد الاحتفاظ بها)
        settings_file = os.path.join(SESSIONS_DIR, f"{user_id}.json")
        if os.path.exists(settings_file):
            try:
                # لا نحذف الإعدادات، نفرغ البيانات الحساسة فقط
                settings = load_settings(user_id)
                settings.update({
                    'phone': '',
                    'authenticated': False,
                    'connected': False
                })
                save_settings(user_id, settings)
                logger.info(f"Settings cleared for {user_id}")
            except Exception as e:
                logger.error(f"خطأ في مسح الإعدادات: {e}")

        # إرسال إشعارات التحديث
        socketio.emit('log_update', {
            "message": f"🔄 تم إعادة تعيين جلسة تسجيل الدخول لـ {PREDEFINED_USERS[user_id]['name']}"
        }, to=user_id)

        socketio.emit('connection_status', {
            "status": "disconnected"
        }, to=user_id)

        socketio.emit('login_status', {
            "logged_in": False,
            "connected": False,
            "awaiting_code": False,
            "awaiting_password": False,
            "is_running": False
        }, to=user_id)

        logger.info(f"Login reset completed for user {user_id}")

        return jsonify({
            "success": True, 
            "message": f"✅ تم إعادة تعيين جلسة {PREDEFINED_USERS[user_id]['name']} بنجاح"
        })

    except Exception as e:
        logger.error(f"Error resetting login for {user_id}: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"❌ خطأ في إعادة التعيين: {str(e)}"
        })

# =========================== 
# Admin Routes for Temporary Links
# ===========================
@app.route("/admin")
def admin_panel():
    """لوحة تحكم الأدمن"""
    # التحقق من كلمة مرور الأدمن (يمكن تحسينها)
    admin_password = request.args.get('pass')
    if admin_password != 'admin123':  # يمكن تغيير كلمة المرور
        return jsonify({"error": "Unauthorized"}), 401

    # الحصول على جميع الروابط المؤقتة
    with TEMP_LINKS_LOCK:
        temp_links = []
        current_time = time.time()

        for token, info in TEMP_LINKS.items():
            remaining_hours = max(0, (info['expires_at'] - current_time) / 3600)
            temp_links.append({
                'token': token,
                'duration_hours': info['duration_hours'],
                'remaining_hours': round(remaining_hours, 2),
                'is_active': info['is_active'] and current_time < info['expires_at'],
                'created_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(info['created_at'])),
                'expires_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(info['expires_at'])),
                'full_url': f"{request.host_url}temp/{token}"
            })

    return render_template('admin.html', temp_links=temp_links)

@app.route("/api/admin/create_temp_link", methods=["POST"])
def api_create_temp_link():
    """إنشاء رابط مؤقت جديد"""
    try:
        data = request.json
        duration_hours = int(data.get('duration_hours', 1))

        if duration_hours < 1 or duration_hours > 72:  # حد أقصى 72 ساعة
            return jsonify({
                "success": False,
                "message": "المدة يجب أن تكون بين 1 و 72 ساعة"
            })

        token = create_temp_link(duration_hours)
        full_url = f"{request.host_url}temp/{token}"

        logger.info(f"Created temporary link: {token} for {duration_hours} hours")

        return jsonify({
            "success": True,
            "token": token,
            "full_url": full_url,
            "duration_hours": duration_hours,
            "expires_at": time.strftime('%Y-%m-%d %H:%M:%S', 
                                      time.localtime(time.time() + duration_hours * 3600))
        })

    except Exception as e:
        logger.error(f"Error creating temp link: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"خطأ: {str(e)}"
        })

@app.route("/api/admin/deactivate_temp_link", methods=["POST"])
def api_deactivate_temp_link():
    """إلغاء تفعيل رابط مؤقت"""
    try:
        data = request.json
        token = data.get('token')

        with TEMP_LINKS_LOCK:
            if token in TEMP_LINKS:
                TEMP_LINKS[token]['is_active'] = False
                logger.info(f"Deactivated temporary link: {token}")
                return jsonify({
                    "success": True,
                    "message": "تم إلغاء تفعيل الرابط بنجاح"
                })
            else:
                return jsonify({
                    "success": False,
                    "message": "الرابط غير موجود"
                })

    except Exception as e:
        logger.error(f"Error deactivating temp link: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"خطأ: {str(e)}"
        })

# =========================== 
# Keep-Alive API
# ===========================
@app.route("/api/keep_alive_status", methods=["GET"])
def api_keep_alive_status():
    """الحصول على حالة نظام Keep-Alive"""
    try:
        from keep_alive import get_keep_alive_status
        status = get_keep_alive_status()
        return jsonify({
            "success": True,
            "status": status
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"خطأ: {str(e)}"
        })

@app.route("/api/system_health", methods=["GET"])
def api_system_health():
    """فحص صحة النظام"""
    try:
        import psutil

        # معلومات الذاكرة
        memory = psutil.virtual_memory()

        # معلومات القرص
        disk = psutil.disk_usage('/')

        # معلومات الـ CPU
        cpu_percent = psutil.cpu_percent(interval=1)

        # معلومات الشبكة
        network = psutil.net_io_counters()

        health_info = {
            'memory': {
                'total': memory.total,
                'available': memory.available,
                'percent': memory.percent,
                'used': memory.used
            },
            'disk': {
                'total': disk.total,
                'used': disk.used,
                'free': disk.free,
                'percent': (disk.used / disk.total) * 100
            },
            'cpu': {
                'percent': cpu_percent,
                'count': psutil.cpu_count()
            },
            'network': {
                'bytes_sent': network.bytes_sent,
                'bytes_recv': network.bytes_recv
            },
            'timestamp': time.time()
        }

        return jsonify({
            "success": True,
            "health": health_info
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"خطأ: {str(e)}"
        })


# =========================== 
# نظام الانضمام التلقائي للمجموعات
# ===========================

import re
from datetime import datetime, timedelta
from telethon.tl.types import Channel, Chat, User
from telethon.tl.functions.contacts import SearchRequest, ResolveUsernameRequest
from telethon.tl.functions.messages import SearchGlobalRequest

def extract_telegram_links(text):
    """استخراج روابط التليجرام من النص مع التنظيف والفلترة"""
    if not text:
        return []

    # أنماط شاملة لروابط التليجرام
    patterns = [
        # روابط عادية
        r'https?://t\.me/([a-zA-Z0-9_]+)(?:/\d+)?',           # https://t.me/channel أو https://t.me/channel/123
        r'https?://telegram\.me/([a-zA-Z0-9_]+)(?:/\d+)?',    # https://telegram.me/channel

        # روابط الدعوة
        r'https?://t\.me/\+([a-zA-Z0-9_\-]+)',                # https://t.me/+inviteHash
        r'https?://telegram\.me/\+([a-zA-Z0-9_\-]+)',         # https://telegram.me/+inviteHash

        # روابط بدون بروتوكول
        r't\.me/([a-zA-Z0-9_]+)',                             # t.me/channel
        r't\.me/\+([a-zA-Z0-9_\-]+)',                        # t.me/+inviteHash
        r'telegram\.me/([a-zA-Z0-9_]+)',                      # telegram.me/channel

        # أسماء المستخدمين والقنوات
        r'@([a-zA-Z0-9_]{5,})',                              # @channel (أكثر من 4 أحرف)
    ]

    found_links = set()

    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            clean_match = match if isinstance(match, str) else match[0] if match else ''

            # تنسيق الرابط
            if pattern.startswith(r'@'):
                # اسم المستخدم
                clean_link = f"https://t.me/{clean_match}"
            elif '+' in clean_match or pattern.find(r'\+') != -1:
                # رابط دعوة
                clean_link = f"https://t.me/+{clean_match.replace('+', '')}"
            elif clean_match and not clean_match.startswith('http'):
                # رابط بدون بروتوكول
                clean_link = f"https://t.me/{clean_match}"
            elif clean_match.startswith('http'):
                # رابط كامل
                clean_link = f"https://t.me/{clean_match.split('/')[-1]}"
            else:
                clean_link = clean_match

            # التحقق من صحة الرابط
            if clean_link and len(clean_link) > 15:  # على الأقل https://t.me/x
                # إزالة أي معاملات إضافية
                clean_link = clean_link.split('?')[0].split('#')[0]
                found_links.add(clean_link)

    # تحويل إلى قائمة مع ترتيب
    links_list = sorted(list(found_links))

    # إنشاء كائنات الروابط مع معلومات إضافية
    result_links = []
    for link in links_list:
        username = link.split('/')[-1].replace('@', '')
        result_links.append({
            'url': link,
            'username': username,
            'type': 'invite' if '+' in link else 'channel'
        })

    return result_links

async def join_telegram_group(client, group_link):
    """الانضمام لمجموعة تليجرام"""
    try:
        # تنظيف الرابط
        if group_link.startswith('https://t.me/'):
            group_identifier = group_link.replace('https://t.me/', '')
        elif group_link.startswith('https://telegram.me/'):
            group_identifier = group_link.replace('https://telegram.me/', '')
        elif group_link.startswith('@'):
            group_identifier = group_link[1:]
        else:
            group_identifier = group_link

        # محاولة الانضمام
        try:
            # التحقق من الحالة أولاً
            entity = await client.get_entity(group_identifier)

            # محاولة الانضمام مباشرة (سنتعامل مع الاستثناءات)
            if hasattr(entity, 'megagroup') or hasattr(entity, 'broadcast'):
                # قناة أو مجموعة كبيرة
                result = await client(functions.channels.JoinChannelRequest(entity))
            else:
                # مجموعة عادية - سنحاول الانضمام من خلال رابط دعوة
                raise Exception("مجموعة عادية - يجب استخدام رابط دعوة")

            return {
                "success": True,
                "already_joined": False,
                "message": "تم الانضمام بنجاح"
            }

        except UserAlreadyParticipantError:
            return {
                "success": True,
                "already_joined": True,
                "message": "منضم مسبقاً للمجموعة"
            }

        except FloodWaitError as e:
            return {
                "success": False,
                "message": f"يرجى الانتظار {e.seconds} ثانية"
            }

        except InviteHashExpiredError:
            return {
                "success": False,
                "message": "انتهت صلاحية رابط الدعوة"
            }

        except InviteHashInvalidError:
            return {
                "success": False,
                "message": "رابط الدعوة غير صحيح"
            }

        except Exception as group_error:
            # محاولة أخرى مع تعديل الرابط
            try:
                if '/' in group_identifier:
                    # قد يكون رابط دعوة
                    result = await client(functions.messages.ImportChatInviteRequest(group_identifier.split('/')[-1]))
                    return {
                        "success": True,
                        "already_joined": False,
                        "message": "تم الانضمام عبر رابط الدعوة"
                    }
                else:
                    raise group_error
            except UserAlreadyParticipantError:
                return {
                    "success": True,
                    "already_joined": True,
                    "message": "منضم مسبقاً للمجموعة"
                }
            except Exception as final_error:
                return {
                    "success": False,
                    "message": f"فشل الانضمام: {str(final_error)}"
                }

    except Exception as e:
        return {
            "success": False,
            "message": f"خطأ: {str(e)}"
        }

# =========================== 
# API للانضمام التلقائي
# ===========================
@app.route("/api/extract_group_links", methods=["POST"])
def api_extract_group_links():
    """استخراج روابط المجموعات من النص"""
    try:
        data = request.json
        if not data or not data.get('text'):
            return jsonify({
                "success": False,
                "message": "❌ لم يتم إرسال النص"
            })

        text = data.get('text', '')
        links = extract_telegram_links(text)

        return jsonify({
            "success": True,
            "links": links,
            "count": len(links),
            "message": f"✅ تم استخراج {len(links)} رابط"
        })

    except Exception as e:
        logger.error(f"Error extracting links: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"❌ خطأ: {str(e)}"
        })

@app.route("/api/join_group", methods=["POST"])
def api_join_group():
    """الانضمام لمجموعة واحدة"""
    try:
        user_id = session.get('user_id', 'user_1')

        if user_id not in PREDEFINED_USERS:
            return jsonify({
                "success": False,
                "message": "❌ مستخدم غير صحيح"
            })

        data = request.json

        if not data or not data.get('group_link'):
            return jsonify({
                "success": False,
                "message": "❌ لم يتم إرسال رابط المجموعة"
            })

        group_link_raw = data.get('group_link', '')
        if isinstance(group_link_raw, dict):
            # إذا كان group_link عبارة عن dict، استخرج الرابط منه
            group_link = group_link_raw.get('url', '') or group_link_raw.get('link', '') or str(group_link_raw)
        else:
            group_link = str(group_link_raw)

        group_link = group_link.strip()

        with USERS_LOCK:
            if user_id not in USERS:
                return jsonify({
                    "success": False,
                    "message": f"❌ المستخدم {PREDEFINED_USERS[user_id]['name']} غير مسجل"
                })

            client_manager = USERS[user_id].get('client_manager')
            if not client_manager or not client_manager.client:
                return jsonify({
                    "success": False,
                    "message": "❌ يرجى تسجيل الدخول أولاً"
                })

        # تشغيل عملية الانضمام
        result = client_manager.run_coroutine(
            join_telegram_group(client_manager.client, group_link)
        )

        # تسجيل النتيجة
        socketio.emit('log_update', {
            "message": f"{'✅' if result['success'] else '❌'} {group_link}: {result['message']}"
        }, to=user_id)

        return jsonify(result)

    except Exception as e:
        logger.error(f"Error joining group: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"❌ خطأ: {str(e)}"
        })

@app.route("/api/start_auto_join", methods=["POST"])
def api_start_auto_join():
    """بدء الانضمام التلقائي المتعدد للمجموعات"""
    try:
        user_id = session.get('user_id', 'user_1')

        if user_id not in PREDEFINED_USERS:
            return jsonify({
                "success": False,
                "message": "❌ مستخدم غير صحيح"
            })

        data = request.json
        if not data or not data.get('links'):
            return jsonify({
                "success": False,
                "message": "❌ لم يتم إرسال روابط المجموعات"
            })

        links = data.get('links', [])
        delay = data.get('delay', 3)  # تأخير افتراضي 3 ثواني

        if not links:
            return jsonify({
                "success": False,
                "message": "❌ لا توجد روابط للانضمام إليها"
            })

        with USERS_LOCK:
            if user_id not in USERS:
                return jsonify({
                    "success": False,
                    "message": f"❌ المستخدم {PREDEFINED_USERS[user_id]['name']} غير مسجل"
                })

            client_manager = USERS[user_id].get('client_manager')
            if not client_manager or not client_manager.client:
                return jsonify({
                    "success": False,
                    "message": "❌ يرجى تسجيل الدخول أولاً"
                })

        # بدء عملية الانضمام التلقائي في thread منفصل
        import threading

        def auto_join_worker():
            success_count = 0
            fail_count = 0
            already_joined_count = 0

            socketio.emit('log_update', {
                "message": f"🚀 بدء الانضمام التلقائي لـ {len(links)} مجموعة..."
            }, to=user_id)

            for i, link_obj in enumerate(links):
                try:
                    # الحصول على الرابط
                    if isinstance(link_obj, dict):
                        group_link = link_obj.get('url', '') or link_obj.get('link', '') or str(link_obj)
                    else:
                        group_link = str(link_obj)

                    group_link = group_link.strip()

                    # إرسال حالة التقدم
                    socketio.emit('join_progress', {
                        'current': i + 1,
                        'total': len(links),
                        'link': group_link
                    }, to=user_id)

                    # محاولة الانضمام
                    result = client_manager.run_coroutine(
                        join_telegram_group(client_manager.client, group_link)
                    )

                    if result['success']:
                        if result.get('already_joined', False):
                            already_joined_count += 1
                            socketio.emit('log_update', {
                                "message": f"ℹ️ منضم مسبقاً: {group_link}"
                            }, to=user_id)
                        else:
                            success_count += 1
                            socketio.emit('log_update', {
                                "message": f"✅ تم الانضمام: {group_link}"
                            }, to=user_id)
                    else:
                        fail_count += 1
                        socketio.emit('log_update', {
                            "message": f"❌ فشل: {group_link} - {result['message']}"
                        }, to=user_id)

                    # تحديث الإحصائيات
                    socketio.emit('join_stats', {
                        'success': success_count,
                        'fail': fail_count,
                        'already_joined': already_joined_count
                    }, to=user_id)

                    # تأخير بين المجموعات لتجنب flood
                    if i < len(links) - 1:  # لا نؤخر بعد آخر مجموعة
                        time.sleep(delay)

                except Exception as e:
                    fail_count += 1
                    socketio.emit('log_update', {
                        "message": f"❌ خطأ في {group_link}: {str(e)}"
                    }, to=user_id)

            # إرسال النتيجة النهائية
            socketio.emit('auto_join_completed', {
                'success': success_count,
                'fail': fail_count,
                'already_joined': already_joined_count,
                'total': len(links)
            }, to=user_id)

            socketio.emit('log_update', {
                "message": f"🎉 انتهى الانضمام التلقائي! النجح: {success_count}, فشل: {fail_count}, منضم مسبقاً: {already_joined_count}"
            }, to=user_id)

        # تشغيل العملية في thread منفصل
        thread = threading.Thread(target=auto_join_worker, daemon=True)
        thread.start()

        return jsonify({
            "success": True,
            "message": f"✅ تم بدء الانضمام التلقائي لـ {len(links)} مجموعة",
            "total_links": len(links)
        })

    except Exception as e:
        logger.error(f"Error starting auto join: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"❌ خطأ في بدء الانضمام التلقائي: {str(e)}"
        })

# ==========================
# APIs البحث عن الروابط
# ==========================

import re
from datetime import datetime, timedelta
from telethon.tl.types import Channel, Chat, User
from telethon.tl.functions.contacts import SearchRequest, ResolveUsernameRequest
from telethon.tl.functions.messages import SearchGlobalRequest

def extract_telegram_links(text):
    """استخراج روابط التليجرام من النص"""
    if not text:
        return []

    # أنماط الروابط المختلفة (شامل وقوي)
    patterns = [
        # روابط عادية
        r'https?://t\.me/([a-zA-Z0-9_]+)',           # https://t.me/channel
        r'https?://telegram\.me/([a-zA-Z0-9_]+)',    # https://telegram.me/channel

        # روابط الدعوة (invite links)
        r'https?://t\.me/\+([a-zA-Z0-9_\-]+)',       # https://t.me/+inviteHash
        r'https?://telegram\.me/\+([a-zA-Z0-9_\-]+)', # https://telegram.me/+inviteHash

        # روابط الرسائل في القنوات الخاصة
        r'https?://t\.me/c/(\d+)/(\d+)',             # https://t.me/c/channelid/messageid
        r'https?://telegram\.me/c/(\d+)/(\d+)',      # https://telegram.me/c/channelid/messageid

        # روابط الرسائل في القنوات العامة
        r'https?://t\.me/([a-zA-Z0-9_]+)/(\d+)',     # https://t.me/channel/messageid
        r'https?://telegram\.me/([a-zA-Z0-9_]+)/(\d+)', # https://telegram.me/channel/messageid

        # ذكر المستخدمين والقنوات
        r'@([a-zA-Z0-9_]+)',                         # @channel

        # روابط بدون بروتوكول
        r't\.me/([a-zA-Z0-9_]+)',                    # t.me/channel
        r't\.me/\+([a-zA-Z0-9_\-]+)',               # t.me/+inviteHash
        r'telegram\.me/([a-zA-Z0-9_]+)',             # telegram.me/channel
        r'telegram\.me/\+([a-zA-Z0-9_\-]+)',        # telegram.me/+inviteHash
    ]

    links = []
    seen_urls = set()

    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            if isinstance(match, tuple):
                # التعامل مع التطابقات المتعددة (مثل channel/message)
                if len(match) == 2 and match[1].isdigit():
                    # رابط رسالة
                    if pattern.startswith(r'https?://t\.me/c/'):
                        clean_link = f"https://t.me/c/{match[0]}/{match[1]}"
                        username = f"c/{match[0]}"
                    else:
                        clean_link = f"https://t.me/{match[0]}/{match[1]}"
                        username = match[0]
                else:
                    # رابط دعوة أو قناة خاصة
                    if '+' in str(match[0]) or 'c/' in str(match[0]):
                        clean_link = f"https://t.me/+{match[0]}" if not match[0].startswith('c/') else f"https://t.me/c/{match[0]}"
                        username = match[0]
                    else:
                        clean_link = f"https://t.me/{match[0]}"
                        username = match
            else:
                # تطابق واحد
                if match.startswith('+'):
                    # رابط دعوة
                    clean_link = f"https://t.me/{match}"
                    username = match[1:]  # إزالة علامة +
                elif match.startswith('@'):
                    # ذكر مستخدم/قناة
                    clean_link = f"https://t.me/{match[1:]}"
                    username = match[1:]
                else:
                    # قناة أو مستخدم عادي
                    clean_link = f"https://t.me/{match}"
                    username = match

            # تجنب التكرار
            if clean_link not in seen_urls:
                seen_urls.add(clean_link)
                links.append({
                    'url': clean_link,
                    'original_text': text[:200] + ('...' if len(text) > 200 else ''),
                    'username': username.replace('@', '') if isinstance(username, str) else str(username)
                })

    return links

@app.route("/api/search_my_links", methods=["POST"])
def api_search_my_links():
    """البحث عن روابط التليجرام في محادثات المستخدم"""
    try:
        if 'user_id' not in session:
            return jsonify({
                "success": False,
                "message": "❌ يرجى تسجيل الدخول أولاً"
            })

        user_id = session['user_id']
        data = request.json

        # الحصول على عدد الأيام (افتراضي: شهرين)
        days = data.get('days', 60)
        if days <= 0 or days > 365:  # حد أقصى سنة واحدة
            days = 60

        with USERS_LOCK:
            if user_id not in USERS:
                return jsonify({
                    "success": False,
                    "message": "❌ المستخدم غير مسجل"
                })

            client_manager = USERS[user_id].get('client_manager')
            if not client_manager or not client_manager.client:
                return jsonify({
                    "success": False,
                    "message": "❌ يرجى تسجيل الدخول أولاً"
                })

        logger.info(f"🔍 بدء البحث عن الروابط للمستخدم {user_id} لمدة {days} يوم")

        # حساب التاريخ المحدد
        since_date = datetime.now() - timedelta(days=days)

        # تشغيل البحث
        result = client_manager.run_coroutine(
            search_links_in_chats(client_manager.client, since_date)
        )

        logger.info(f"✅ تم العثور على {len(result)} رابط للمستخدم {user_id}")

        return jsonify({
            "success": True,
            "links": result,
            "message": f"تم العثور على {len(result)} رابط"
        })

    except Exception as e:
        logger.error(f"خطأ في البحث عن الروابط: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"❌ خطأ في البحث: {str(e)}"
        })

async def search_links_in_chats(client, since_date):
    """البحث عن الروابط في جميع المحادثات"""
    found_links = []

    try:
        # الحصول على جميع المحادثات
        async for dialog in client.iter_dialogs():
            try:
                # تخطي المحادثات المحذوفة
                if not dialog.entity:
                    continue

                chat_title = dialog.title or "محادثة غير معروفة"

                # البحث في رسائل هذه المحادثة
                async for message in client.iter_messages(
                    dialog, 
                    offset_date=since_date,
                    limit=1000  # حد أقصى لتجنب التحميل المفرط
                ):
                    if message.text:
                        # استخراج الروابط من النص
                        links = extract_telegram_links(message.text)

                        for link in links:
                            # الحصول على معلومات القناة إن أمكن
                            title = await get_channel_title(client, link['username'])

                            found_links.append({
                                'url': link['url'],
                                'title': title or link['username'],
                                'date': message.date.strftime('%Y-%m-%d %H:%M'),
                                'chat_title': chat_title,
                                'original_text': link['original_text']
                            })

                # حد أقصى للمحادثات المفحوصة لتجنب الإبطاء
                if len(found_links) > 500:
                    break

            except Exception as e:
                logger.warning(f"تخطي محادثة بسبب خطأ: {str(e)}")
                continue

    except Exception as e:
        logger.error(f"خطأ في البحث عن الروابط: {str(e)}")

    # إزالة الروابط المكررة وترتيبها حسب التاريخ
    unique_links = []
    seen_urls = set()

    for link in found_links:
        if link['url'] not in seen_urls:
            seen_urls.add(link['url'])
            unique_links.append(link)

    # ترتيب حسب التاريخ (الأحدث أولاً)
    unique_links.sort(key=lambda x: x['date'], reverse=True)

    return unique_links

async def get_channel_title(client, username):
    """الحصول على عنوان القناة من username"""
    try:
        if username.startswith('@'):
            username = username[1:]

        entity = await client.get_entity(username)
        return entity.title if hasattr(entity, 'title') else username
    except Exception:
        return None

@app.route("/api/search_public_channels", methods=["POST"])
def api_search_public_channels():
    """البحث العام في التليجرام عن القنوات والمجموعات"""
    try:
        if 'user_id' not in session:
            return jsonify({
                "success": False,
                "message": "❌ يرجى تسجيل الدخول أولاً"
            })

        user_id = session['user_id']
        data = request.json

        query = data.get('query', '').strip()
        if not query:
            return jsonify({
                "success": False,
                "message": "❌ يرجى كتابة نص للبحث"
            })

        # تحديد عدد النتائج المطلوبة
        limit = min(data.get('limit', 50), 100)  # حد أقصى 100

        with USERS_LOCK:
            if user_id not in USERS:
                return jsonify({
                    "success": False,
                    "message": "❌ المستخدم غير مسجل"
                })

            client_manager = USERS[user_id].get('client_manager')
            if not client_manager or not client_manager.client:
                return jsonify({
                    "success": False,
                    "message": "❌ يرجى تسجيل الدخول أولاً"
                })

        logger.info(f"🌐 بدء البحث العام للمستخدم {user_id} عن: {query}")

        # تشغيل البحث العام
        result = client_manager.run_coroutine(
            search_public_telegram(client_manager.client, query, limit)
        )

        logger.info(f"✅ تم العثور على {len(result)} قناة/مجموعة للمستخدم {user_id}")

        return jsonify({
            "success": True,
            "channels": result,
            "message": f"تم العثور على {len(result)} قناة/مجموعة"
        })

    except Exception as e:
        logger.error(f"خطأ في البحث العام: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"❌ خطأ في البحث: {str(e)}"
        })

async def search_public_telegram(client, query, limit=50):
    """البحث العام في التليجرام"""
    results = []

    try:
        # البحث العام باستخدام SearchGlobalRequest
        global_search = await client(SearchGlobalRequest(
            q=query,
            offset_date=None,
            offset_peer=None,
            offset_id=0,
            limit=limit
        ))

        # معالجة النتائج
        for message in global_search.messages:
            if hasattr(message, 'peer_id') and hasattr(message.peer_id, 'channel_id'):
                # البحث عن القناة في الكيانات
                channel_id = message.peer_id.channel_id

                for chat in global_search.chats:
                    if hasattr(chat, 'id') and chat.id == channel_id:
                        if isinstance(chat, Channel):
                            username = chat.username if hasattr(chat, 'username') else None

                            result_item = {
                                'id': str(chat.id),
                                'title': chat.title,
                                'username': username,
                                'participants_count': getattr(chat, 'participants_count', 0),
                                'megagroup': getattr(chat, 'megagroup', False),
                                'verified': getattr(chat, 'verified', False),
                                'scam': getattr(chat, 'scam', False)
                            }

                            # تجنب التكرار
                            if not any(r['id'] == result_item['id'] for r in results):
                                results.append(result_item)

        # بحث إضافي بطرق أخرى إذا كانت النتائج قليلة
        if len(results) < 10:
            try:
                # محاولة البحث باستخدام اسم المستخدم مباشرة
                if not query.startswith('@'):
                    potential_username = '@' + query.replace(' ', '').replace('@', '')
                    try:
                        entity = await client.get_entity(potential_username)
                        if isinstance(entity, (Channel, Chat)):
                            result_item = {
                                'id': str(entity.id),
                                'title': entity.title,
                                'username': getattr(entity, 'username', None),
                                'participants_count': getattr(entity, 'participants_count', 0),
                                'megagroup': getattr(entity, 'megagroup', False),
                                'verified': getattr(entity, 'verified', False),
                                'scam': getattr(entity, 'scam', False)
                            }

                            if not any(r['id'] == result_item['id'] for r in results):
                                results.append(result_item)
                    except Exception:
                        pass
            except Exception:
                pass

    except Exception as e:
        logger.warning(f"خطأ في البحث العام: {str(e)}")
        # محاولة بطريقة بديلة
        pass

    # ترتيب النتائج حسب عدد الأعضاء
    results.sort(key=lambda x: x.get('participants_count', 0), reverse=True)

    return results[:limit]

# بدء نظام التنبيهات عند تشغيل التطبيق
alert_queue.start()

# تحميل الجلسات عند بدء التطبيق
load_all_sessions()

if __name__ == '__main__':
    # استخدام المنفذ 5000 مباشرة
    port = 5000
    print(f"🌐 تشغيل الخادم على المنفذ {port}...")
    print(f"🔗 رابط التطبيق: http://0.0.0.0:{port}")
    print("🛡️ نظام الاستمرارية المتقدم مُفعل - سيعمل التطبيق لفترات أطول")

    # إعداد logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    try:
        socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
    except Exception as e:
        print(f"❌ خطأ في تشغيل الخادم: {e}")