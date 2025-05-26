from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, session
import sqlite3
from datetime import datetime, timedelta
import threading
import time
import requests
import json
import os
import hashlib
from functools import wraps
import logging
import pytz

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)

app = Flask(__name__)
app.secret_key = 'your-super-secret-key-for-sessions-2023'

# 时区配置
CHINA_TZ = pytz.timezone('Asia/Shanghai')

# 全局锁和已处理记录，防止并发处理同一任务
reminder_lock = threading.Lock()
processed_reminders = set()  # 存储已处理的提醒，格式：task_id_date_hour_minute
last_check_minute = None  # 记录上次检查的分钟，防止同一分钟内多次执行

def get_china_time():
    """获取中国时间"""
    return datetime.now(CHINA_TZ)

def get_server_time():
    """获取服务器时间"""
    return datetime.now()

def dict_factory(cursor, row):
    """将查询结果转换为字典格式"""
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

def hash_password(password):
    """对密码进行哈希处理"""
    return hashlib.sha256(password.encode()).hexdigest()

def login_required(f):
    """登录验证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('请先登录！', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def init_db():
    """初始化数据库"""
    conn = sqlite3.connect('todolist.db')
    c = conn.cursor()
    
    # 创建用户表
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  username TEXT UNIQUE NOT NULL,
                  password_hash TEXT NOT NULL,
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  last_login TEXT,
                  timezone TEXT DEFAULT 'Asia/Shanghai')''')
    
    # 创建待办事项表
    c.execute('''CREATE TABLE IF NOT EXISTS todos
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  title TEXT NOT NULL,
                  description TEXT,
                  due_date TEXT,
                  priority TEXT DEFAULT 'medium',
                  status TEXT DEFAULT 'pending',
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  reminder_sent TEXT DEFAULT '',
                  robot_id INTEGER DEFAULT 1,
                  notification_time TEXT DEFAULT '10:30',
                  last_notification_date TEXT DEFAULT '',
                  user_id INTEGER DEFAULT 1,
                  timezone TEXT DEFAULT 'Asia/Shanghai')''')
    
    # 创建机器人配置表
    c.execute('''CREATE TABLE IF NOT EXISTS robots
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL,
                  webhook_url TEXT NOT NULL,
                  description TEXT,
                  is_active INTEGER DEFAULT 1,
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    
    # 创建提醒配置表
    c.execute('''CREATE TABLE IF NOT EXISTS reminder_settings
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  days_before INTEGER NOT NULL,
                  is_active INTEGER DEFAULT 1,
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    
    # 创建提醒日志表（优化版）
    c.execute('''CREATE TABLE IF NOT EXISTS reminder_logs
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  todo_id INTEGER NOT NULL,
                  reminder_key TEXT NOT NULL,
                  reminder_type TEXT NOT NULL,
                  days_before INTEGER NOT NULL,
                  sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(todo_id, reminder_key))''')
    
    # 插入默认用户（如果不存在）
    c.execute('SELECT COUNT(*) FROM users')
    if c.fetchone()[0] == 0:
        username = 'jerson'
        password = 'HN@Kiz78L,h2'
        password_hash = hash_password(password)
        c.execute('INSERT INTO users (username, password_hash, timezone) VALUES (?, ?, ?)',
                  (username, password_hash, 'Asia/Shanghai'))
    
    # 插入默认机器人（如果不存在）
    c.execute('SELECT COUNT(*) FROM robots')
    if c.fetchone()[0] == 0:
        c.execute('''INSERT INTO robots (name, webhook_url, description) 
                     VALUES (?, ?, ?)''',
                  ('默认机器人', 
                   'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=3c25323c-4083-4b54-9854-5c8cd42fddb9',
                   '主要通知机器人'))
    
    # 插入默认提醒设置（如果不存在）
    c.execute('SELECT COUNT(*) FROM reminder_settings')
    if c.fetchone()[0] == 0:
        default_reminders = [30, 7, 1, 0]  # 提前30天、7天、1天和当天开始连续提醒
        for days in default_reminders:
            c.execute('INSERT INTO reminder_settings (days_before) VALUES (?)', (days,))
    
    # 添加字段（如果不存在）
    try:
        c.execute('ALTER TABLE users ADD COLUMN timezone TEXT DEFAULT "Asia/Shanghai"')
    except:
        pass
    
    try:
        c.execute('ALTER TABLE todos ADD COLUMN timezone TEXT DEFAULT "Asia/Shanghai"')
    except:
        pass
    
    try:
        c.execute('ALTER TABLE reminder_logs ADD COLUMN reminder_type TEXT DEFAULT "daily"')
    except:
        pass
    
    try:
        c.execute('ALTER TABLE reminder_logs ADD COLUMN days_before INTEGER DEFAULT 0')
    except:
        pass
    
    conn.commit()
    conn.close()

def get_active_robots():
    """获取活跃的机器人列表"""
    conn = sqlite3.connect('todolist.db')
    conn.row_factory = dict_factory
    c = conn.cursor()
    c.execute('SELECT * FROM robots WHERE is_active = 1 ORDER BY id')
    robots = c.fetchall()
    conn.close()
    return robots

def get_robot_by_id(robot_id):
    """根据ID获取机器人信息"""
    conn = sqlite3.connect('todolist.db')
    conn.row_factory = dict_factory
    c = conn.cursor()
    c.execute('SELECT * FROM robots WHERE id = ? AND is_active = 1', (robot_id,))
    robot = c.fetchone()
    conn.close()
    return robot

def get_reminder_settings():
    """获取提醒设置"""
    conn = sqlite3.connect('todolist.db')
    conn.row_factory = dict_factory
    c = conn.cursor()
    c.execute('SELECT * FROM reminder_settings WHERE is_active = 1 ORDER BY days_before DESC')
    settings = c.fetchall()
    conn.close()
    return settings

def send_wechat_message(message, robot_id=1):
    """发送消息到指定的企业微信机器人"""
    robot = get_robot_by_id(robot_id)
    if not robot:
        logging.error(f"机器人 ID {robot_id} 不存在或未激活")
        return False, "机器人不存在或未激活"
    
    webhook_url = robot['webhook_url']
    robot_name = robot['name']
    
    data = {
        "msgtype": "text",
        "text": {
            "content": message
        }
    }
    
    try:
        response = requests.post(webhook_url, json=data, timeout=10)
        if response.status_code == 200:
            result = response.json()
            if result.get('errcode') == 0:
                logging.info(f"通过机器人 '{robot_name}' 发送成功: {message[:50]}...")
                return True, "发送成功"
            else:
                error_msg = f"企业微信API错误: {result}"
                logging.error(f"通过机器人 '{robot_name}' 发送失败: {result}")
                return False, error_msg
        else:
            error_msg = f"HTTP错误，状态码: {response.status_code}"
            logging.error(error_msg)
            return False, error_msg
    except requests.exceptions.Timeout:
        error_msg = "请求超时，请检查网络连接"
        logging.error(error_msg)
        return False, error_msg
    except requests.exceptions.RequestException as e:
        error_msg = f"网络请求错误: {str(e)}"
        logging.error(error_msg)
        return False, error_msg
    except Exception as e:
        error_msg = f"发送企业微信提醒时出错: {str(e)}"
        logging.error(error_msg)
        return False, error_msg

def is_time_to_notify(notification_time, task_timezone='Asia/Shanghai'):
    """检查当前时间是否到了通知时间（基于任务时区）- 精确匹配"""
    try:
        # 获取任务时区的当前时间
        tz = pytz.timezone(task_timezone)
        now = datetime.now(tz)
        
        notify_hour, notify_minute = map(int, notification_time.split(':'))
        
        # 精确匹配当前分钟
        current_hour = now.hour
        current_minute = now.minute
        
        # 只在精确的分钟匹配时返回True
        is_exact_time = (current_hour == notify_hour and current_minute == notify_minute)
        
        if is_exact_time:
            logging.info(f"精确时间匹配: {now.strftime('%H:%M')} = {notification_time} (时区: {task_timezone})")
        
        return is_exact_time
    except Exception as e:
        logging.error(f"检查通知时间时出错: {e}")
        return False

def should_send_reminder(days_until_due, reminder_settings):
    """
    新的提醒规则判断逻辑：
    1. 提醒周期天数 <= 7天：每天在指定时间进行提醒
    2. 提醒周期天数 > 7天：在设定时间周期时间点进行一次提醒
    
    返回: (是否应该提醒, 提醒类型, 匹配的天数设置)
    """
    # 查找精确匹配的提醒设置
    exact_match = None
    for setting in reminder_settings:
        if setting['days_before'] == days_until_due:
            exact_match = setting
            break
    
    # 如果有精确匹配
    if exact_match:
        days_before = exact_match['days_before']
        if days_before <= 7:
            # <= 7天：每日提醒模式
            return True, "daily", days_before
        else:
            # > 7天：单次提醒模式
            return True, "once", days_before
    
    # 检查是否在每日提醒范围内（距离到期 <= 7天且有相应的提醒设置）
    if days_until_due <= 7:
        # 查找所有 <= 7天的提醒设置
        daily_settings = [s for s in reminder_settings if s['days_before'] <= 7]
        if daily_settings:
            # 找到最接近的设置
            closest_setting = min(daily_settings, key=lambda x: abs(x['days_before'] - days_until_due))
            if days_until_due <= closest_setting['days_before']:
                return True, "daily", closest_setting['days_before']
    
    return False, None, None

def is_reminder_already_sent(todo_id, reminder_key):
    """检查提醒是否已经发送过（使用数据库记录）"""
    conn = sqlite3.connect('todolist.db')
    c = conn.cursor()
    c.execute('SELECT id FROM reminder_logs WHERE todo_id = ? AND reminder_key = ?', 
              (todo_id, reminder_key))
    result = c.fetchone()
    conn.close()
    return result is not None

def record_reminder_sent(todo_id, reminder_key, reminder_type, days_before):
    """记录已发送的提醒（使用数据库记录）"""
    conn = sqlite3.connect('todolist.db')
    c = conn.cursor()
    try:
        c.execute('INSERT INTO reminder_logs (todo_id, reminder_key, reminder_type, days_before) VALUES (?, ?, ?, ?)', 
                  (todo_id, reminder_key, reminder_type, days_before))
        conn.commit()
        logging.info(f"记录提醒发送: todo_id={todo_id}, key={reminder_key}, type={reminder_type}, days={days_before}")
        return True
    except sqlite3.IntegrityError:
        # 如果已存在，说明已经发送过
        logging.warning(f"提醒已存在: todo_id={todo_id}, key={reminder_key}")
        return False
    except Exception as e:
        logging.error(f"记录提醒发送失败: {e}")
        return False
    finally:
        conn.close()

def process_single_todo_reminder(todo, reminder_settings, today_str, china_now):
    """处理单个任务的提醒 - 新提醒规则版本"""
    try:
        # 使用任务的时区
        task_tz = pytz.timezone(todo['timezone'])
        task_now = datetime.now(task_tz)
        
        due_date = datetime.strptime(todo['due_date'], '%Y-%m-%d')
        days_until_due = (due_date.date() - task_now.date()).days
        
        notification_time = todo['notification_time'] or '10:30'
        
        # 检查是否到了通知时间（精确匹配）
        if not is_time_to_notify(notification_time, todo['timezone']):
            return False, "不是通知时间"
        
        logging.info(f"处理任务提醒: {todo['title']} - 距离到期: {days_until_due}天")
        
        # 使用新的提醒规则判断是否应该提醒
        should_remind, reminder_type, trigger_days = should_send_reminder(days_until_due, reminder_settings)
        
        if not should_remind:
            return False, f"不在提醒范围内 (距离到期{days_until_due}天，无匹配的提醒设置)"
        
        # 根据提醒类型生成不同的提醒key
        if reminder_type == "daily":
            # 每日提醒：使用日期+时间作为key
            reminder_key = f"{todo['id']}_{today_str}_{task_now.hour:02d}_{task_now.minute:02d}_daily_{trigger_days}"
        else:  # once
            # 单次提醒：使用天数作为key（不包含具体时间）
            reminder_key = f"{todo['id']}_once_{days_until_due}days"
        
        # 三重防重复检查
        # 1. 内存级别的防重复检查
        if reminder_key in processed_reminders:
            return False, f"内存中已处理: {reminder_key}"
        
        # 2. 数据库级别的防重复检查
        if is_reminder_already_sent(todo['id'], reminder_key):
            processed_reminders.add(reminder_key)  # 同步到内存
            return False, f"数据库中已发送: {reminder_key}"
        
        # 3. 使用数据库事务确保原子性操作
        conn = sqlite3.connect('todolist.db')
        c = conn.cursor()
        try:
            # 在事务中再次检查并插入记录
            c.execute('SELECT id FROM reminder_logs WHERE todo_id = ? AND reminder_key = ?', 
                      (todo['id'], reminder_key))
            if c.fetchone():
                processed_reminders.add(reminder_key)
                return False, f"事务检查中已发送: {reminder_key}"
            
            # 先插入记录，如果成功则继续发送
            c.execute('INSERT INTO reminder_logs (todo_id, reminder_key, reminder_type, days_before) VALUES (?, ?, ?, ?)', 
                      (todo['id'], reminder_key, reminder_type, days_until_due))
            conn.commit()
            
            # 记录到内存缓存
            processed_reminders.add(reminder_key)
            
        except sqlite3.IntegrityError:
            # 如果插入失败，说明已经存在
            processed_reminders.add(reminder_key)
            return False, f"数据库约束检查已发送: {reminder_key}"
        except Exception as e:
            logging.error(f"数据库操作失败: {e}")
            return False, f"数据库操作失败: {e}"
        finally:
            conn.close()
        
        # 生成提醒消息
        if reminder_type == "daily":
            # 每日提醒消息
            if days_until_due > 0:
                reminder_message = f"📅 待办事项每日提醒\n\n标题: {todo['title']}\n描述: {todo['description'] or '无'}\n截止日期: {todo['due_date']}\n优先级: {todo['priority']}\n\n⚠️ 还有 {days_until_due} 天到期，请及时处理！\n\n⏰ 提醒时间: {task_now.strftime('%Y-%m-%d %H:%M:%S')} ({todo['timezone']})\n💡 提醒规则: 距离到期 ≤ 7天时每日提醒 (触发设置: ≤{trigger_days}天)"
            elif days_until_due == 0:
                reminder_message = f"🚨 待办事项紧急提醒\n\n标题: {todo['title']}\n描述: {todo['description'] or '无'}\n截止日期: {todo['due_date']}\n优先级: {todo['priority']}\n\n❗ 今天到期，请立即处理！\n\n⏰ 提醒时间: {task_now.strftime('%Y-%m-%d %H:%M:%S')} ({todo['timezone']})\n💡 提醒规则: 当天每日提醒"
            else:
                # 逾期提醒
                overdue_days = abs(days_until_due)
                reminder_message = f"⏰ 待办事项逾期提醒\n\n标题: {todo['title']}\n描述: {todo['description'] or '无'}\n截止日期: {todo['due_date']}\n优先级: {todo['priority']}\n\n🔴 已逾期 {overdue_days} 天，请尽快处理！\n\n⏰ 提醒时间: {task_now.strftime('%Y-%m-%d %H:%M:%S')} ({todo['timezone']})\n💡 提醒规则: 逾期每日提醒"
        else:  # once
            # 单次提醒消息
            reminder_message = f"🔔 待办事项定时提醒\n\n标题: {todo['title']}\n描述: {todo['description'] or '无'}\n截止日期: {todo['due_date']}\n优先级: {todo['priority']}\n\n📌 距离到期还有 {days_until_due} 天\n\n⏰ 提醒时间: {task_now.strftime('%Y-%m-%d %H:%M:%S')} ({todo['timezone']})\n💡 提醒规则: 提前 {trigger_days} 天单次提醒"
        
        logging.info(f"准备发送提醒: {todo['title']} - {reminder_key} - 类型: {reminder_type}")
        success, error_msg = send_wechat_message(reminder_message, todo['robot_id'] or 1)
        
        if success:
            logging.info(f"✅ 提醒发送成功: {todo['title']} ({reminder_key}) - 类型: {reminder_type}")
            return True, f"发送成功 - {reminder_type}"
        else:
            # 如果发送失败，删除数据库记录和内存缓存
            try:
                conn = sqlite3.connect('todolist.db')
                c = conn.cursor()
                c.execute('DELETE FROM reminder_logs WHERE todo_id = ? AND reminder_key = ?', 
                          (todo['id'], reminder_key))
                conn.commit()
                conn.close()
                processed_reminders.discard(reminder_key)
                logging.warning(f"发送失败，已清理记录: {reminder_key}")
            except Exception as cleanup_error:
                logging.error(f"清理失败记录时出错: {cleanup_error}")
            
            logging.error(f"❌ 提醒发送失败: {todo['title']} - {error_msg}")
            return False, f"发送失败: {error_msg}"
            
    except ValueError as e:
        logging.error(f"日期格式错误: {todo.get('due_date', 'Unknown')} - {e}")
        return False, f"日期格式错误: {e}"
    except Exception as e:
        logging.error(f"处理任务提醒时出错: {todo.get('title', 'Unknown')} - {e}")
        return False, f"处理出错: {e}"

def cleanup_old_reminder_logs():
    """清理7天前的提醒日志"""
    try:
        conn = sqlite3.connect('todolist.db')
        c = conn.cursor()
        
        # 删除7天前的记录
        seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        c.execute('DELETE FROM reminder_logs WHERE sent_at < ?', (seven_days_ago,))
        deleted_count = c.rowcount
        conn.commit()
        conn.close()
        
        if deleted_count > 0:
            logging.info(f"清理了 {deleted_count} 条旧的提醒日志")
    except Exception as e:
        logging.error(f"清理提醒日志时出错: {e}")

def check_reminders():
    """检查需要提醒的待办事项（定时检查）- 新提醒规则版本"""
    global last_check_minute
    logging.info("提醒检查线程启动 - 新提醒规则版本")
    
    while True:
        try:
            # 获取当前时间
            current_time = datetime.now()
            current_minute_key = f"{current_time.hour:02d}:{current_time.minute:02d}"
            
            # 防止同一分钟内多次执行
            if last_check_minute == current_minute_key:
                time.sleep(10)  # 等待10秒再检查
                continue
            
            # 使用全局锁防止并发执行
            with reminder_lock:
                # 更新检查时间
                last_check_minute = current_minute_key
                
                # 每小时清理一次旧日志
                if current_time.minute == 0:  # 每小时的0分钟执行清理
                    cleanup_old_reminder_logs()
                
                conn = sqlite3.connect('todolist.db')
                conn.row_factory = dict_factory
                c = conn.cursor()
                
                # 获取提醒设置
                reminder_settings = get_reminder_settings()
                if not reminder_settings:
                    logging.warning("没有配置提醒设置")
                    conn.close()
                    time.sleep(30)  # 等待30秒再检查
                    continue
                
                # 获取中国时间
                china_now = get_china_time()
                today_str = china_now.strftime('%Y-%m-%d')
                
                logging.info(f"开始检查提醒 - 中国时间: {china_now.strftime('%Y-%m-%d %H:%M:%S')} - 检查key: {current_minute_key}")
                
                # 获取所有待处理的任务
                c.execute('''SELECT id, title, description, due_date, priority, robot_id, 
                            reminder_sent, notification_time, last_notification_date, user_id,
                            COALESCE(timezone, 'Asia/Shanghai') as timezone
                            FROM todos 
                            WHERE status = 'pending' 
                            AND due_date IS NOT NULL 
                            AND due_date != ""''')
                
                todos = c.fetchall()
                conn.close()
                
                logging.info(f"找到 {len(todos)} 个待处理任务")
                
                # 处理每个任务
                processed_count = 0
                sent_count = 0
                
                for todo in todos:
                    success, message = process_single_todo_reminder(todo, reminder_settings, today_str, china_now)
                    processed_count += 1
                    if success:
                        sent_count += 1
                
                logging.info(f"本轮检查完成 - 处理任务: {processed_count}, 发送提醒: {sent_count}, 内存缓存: {len(processed_reminders)}")
            
        except Exception as e:
            logging.error(f"检查提醒时出错: {e}")
        
        # 等待30秒再进行下一次检查
        time.sleep(30)

# 登录相关路由
@app.route('/login', methods=['GET', 'POST'])
def login():
    """用户登录"""
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        password_hash = hash_password(password)
        
        conn = sqlite3.connect('todolist.db')
        conn.row_factory = dict_factory
        c = conn.cursor()
        c.execute('SELECT * FROM users WHERE username = ? AND password_hash = ?', 
                  (username, password_hash))
        user = c.fetchone()
        
        if user:
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['timezone'] = user.get('timezone', 'Asia/Shanghai')
            
            # 更新最后登录时间
            china_time = get_china_time()
            c.execute('UPDATE users SET last_login = ? WHERE id = ?', 
                      (china_time.strftime('%Y-%m-%d %H:%M:%S'), user['id']))
            conn.commit()
            conn.close()
            
            flash(f'欢迎回来，{username}！', 'success')
            return redirect(url_for('index'))
        else:
            conn.close()
            flash('用户名或密码错误！', 'error')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    """用户登出"""
    username = session.get('username', '用户')
    session.clear()
    flash(f'再见，{username}！', 'info')
    return redirect(url_for('login'))

# 主要功能路由（需要登录）
@app.route('/')
@login_required
def index():
    """主页 - 显示任务列表"""
    conn = sqlite3.connect('todolist.db')
    conn.row_factory = dict_factory
    c = conn.cursor()
    
    # 只显示当前用户的任务
    user_id = session['user_id']
    c.execute('''SELECT t.*, COALESCE(r.name, '默认机器人') as robot_name 
                 FROM todos t 
                 LEFT JOIN robots r ON t.robot_id = r.id 
                 WHERE t.user_id = ?
                 ORDER BY t.created_at DESC''', (user_id,))
    todos = c.fetchall()
    conn.close()
    
    robots = get_active_robots()
    
    # 添加时区信息到模板
    china_time = get_china_time()
    server_time = get_server_time()
    
    return render_template('index.html', todos=todos, robots=robots, 
                         china_time=china_time, server_time=server_time)

@app.route('/config')
@login_required
def config():
    """配置页面"""
    robots = get_active_robots()
    reminder_settings = get_reminder_settings()
    return render_template('config.html', robots=robots, reminder_settings=reminder_settings)

@app.route('/add_robot', methods=['POST'])
@login_required
def add_robot():
    """添加机器人"""
    name = request.form['name']
    webhook_url = request.form['webhook_url']
    description = request.form.get('description', '')
    
    conn = sqlite3.connect('todolist.db')
    c = conn.cursor()
    c.execute('INSERT INTO robots (name, webhook_url, description) VALUES (?, ?, ?)',
              (name, webhook_url, description))
    conn.commit()
    conn.close()
    
    flash(f'机器人 "{name}" 添加成功！', 'success')
    return redirect(url_for('config'))

@app.route('/delete_robot/<int:robot_id>')
@login_required
def delete_robot(robot_id):
    """删除机器人"""
    conn = sqlite3.connect('todolist.db')
    c = conn.cursor()
    c.execute('UPDATE robots SET is_active = 0 WHERE id = ?', (robot_id,))
    conn.commit()
    conn.close()
    
    flash('机器人已删除！', 'info')
    return redirect(url_for('config'))

@app.route('/add_reminder', methods=['POST'])
@login_required
def add_reminder():
    """添加提醒设置"""
    days_before = int(request.form['days_before'])
    
    conn = sqlite3.connect('todolist.db')
    c = conn.cursor()
    
    # 检查是否已存在
    c.execute('SELECT id FROM reminder_settings WHERE days_before = ? AND is_active = 1', (days_before,))
    if c.fetchone():
        flash(f'提前 {days_before} 天的提醒设置已存在！', 'warning')
    else:
        c.execute('INSERT INTO reminder_settings (days_before) VALUES (?)', (days_before,))
        conn.commit()
        
        # 根据天数给出不同的提示
        if days_before <= 7:
            flash(f'提前 {days_before} 天开始每日提醒的设置添加成功！', 'success')
        else:
            flash(f'提前 {days_before} 天单次提醒的设置添加成功！', 'success')
    
    conn.close()
    return redirect(url_for('config'))

@app.route('/delete_reminder/<int:reminder_id>')
@login_required
def delete_reminder(reminder_id):
    """删除提醒设置"""
    conn = sqlite3.connect('todolist.db')
    c = conn.cursor()
    c.execute('UPDATE reminder_settings SET is_active = 0 WHERE id = ?', (reminder_id,))
    conn.commit()
    conn.close()
    
    flash('提醒设置已删除！', 'info')
    return redirect(url_for('config'))

@app.route('/add', methods=['POST'])
@login_required
def add_todo():
    """添加新任务"""
    title = request.form['title']
    description = request.form.get('description', '')
    due_date = request.form.get('due_date', '')
    priority = request.form.get('priority', 'medium')
    robot_id = int(request.form.get('robot_id', 1))
    notification_time = request.form.get('notification_time', '10:30')
    user_id = session['user_id']
    user_timezone = session.get('timezone', 'Asia/Shanghai')
    
    conn = sqlite3.connect('todolist.db')
    c = conn.cursor()
    c.execute('''INSERT INTO todos (title, description, due_date, priority, robot_id, notification_time, user_id, timezone) 
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
              (title, description, due_date if due_date else None, priority, robot_id, notification_time, user_id, user_timezone))
    conn.commit()
    
    # 发送创建通知到指定机器人
    username = session['username']
    china_time = get_china_time()
    message = f"✅ 新待办事项已创建\n\n用户: {username}\n标题: {title}\n描述: {description or '无'}\n截止日期: {due_date or '无'}\n优先级: {priority}\n通知时间: {notification_time}\n创建时间: {china_time.strftime('%Y-%m-%d %H:%M:%S')} (中国时间)\n\n💡 提醒规则: ≤7天每日提醒，>7天单次提醒"
    success, error_msg = send_wechat_message(message, robot_id)
    
    if success:
        flash('任务创建成功，已发送微信通知！', 'success')
    else:
        flash(f'任务创建成功，但微信通知发送失败: {error_msg}', 'warning')
    
    conn.close()
    return redirect(url_for('index'))

@app.route('/edit/<int:todo_id>')
@login_required
def edit_todo(todo_id):
    """编辑任务页面"""
    conn = sqlite3.connect('todolist.db')
    conn.row_factory = dict_factory
    c = conn.cursor()
    
    # 确保只能编辑自己的任务
    user_id = session['user_id']
    c.execute('SELECT * FROM todos WHERE id = ? AND user_id = ?', (todo_id, user_id))
    todo = c.fetchone()
    conn.close()
    
    if not todo:
        flash('任务不存在或无权限访问！', 'error')
        return redirect(url_for('index'))
    
    robots = get_active_robots()
    return render_template('edit_todo.html', todo=todo, robots=robots)

@app.route('/update/<int:todo_id>', methods=['POST'])
@login_required
def update_todo(todo_id):
    """更新任务"""
    title = request.form['title']
    description = request.form.get('description', '')
    due_date = request.form.get('due_date', '')
    priority = request.form.get('priority', 'medium')
    robot_id = int(request.form.get('robot_id', 1))
    notification_time = request.form.get('notification_time', '10:30')
    user_id = session['user_id']
    user_timezone = session.get('timezone', 'Asia/Shanghai')
    
    conn = sqlite3.connect('todolist.db')
    c = conn.cursor()
    
    # 清理相关的提醒日志（因为任务已修改）
    c.execute('DELETE FROM reminder_logs WHERE todo_id = ?', (todo_id,))
    
    # 确保只能更新自己的任务，并重置提醒状态
    c.execute('''UPDATE todos SET title = ?, description = ?, due_date = ?, 
                 priority = ?, robot_id = ?, notification_time = ?, reminder_sent = '', timezone = ?
                 WHERE id = ? AND user_id = ?''',
              (title, description, due_date if due_date else None, 
               priority, robot_id, notification_time, user_timezone, todo_id, user_id))
    
    if c.rowcount > 0:
        conn.commit()
        flash('任务更新成功！提醒状态已重置。', 'success')
    else:
        flash('任务不存在或无权限修改！', 'error')
    
    conn.close()
    return redirect(url_for('index'))

@app.route('/complete/<int:todo_id>')
@login_required
def complete_todo(todo_id):
    """完成任务"""
    conn = sqlite3.connect('todolist.db')
    c = conn.cursor()
    
    # 获取待办事项信息（确保是当前用户的任务）
    user_id = session['user_id']
    c.execute('SELECT title, description, robot_id FROM todos WHERE id = ? AND user_id = ?', 
              (todo_id, user_id))
    todo = c.fetchone()
    
    if todo:
        title, description, robot_id = todo
        
        # 清理相关的提醒日志
        c.execute('DELETE FROM reminder_logs WHERE todo_id = ?', (todo_id,))
        
        c.execute('UPDATE todos SET status = "completed" WHERE id = ? AND user_id = ?', 
                  (todo_id, user_id))
        conn.commit()
        
        # 发送完成通知到指定机器人
        username = session['username']
        china_time = get_china_time()
        message = f"🎉 待办事项已完成\n\n用户: {username}\n标题: {title}\n描述: {description or '无'}\n完成时间: {china_time.strftime('%Y-%m-%d %H:%M:%S')} (中国时间)\n\n✅ 所有提醒已停止"
        success, error_msg = send_wechat_message(message, robot_id or 1)
        
        if success:
            flash('任务已完成，已发送微信通知！', 'success')
        else:
            flash(f'任务已完成，但微信通知发送失败: {error_msg}', 'warning')
    else:
        flash('任务不存在或无权限操作！', 'error')
    
    conn.close()
    return redirect(url_for('index'))

@app.route('/delete/<int:todo_id>')
@login_required
def delete_todo(todo_id):
    """删除任务"""
    conn = sqlite3.connect('todolist.db')
    c = conn.cursor()
    
    # 清理相关的提醒日志
    c.execute('DELETE FROM reminder_logs WHERE todo_id = ?', (todo_id,))
    
    # 确保只能删除自己的任务
    user_id = session['user_id']
    c.execute('DELETE FROM todos WHERE id = ? AND user_id = ?', (todo_id, user_id))
    
    if c.rowcount > 0:
        conn.commit()
        flash('任务已删除！', 'info')
    else:
        flash('任务不存在或无权限删除！', 'error')
    
    conn.close()
    return redirect(url_for('index'))

@app.route('/test_wechat')
@login_required
def test_wechat():
    """测试企业微信通知"""
    robot_id = request.args.get('robot_id', 1, type=int)
    robot = get_robot_by_id(robot_id)
    username = session['username']
    
    if not robot:
        response_data = {
            "success": False,
            "message": "指定的机器人不存在或未激活",
            "robot_name": "未知"
        }
    else:
        china_time = get_china_time()
        server_time = get_server_time()
        message = f"🔔 待办事项系统测试消息\n\n用户: {username}\n中国时间: {china_time.strftime('%Y-%m-%d %H:%M:%S')}\n服务器时间: {server_time.strftime('%Y-%m-%d %H:%M:%S')}\n机器人: {robot['name']}\n\n💡 新提醒规则:\n- ≤7天: 每日提醒\n- >7天: 单次提醒\n\n系统运行正常！"
        success, error_msg = send_wechat_message(message, robot_id)
        
        response_data = {
            "success": success,
            "message": "测试消息发送成功！" if success else f"发送失败: {error_msg}",
            "robot_name": robot['name'],
            "robot_id": robot_id,
            "china_time": china_time.strftime('%Y-%m-%d %H:%M:%S'),
            "server_time": server_time.strftime('%Y-%m-%d %H:%M:%S')
        }
    
    # 确保中文正确编码
    response = app.response_class(
        response=json.dumps(response_data, ensure_ascii=False, indent=2),
        status=200,
        mimetype='application/json; charset=utf-8'
    )
    return response

@app.route('/debug_reminders')
@login_required
def debug_reminders():
    """调试提醒功能 - 显示当前任务状态"""
    conn = sqlite3.connect('todolist.db')
    conn.row_factory = dict_factory
    c = conn.cursor()
    
    user_id = session['user_id']
    c.execute('''SELECT id, title, due_date, notification_time, reminder_sent, 
                 last_notification_date, status, priority, COALESCE(timezone, 'Asia/Shanghai') as timezone
                 FROM todos 
                 WHERE user_id = ? AND status = 'pending'
                 ORDER BY due_date''', (user_id,))
    todos = c.fetchall()
    
    reminder_settings = get_reminder_settings()
    
    china_time = get_china_time()
    server_time = get_server_time()
    
    debug_info = {
        "china_time": china_time.strftime('%Y-%m-%d %H:%M:%S'),
        "server_time": server_time.strftime('%Y-%m-%d %H:%M:%S'),
        "reminder_rule": "新提醒规则: ≤7天每日提醒，>7天单次提醒",
        "reminder_settings": [
            {
                "days_before": s['days_before'],
                "type": "每日提醒" if s['days_before'] <= 7 else "单次提醒"
            } for s in reminder_settings
        ],
        "memory_cache_size": len(processed_reminders),
        "last_check_minute": last_check_minute,
        "todos": []
    }
    
    for todo in todos:
        if todo['due_date']:
            try:
                # 使用任务的时区
                task_tz = pytz.timezone(todo['timezone'])
                task_now = datetime.now(task_tz)
                
                due_date = datetime.strptime(todo['due_date'], '%Y-%m-%d')
                days_until_due = (due_date.date() - task_now.date()).days
                
                # 判断是否应该提醒
                should_remind, reminder_type, trigger_days = should_send_reminder(days_until_due, reminder_settings)
                
                # 生成当前的提醒key
                today_str = china_time.strftime('%Y-%m-%d')
                if reminder_type == "daily":
                    current_reminder_key = f"{todo['id']}_{today_str}_{task_now.hour:02d}_{task_now.minute:02d}_daily_{trigger_days}"
                elif reminder_type == "once":
                    current_reminder_key = f"{todo['id']}_once_{days_until_due}days"
                else:
                    current_reminder_key = "N/A"
                
                # 检查数据库中的提醒记录
                c.execute('SELECT COUNT(*) FROM reminder_logs WHERE todo_id = ? AND reminder_key LIKE ?', 
                         (todo['id'], f"{todo['id']}_%"))
                total_sent_count = c.fetchone()[0]
                
                # 检查今日发送记录
                c.execute('SELECT COUNT(*) FROM reminder_logs WHERE todo_id = ? AND reminder_key LIKE ?', 
                         (todo['id'], f"{todo['id']}_{today_str}_%"))
                today_sent_count = c.fetchone()[0]
                
                todo_info = dict(todo)
                todo_info['days_until_due'] = days_until_due
                todo_info['task_current_time'] = task_now.strftime('%Y-%m-%d %H:%M:%S')
                todo_info['is_notification_time'] = is_time_to_notify(todo['notification_time'] or '10:30', todo['timezone'])
                todo_info['should_remind'] = should_remind
                todo_info['reminder_type'] = reminder_type or "无"
                todo_info['trigger_rule'] = f"{trigger_days}天-{reminder_type}" if trigger_days is not None else "不在提醒范围"
                todo_info['current_reminder_key'] = current_reminder_key
                todo_info['today_sent_count'] = today_sent_count
                todo_info['total_sent_count'] = total_sent_count
                todo_info['in_memory_cache'] = current_reminder_key in processed_reminders
                
                debug_info["todos"].append(todo_info)
            except Exception as e:
                logging.error(f"处理调试信息时出错: {e}")
                pass
    
    conn.close()
    
    response = app.response_class(
        response=json.dumps(debug_info, ensure_ascii=False, indent=2),
        status=200,
        mimetype='application/json; charset=utf-8'
    )
    return response

if __name__ == '__main__':
    init_db()
    
    # 启动提醒检查线程
    reminder_thread = threading.Thread(target=check_reminders, daemon=True)
    reminder_thread.start()
    
    app.run(host='0.0.0.0', port=8081, debug=True)
