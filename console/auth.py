"""控制台账号：注册、登录、会话、修改密码（密码使用 PBKDF2 加盐哈希保存）"""
import hashlib
import hmac
import secrets
import time

from . import store

SESSION_DAYS = 30
ITERATIONS = 200_000


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), ITERATIONS).hex()
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest}"


def verify_password(password, stored):
    try:
        _, iterations, salt, digest = stored.split("$")
        check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), int(iterations)).hex()
        return hmac.compare_digest(check, digest)
    except (ValueError, AttributeError):
        return False


def _validate(username, password):
    username = (username or "").strip()
    if not 3 <= len(username) <= 32:
        raise ValueError("用户名长度需要 3～32 个字符")
    if len(password or "") < 6:
        raise ValueError("密码至少 6 位")
    return username


def has_users():
    return store.row("SELECT id FROM users LIMIT 1") is not None


def create_user(username, password, is_admin=False):
    username = _validate(username, password)
    if store.row("SELECT id FROM users WHERE username = ?", (username,)):
        raise ValueError("用户名已存在")
    return store.execute(
        "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
        (username, hash_password(password), 1 if is_admin else 0, time.time()),
    )


def login(username, password):
    user = store.row("SELECT * FROM users WHERE username = ?", ((username or "").strip(),))
    if not user or not verify_password(password or "", user["password_hash"]):
        # 固定延迟，降低暴力猜密码的速度
        time.sleep(0.5)
        raise ValueError("用户名或密码错误")
    token = secrets.token_urlsafe(32)
    store.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
    store.execute(
        "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user["id"], time.time() + SESSION_DAYS * 86400),
    )
    return token


def logout(token):
    store.execute("DELETE FROM sessions WHERE token = ?", (token or "",))


def user_by_token(token):
    if not token:
        return None
    return store.row(
        "SELECT u.id, u.username, u.is_admin FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ? AND s.expires_at > ?",
        (token, time.time()),
    )


def list_users():
    return store.rows("SELECT id, username, is_admin, created_at FROM users ORDER BY id")


def change_password(user_id, old_password, new_password):
    user = store.row("SELECT * FROM users WHERE id = ?", (user_id,))
    if not user or not verify_password(old_password or "", user["password_hash"]):
        raise ValueError("原密码不正确")
    _validate(user["username"], new_password)
    store.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_password), user_id))


def delete_user(user_id, current_user_id):
    if int(user_id) == int(current_user_id):
        raise ValueError("不能删除当前登录的账号")
    store.execute("DELETE FROM sessions WHERE user_id = ?", (int(user_id),))
    store.execute("DELETE FROM users WHERE id = ?", (int(user_id),))
