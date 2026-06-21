import os
from datetime import datetime, timedelta
from fastapi import FastAPI, File, UploadFile, Form, Request, HTTPException, Depends, Response, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, or_
from sqlalchemy.orm import declarative_base, sessionmaker
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted
from PIL import Image
import io
import uuid
import qrcode
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv()

# Setup Gemini API key
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# --- 資料庫設定 (SQLite) ---
SQLALCHEMY_DATABASE_URL = "sqlite:///./lost_found.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class LostItem(Base):
    __tablename__ = "lost_items"
    id = Column(Integer, primary_key=True, index=True)
    category = Column(String, index=True)
    item_name = Column(String, nullable=True)  # 物品名稱（簡短，如「皮夾」「ipad」），對應台鐵公告格式
    description = Column(String)
    found_location = Column(String)
    image_filename = Column(String)
    additional_images = Column(String, default="")
    train_type = Column(String, nullable=True)
    train_number = Column(String, nullable=True)
    seat_number = Column(String, nullable=True)
    found_time = Column(DateTime, default=datetime.now)
    status = Column(String, default="pending")
    tags = Column(String, nullable=True)
    qr_code_id = Column(String, unique=True, index=True, nullable=True)
    current_station = Column(String, nullable=True)
    is_sensitive = Column(Boolean, default=False)
    owner_name = Column(String, nullable=True)
    card_last_four = Column(String, nullable=True)
    # 對應台鐵官方遺失物公告格式的欄位
    pickup_type = Column(String, nullable=True)       # "車上" 或 "車站"
    station_code = Column(String, nullable=True)      # 例如 "3390"
    custodian_unit = Column(String, nullable=True)     # 保管單位，例如 "員林"
    service_phone = Column(String, nullable=True)      # 服務電話，例如 "04-8320544"
    item_code = Column(String, unique=True, index=True, nullable=True)  # 物品代號，例如 "3390-026-45262"

WISHLIST_EXPIRY_DAYS = 90

class Wishlist(Base):
    __tablename__ = "wishlist"
    id = Column(Integer, primary_key=True, index=True)
    user_name = Column(String)
    user_email = Column(String)
    user_phone = Column(String, nullable=True)
    category = Column(String)
    keywords = Column(String)
    is_active = Column(Boolean, default=True)
    matched_item_id = Column(Integer, nullable=True)  # 物品被標記 claimed 時，若特徵相符會記錄在這裡，供後台提示「可能已找到」
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)  # 超過此時間後不再參與比對（仍保留資料，僅跳過比對以節省 AI 額度與效能）

class DeliveryRequest(Base):
    __tablename__ = "delivery_requests"
    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer)
    customer_name = Column(String)
    customer_phone = Column(String)
    customer_email = Column(String)
    proof_description = Column(String)
    target_station = Column(String)
    status = Column(String, default="pending")
    verified_by = Column(String, nullable=True)      # 領取時核對驗證描述的站務員，留空代表尚未核對
    verified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class ItemTransferLog(Base):
    __tablename__ = "item_transfer_logs"
    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, index=True)
    station = Column(String)
    status = Column(String, nullable=True)
    operator = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class SensitiveAccessLog(Base):
    __tablename__ = "sensitive_access_logs"
    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, index=True)
    operator = Column(String, nullable=True)
    accessed_at = Column(DateTime, default=datetime.utcnow)

class AICorrectionLog(Base):
    __tablename__ = "ai_correction_logs"
    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, nullable=True)
    field_name = Column(String)
    ai_value = Column(String)
    corrected_value = Column(String)
    item_category = Column(String, nullable=True)  # 物品最終的台鐵分類，用於拆分修正率到分類維度
    created_at = Column(DateTime, default=datetime.utcnow)

class AIAnalysisLog(Base):
    """每次成功呼叫 /api/analyze-image 時，記錄哪些欄位被 AI 產生了建議值，作為計算修正率的分母。
    同時記錄 AI 當時建議的分類（ai_suggested_category），讓修正率可以拆到「分類 x 欄位」維度，
    避免不同分類的辨識難易度混在一起計算，稀釋掉真正需要警示的情況（例如顏色辨識在 3C 類很準，在一般物品類卻常出錯）。"""
    __tablename__ = "ai_analysis_logs"
    id = Column(Integer, primary_key=True, index=True)
    fields_suggested = Column(String)  # 逗號分隔，例如 "category,tags,colors,description"
    ai_suggested_category = Column(String, nullable=True)  # AI 辨識當時建議的物品分類（非台鐵 6 類，是 AI 原始判斷，如「手機」「皮夾」）
    created_at = Column(DateTime, default=datetime.utcnow)

class DeliveryRule(Base):
    __tablename__ = "delivery_rules"
    id = Column(Integer, primary_key=True, index=True)
    category_keyword = Column(String, unique=True)
    allowed = Column(Boolean, default=True)
    reason = Column(String, nullable=True)

class ImageSearchFeedback(Base):
    __tablename__ = "image_search_feedback"
    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, nullable=True)
    extracted_keywords = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# create_all() 只會建立全新的表，不會替已存在的表補欄位。
# 因為 lost_found.db 在新增台鐵格式欄位前就已存在，這裡手動檢查並補上缺少的欄位。
def _ensure_columns_exist():
    with engine.connect() as conn:
        existing_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(lost_items)").fetchall()}
        required_cols = {
            "item_name": "VARCHAR",
            "pickup_type": "VARCHAR",
            "station_code": "VARCHAR",
            "custodian_unit": "VARCHAR",
            "service_phone": "VARCHAR",
            "item_code": "VARCHAR",
        }
        for col_name, col_type in required_cols.items():
            if col_name not in existing_cols:
                conn.exec_driver_sql(f"ALTER TABLE lost_items ADD COLUMN {col_name} {col_type}")

        wishlist_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(wishlist)").fetchall()}
        if "user_phone" not in wishlist_cols:
            conn.exec_driver_sql("ALTER TABLE wishlist ADD COLUMN user_phone VARCHAR")
        if "matched_item_id" not in wishlist_cols:
            conn.exec_driver_sql("ALTER TABLE wishlist ADD COLUMN matched_item_id INTEGER")
        if "expires_at" not in wishlist_cols:
            conn.exec_driver_sql("ALTER TABLE wishlist ADD COLUMN expires_at DATETIME")

        delivery_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(delivery_requests)").fetchall()}
        if "verified_by" not in delivery_cols:
            conn.exec_driver_sql("ALTER TABLE delivery_requests ADD COLUMN verified_by VARCHAR")
        if "verified_at" not in delivery_cols:
            conn.exec_driver_sql("ALTER TABLE delivery_requests ADD COLUMN verified_at DATETIME")

        correction_log_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(ai_correction_logs)").fetchall()}
        if "item_category" not in correction_log_cols:
            conn.exec_driver_sql("ALTER TABLE ai_correction_logs ADD COLUMN item_category VARCHAR")

        analysis_log_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(ai_analysis_logs)").fetchall()}
        if "ai_suggested_category" not in analysis_log_cols:
            conn.exec_driver_sql("ALTER TABLE ai_analysis_logs ADD COLUMN ai_suggested_category VARCHAR")

        conn.commit()

_ensure_columns_exist()

# 預設配送限制規則（首次啟動時 seed）
_seed_db = SessionLocal()
if _seed_db.query(DeliveryRule).count() == 0:
    _seed_db.add(DeliveryRule(
        category_keyword="3C",
        allowed=False,
        reason="依台鐵新制規定，3C 電子產品不受理跨站配送，請親自至保管車站領取"
    ))
    _seed_db.commit()
_seed_db.close()

# --- FastAPI 應用設定 ---
class StatusUpdate(BaseModel):
    status: str

class BatchRequest(BaseModel):
    item_ids: List[int]

class BatchStatusRequest(BaseModel):
    item_ids: List[int]
    status: str

class WishlistCreate(BaseModel):
    user_name: str
    user_email: str
    user_phone: str = ""
    category: str = "全部"  # 不再用於配對邏輯，保留欄位僅供後台參考顯示
    keywords: str

class DeliveryRequestCreate(BaseModel):
    item_id: int
    customer_name: str
    customer_phone: str
    customer_email: str
    proof_description: str
    target_station: str

class DeliveryStatusUpdate(BaseModel):
    status: str

class ScanQRRequest(BaseModel):
    qr_code_id: str
    new_station: str
    new_status: str

app = FastAPI()

# 確保上傳資料夾存在
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# 確保靜態資料夾存在
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

# 掛載靜態檔案 (供讀取圖片與前端腳本)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=BASE_DIR)

# --- 寄信邏輯 (Phase 3) ---
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
GMAIL_ACCOUNT = os.environ.get("GMAIL_ACCOUNT", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

def send_match_email(to_email: str, user_name: str, item: LostItem):
    if not GMAIL_APP_PASSWORD:
        return
        
    try:
        msg = MIMEMultipart()
        msg['From'] = GMAIL_ACCOUNT
        msg['To'] = to_email
        msg['Subject'] = f"【鐵道尋蹤】遺失物尋獲可能通知：{item.description[:10]}..."
        
        body = f"""親愛的 {user_name} 您好：
        
系統發現有一件新登錄的遺失物可能符合您在「許願池」協尋的條件！

【物品資訊】
- 類別：{item.category}
- 特徵：{item.description}
- 拾獲地點：{item.found_location}
- 拾獲時間：{item.found_time.strftime("%Y-%m-%d %H:%M")}

請登入「鐵道尋蹤」平台首頁查看最新拾獲紀錄，若確認為您的物品，請盡速前往該車站進行認領。

祝您順心
鐵道尋蹤 系統自動通知
"""
        msg.attach(MIMEText(body, 'plain'))
        
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(GMAIL_ACCOUNT, GMAIL_APP_PASSWORD)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print(f"Email failed: {e}")

def send_already_claimed_match_email(to_email: str, user_name: str, item: LostItem):
    """許願池比對到的物品其實已經被領回（可能是別人，也可能旅客本人忘了登記就直接領走），
    主動告知旅客並請其確認，避免站務員單方面判斷後就結案，卻其實配對錯誤。"""
    if not GMAIL_APP_PASSWORD:
        return

    try:
        msg = MIMEMultipart()
        msg['From'] = GMAIL_ACCOUNT
        msg['To'] = to_email
        msg['Subject'] = f"【鐵道尋蹤】請確認：系統找到一件可能符合的物品（已被領回）"

        body = f"""親愛的 {user_name} 您好：

系統比對到一件特徵可能符合您在「許願池」協尋的物品，但這件物品狀態顯示已經被領回：

【物品資訊】
- 類別：{item.category}
- 特徵：{item.description}
- 拾獲地點：{item.found_location}

如果這正是您本人已經領回的物品，請忽略此信，我們會將此筆許願請求結案。
如果您尚未領回任何物品、這件被別人誤領了，請盡速回覆此信或聯繫站務人員協助確認，以保障您的權益。

祝您順心
鐵道尋蹤 系統自動通知
"""
        msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(GMAIL_ACCOUNT, GMAIL_APP_PASSWORD)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print(f"Email failed: {e}")

def send_delivery_notification(email: str, item_desc: str, target_station: str):
    if not GMAIL_ACCOUNT or not GMAIL_APP_PASSWORD:
        return
    try:
        msg = MIMEMultipart()
        msg['From'] = GMAIL_ACCOUNT
        msg['To'] = email
        msg['Subject'] = f"[鐵道尋蹤] 您的物品已送達 {target_station}！"
        
        body = f"""親愛的旅客您好：

您所申請認領的物品已成功送達指定的【{target_station}】！

【物品資訊】
- 特徵：{item_desc}

請您攜帶有照片之身分證件，於營業時間內前往 {target_station} 服務台進行核對與領取。
若該物品屬於高價值物品，可能需要您提供其他證明（如報案單、詳細特徵照片等）。

祝您順心
鐵道尋蹤 系統自動通知
"""
        msg.attach(MIMEText(body, 'plain'))
        
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(GMAIL_ACCOUNT, GMAIL_APP_PASSWORD)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print(f"Delivery Email failed: {e}")

VALID_STATUS_TRANSITIONS = {
    "pending": {"claimed", "delivering"},
    "delivering": {"arrived", "pending"},
    "arrived": {"pending", "claimed"},  # claimed：站務員核對驗證描述完成領取（verify-pickup）後才允許
    "claimed": {"pending"},
}

def is_valid_transition(old_status: str, new_status: str) -> bool:
    if not old_status or old_status == new_status:
        return True
    if new_status == "pending":
        return True
    return new_status in VALID_STATUS_TRANSITIONS.get(old_status, set())

def gemini_error_message(e: Exception) -> str:
    if isinstance(e, ResourceExhausted):
        return "AI 額度已達上限，請稍後再試（通常 1 分鐘內會恢復）"
    return f"AI 辨識發生錯誤：{str(e)}"

def active_unexpired_wishes_query(db):
    """許願池比對前先篩除已關閉或已逾期（超過 90 天未結案）的登記，避免隨資料量增長每次都全表掃描並耗用 AI 額度"""
    return db.query(Wishlist).filter(
        Wishlist.is_active == True,
        or_(Wishlist.expires_at.is_(None), Wishlist.expires_at > datetime.utcnow())
    )

def keyword_match_score(keywords: list, item) -> int:
    """逐字比對備援：把關鍵字拆成單字，部分命中也算分數，緩解用詞不同（水壺 vs 保溫杯）造成的漏配對"""
    target_text = f"{item.description or ''} {item.tags or ''} {item.train_number or ''} {item.found_location or ''}"
    score = 0
    for kw in keywords:
        if kw in target_text:
            score += 2
            continue
        # 拆字比對：關鍵字中的每個字若有一半以上出現在物品文字中，視為部分命中
        chars = [c for c in kw if c.strip()]
        if chars:
            hit_chars = sum(1 for c in chars if c in target_text)
            if hit_chars / len(chars) >= 0.5:
                score += 1
    return score

def ai_semantic_match(wish_text: str, candidates: list) -> set:
    """用 Gemini 對「許願描述」與「候選物品清單」做一次語意比對，避免用詞不同（水壺 vs 保溫杯）造成漏配對。
    一次請求比對全部候選物品，不逐筆呼叫，節省 API 額度。回傳判定為相似的物品 id 集合。"""
    if not GEMINI_API_KEY or not candidates:
        return set()
    try:
        model = genai.GenerativeModel('gemini-flash-latest')
        items_text = "\n".join(
            f"id={item.id}: {item.description or ''} {item.tags or ''}"
            for item in candidates
        )
        prompt = f"""你是遺失物配對助理。旅客描述他遺失的物品特徵如下：
「{wish_text}」

以下是目前資料庫中的拾獲物品清單（id 與描述）：
{items_text}

請判斷哪些物品「可能」是旅客描述的同一件物品（即使用詞不同，例如「水壺」與「保溫杯」、「掉漆」與「漆面剝落」視為相似，只要特徵、顏色、外型相符即可）。
只回傳符合的物品 id，以逗號分隔（例如：3,8,12）。如果沒有任何符合，回傳空字串。不要回答其他文字。"""
        response = model.generate_content(prompt)
        text = response.text.strip()
        if not text:
            return set()
        ids = set()
        for part in text.split(','):
            part = part.strip()
            if part.isdigit():
                ids.add(int(part))
        return ids
    except Exception:
        return set()

def check_wishlist_and_notify(item_id: int):
    db = SessionLocal()
    item = db.query(LostItem).filter(LostItem.id == item_id).first()
    if not item:
        db.close()
        return
    active_wishes = active_unexpired_wishes_query(db).all()

    for wish in active_wishes:
        # 不依賴物品分類比對：站務員與民眾對「分類」的認知常常不同（例如同一物品，
        # 站務員標記為「其他特殊物品」，民眾以為是「一般物品」），分類錯位會導致明明特徵相符卻配對失敗。
        # 因此僅以物品特徵（描述/標籤/拾獲地點/車次）與關鍵字進行比對，並用 AI 語意比對輔助同義詞情況。
        if wish.keywords:
            keywords = [k.strip() for k in wish.keywords.split(' ') if k.strip()]
            score = keyword_match_score(keywords, item)
            matched = score > 0
            if not matched:
                ai_matched_ids = ai_semantic_match(wish.keywords, [item])
                matched = item.id in ai_matched_ids
            if not matched:
                continue

        send_match_email(wish.user_email, wish.user_name, item)

    db.close()

# --- API 路由 ---

@app.get("/", response_class=HTMLResponse)
async def read_index(request: Request):
    """回傳前端網頁"""
    return templates.TemplateResponse(request, "index.html", {})

@app.get("/login", response_class=HTMLResponse)
async def read_login(request: Request):
    """回傳登入網頁"""
    return templates.TemplateResponse(request, "login.html", {})

@app.post("/login")
async def process_login(request: Request, username: str = Form(...), password: str = Form(...)):
    """處理登入邏輯"""
    if username == "admin" and password == "admin123":
        response = RedirectResponse(url="/admin", status_code=303)
        response.set_cookie(key="admin_token", value="admin_secret", httponly=True)
        return response
    return templates.TemplateResponse(request, "login.html", {"error": "帳號或密碼錯誤"})

@app.get("/logout")
async def process_logout():
    """處理登出邏輯"""
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key="admin_token")
    return response

def verify_api_admin(request: Request):
    if request.cookies.get("admin_token") != "admin_secret":
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.post("/api/items", dependencies=[Depends(verify_api_admin)])
async def create_item(
    background_tasks: BackgroundTasks,
    category: str = Form(...),
    item_name: str = Form(None),
    description: str = Form(...),
    found_location: str = Form(...),
    train_type: str = Form(None),
    train_number: str = Form(None),
    seat_number: str = Form(None),
    tags: str = Form(None),
    current_station: str = Form(None),
    is_sensitive: bool = Form(False),
    is_perishable: bool = Form(False),
    owner_name: str = Form(None),
    card_last_four: str = Form(None),
    pickup_type: str = Form(None),
    station_code: str = Form(None),
    custodian_unit: str = Form(None),
    service_phone: str = Form(None),
    image: UploadFile = File(...),
    additional_images: List[UploadFile] = File(None)
):
    """接收前端表單並存入資料庫"""
    # 檢查是否為危險品或違禁品
    danger_keywords = ["危險品", "違禁品", "易燃物", "爆裂物", "槍械", "毒品", "炸藥"]
    check_text = (description + " " + (tags or "")).lower()
    for kw in danger_keywords:
        if kw in check_text:
            raise HTTPException(status_code=400, detail="此為危險品/違禁品，請直接通報鐵路警察處理，不得登錄為遺失物")
            
    # 檢查是否為易腐壞食物
    perishable_keywords = ["食物", "易腐壞", "便當", "生鮮", "食品"]
    if is_perishable or any(kw in check_text for kw in perishable_keywords):
        description = f"[易腐壞/當日作廢] {description}"

    # 儲存圖片
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    safe_filename = f"{timestamp}_{image.filename}"
    file_path = os.path.join(UPLOAD_DIR, safe_filename)
    
    with open(file_path, "wb") as buffer:
        buffer.write(await image.read())
        
    # 儲存附加圖片
    additional_files = []
    if additional_images:
        for i, add_img in enumerate(additional_images):
            if add_img.filename:
                add_safe_name = f"{timestamp}_add_{i}_{add_img.filename}"
                add_path = os.path.join(UPLOAD_DIR, add_safe_name)
                with open(add_path, "wb") as buffer:
                    buffer.write(await add_img.read())
                additional_files.append(add_safe_name)
    additional_images_str = ",".join(additional_files)
        
    # 生成 QR Code
    qr_id = str(uuid.uuid4())
    qr_filename = f"qr_{qr_id}.png"
    qr_path = os.path.join(UPLOAD_DIR, qr_filename)
    qr = qrcode.make(qr_id)
    qr.save(qr_path)
        
    # 寫入資料庫
    db = SessionLocal()

    # 產生物品代號，格式參照台鐵官方公告：{站碼}-026-{序號}
    item_code = None
    if station_code:
        seq = db.query(LostItem).count() + 45000
        item_code = f"{station_code}-026-{seq}"

    new_item = LostItem(
        category=category,
        item_name=item_name,
        description=description,
        found_location=found_location,
        image_filename=safe_filename,
        additional_images=additional_images_str,
        train_type=train_type,
        train_number=train_number,
        seat_number=seat_number,
        tags=tags,
        qr_code_id=qr_id,
        current_station=current_station,
        is_sensitive=is_sensitive,
        owner_name=owner_name,
        card_last_four=card_last_four,
        pickup_type=pickup_type,
        station_code=station_code,
        custodian_unit=custodian_unit,
        service_phone=service_phone,
        item_code=item_code
    )
    db.add(new_item)
    db.commit()
    db.refresh(new_item)
    new_item_id = new_item.id
    new_item_status = new_item.status

    # 寫入第一筆轉移紀錄（初始拾獲站點）
    db.add(ItemTransferLog(item_id=new_item_id, station=current_station, status=new_item_status, operator="admin"))
    db.commit()
    db.close()

    # 觸發背景通知邏輯
    background_tasks.add_task(check_wishlist_and_notify, new_item_id)

    return {"status": "success", "item_id": new_item_id}

def serialize_item(item: LostItem) -> dict:
    """序列化遺失物，遮蔽敏感欄位（完整內容需透過 /api/admin/items/{id}/sensitive 取得並留下查看紀錄）"""
    return {
        "id": item.id,
        "category": item.category,
        "item_name": item.item_name,
        "description": item.description,
        "found_location": item.found_location,
        "image_filename": item.image_filename,
        "additional_images": item.additional_images,
        "train_type": item.train_type,
        "train_number": item.train_number,
        "seat_number": item.seat_number,
        "found_time": item.found_time,
        "status": item.status,
        "tags": item.tags,
        "qr_code_id": item.qr_code_id,
        "current_station": item.current_station,
        "is_sensitive": item.is_sensitive,
        "owner_name": "***" if item.owner_name else None,
        "card_last_four": "****" if item.card_last_four else None,
        "pickup_type": item.pickup_type,
        "station_code": item.station_code,
        "custodian_unit": item.custodian_unit,
        "service_phone": item.service_phone,
        "item_code": item.item_code,
    }

@app.get("/api/items")
async def get_items():
    """取得所有遺失物清單供前端顯示"""
    db = SessionLocal()
    items = db.query(LostItem).order_by(LostItem.found_time.desc()).all()
    db.close()
    return [serialize_item(i) for i in items]

@app.get("/api/search")
async def search_items(
    q: str = None,
    train_number: str = None,
    date_start: str = None,
    date_end: str = None,
    time_hint: str = None
):
    """旅客端：多維度智能搜尋"""
    db = SessionLocal()
    query = db.query(LostItem)

    if q:
        # 簡單的模糊搜尋，包含描述或標籤
        search_filter = f"%{q}%"
        query = query.filter(
            (LostItem.description.like(search_filter)) |
            (LostItem.tags.like(search_filter)) |
            (LostItem.category.like(search_filter))
        )

    if train_number:
        query = query.filter(LostItem.train_number == train_number)

    # 漸進式問答搜尋使用的模糊時間範圍提示
    if time_hint and not date_start:
        from datetime import timedelta
        now = datetime.now()
        hint_to_days = {"今天": 1, "這週": 7, "這個月": 31}
        days = hint_to_days.get(time_hint)
        if days:
            date_start = (now - timedelta(days=days)).strftime("%Y-%m-%d")

    if date_start:
        try:
            start_dt = datetime.strptime(date_start, "%Y-%m-%d")
            query = query.filter(LostItem.found_time >= start_dt)
        except ValueError:
            pass
            
    if date_end:
        try:
            from datetime import timedelta
            end_dt = datetime.strptime(date_end, "%Y-%m-%d")
            end_dt = end_dt + timedelta(days=1)
            query = query.filter(LostItem.found_time < end_dt)
        except ValueError:
            pass
            
    items = query.order_by(LostItem.found_time.desc()).all()
    db.close()
    return [serialize_item(i) for i in items]

@app.get("/api/admin/items/{item_id}/sensitive", dependencies=[Depends(verify_api_admin)])
def get_sensitive_item_info(item_id: int):
    """後台：查看物品完整敏感資訊（會留下查看紀錄）"""
    db = SessionLocal()
    item = db.query(LostItem).filter(LostItem.id == item_id).first()
    if not item:
        db.close()
        raise HTTPException(status_code=404, detail="Item not found")

    log = SensitiveAccessLog(item_id=item_id, operator="admin")
    db.add(log)
    db.commit()

    result = {
        "owner_name": item.owner_name,
        "card_last_four": item.card_last_four,
    }
    db.close()
    return result

@app.get("/api/admin/items/{item_id}/access-logs", dependencies=[Depends(verify_api_admin)])
def get_sensitive_access_logs(item_id: int):
    """後台：查看該物品的敏感資訊存取紀錄"""
    db = SessionLocal()
    logs = db.query(SensitiveAccessLog).filter(SensitiveAccessLog.item_id == item_id).order_by(SensitiveAccessLog.accessed_at.desc()).all()
    db.close()
    return logs

@app.post("/api/search-by-image")
async def search_by_image(image: UploadFile = File(...)):
    """旅客端：以圖找圖 (使用 Gemini 提取特徵後比對資料庫)"""
    if not GEMINI_API_KEY:
        return {"status": "error", "message": "API key not set"}
        
    try:
        image_data = await image.read()
        img = Image.open(io.BytesIO(image_data))
        
        model = genai.GenerativeModel('gemini-flash-latest')
        prompt = "你是一個遺失物搜尋助理。請分析圖片並以逗號分隔列出最重要的 3 到 5 個外觀特徵與顏色（如：藍色,保溫瓶,不鏽鋼,黑色蓋子）。不要回答其他任何文字。"
        response = model.generate_content([prompt, img])
        
        keywords = [k.strip() for k in response.text.split(',') if k.strip()]
        
        db = SessionLocal()
        items = db.query(LostItem).order_by(LostItem.found_time.desc()).all()
        db.close()
        
        results = []
        for item in items:
            score = 0
            # 不比對 category：分類由站務員或 AI 判斷，認知可能與旅客不同，僅以物品實際特徵比對
            target_text = f"{item.description} {item.tags}".lower()
            for kw in keywords:
                if kw.lower() in target_text:
                    score += 1
            if score > 0:
                results.append((score, item))

        results.sort(key=lambda x: x[0], reverse=True)
        top_results = results[:10]

        matched_items = []
        for score, item in top_results:
            confidence = score / len(keywords) if keywords else 0
            item_dict = serialize_item(item)
            item_dict["match_score"] = score
            item_dict["confidence"] = round(confidence, 2)
            item_dict["confidence_level"] = "high" if confidence >= 0.5 else "low"
            matched_items.append(item_dict)

        return {
            "status": "success",
            "extracted_keywords": keywords,
            "items": matched_items
        }
    except Exception as e:
        return {"status": "error", "message": gemini_error_message(e)}

@app.post("/api/search-feedback")
async def search_feedback(item_id: int = Form(...), extracted_keywords: str = Form("")):
    """旅客端：標記以圖找圖結果不相符，記錄供未來分析"""
    db = SessionLocal()
    feedback = ImageSearchFeedback(item_id=item_id, extracted_keywords=extracted_keywords)
    db.add(feedback)
    db.commit()
    db.close()
    return {"status": "success"}

@app.post("/api/wishlist")
async def create_wishlist(wish: WishlistCreate):
    """旅客端：登記許願池協尋"""
    db = SessionLocal()
    new_wish = Wishlist(
        user_name=wish.user_name,
        user_email=wish.user_email,
        user_phone=wish.user_phone,
        keywords=wish.keywords,
        category=wish.category,
        expires_at=datetime.utcnow() + timedelta(days=WISHLIST_EXPIRY_DAYS)
    )
    db.add(new_wish)
    db.commit()

    # 計算目前資料庫中可能相似的物品，供前端直接顯示給使用者確認
    # 不依分類篩選候選清單：分類認知因人而異，僅以物品特徵關鍵字比對
    matched_items = []
    keywords = [k.strip() for k in (wish.keywords or '').split(' ') if k.strip()]
    if keywords:
        candidates = db.query(LostItem).all()
        matched_by_id = {}
        unmatched_candidates = []
        for item in candidates:
            if keyword_match_score(keywords, item) > 0:
                matched_by_id[item.id] = item
            else:
                unmatched_candidates.append(item)
        # 關鍵字/拆字比對沒中的，再用 AI 語意比對一次，處理用詞不同的情況（水壺 vs 保溫杯）
        ai_matched_ids = ai_semantic_match(wish.keywords, unmatched_candidates)
        id_to_item = {item.id: item for item in unmatched_candidates}
        for mid in ai_matched_ids:
            if mid in id_to_item:
                matched_by_id[mid] = id_to_item[mid]
        matched_items = [serialize_item(item) for item in matched_by_id.values()]

        # 若相符物品中已經有人領回了（許願時才登記，但物品早就被領走），
        # 直接標記 matched_item_id，讓後台「智慧許願池管理」也能看到「可能已找到」提示，不會漏掉這種情況
        already_claimed = next((item for item in matched_by_id.values() if item.status == "claimed"), None)
        if already_claimed:
            new_wish.matched_item_id = already_claimed.id
            db.commit()

    db.close()
    return {"status": "success", "similar_count": len(matched_items), "matched_items": matched_items}

class WishlistStatusUpdate(BaseModel):
    is_active: bool

@app.get("/api/admin/wishlists", dependencies=[Depends(verify_api_admin)])
def get_wishlists():
    """後台：取得所有許願池登記，附帶可能相符的物品摘要（供「可能已找到」提示）"""
    db = SessionLocal()
    wishes = db.query(Wishlist).order_by(Wishlist.created_at.desc()).all()
    result = []
    for w in wishes:
        matched_item_summary = None
        if w.matched_item_id:
            matched_item = db.query(LostItem).filter(LostItem.id == w.matched_item_id).first()
            if matched_item:
                matched_item_summary = {
                    "id": matched_item.id,
                    "item_name": matched_item.item_name,
                    "description": matched_item.description,
                    "status": matched_item.status,
                }
        result.append({
            "id": w.id,
            "user_name": w.user_name,
            "user_email": w.user_email,
            "user_phone": w.user_phone,
            "category": w.category,
            "keywords": w.keywords,
            "is_active": w.is_active,
            "created_at": w.created_at,
            "expires_at": w.expires_at,
            "is_expired": bool(w.expires_at and w.expires_at <= datetime.utcnow()),
            "matched_item_id": w.matched_item_id,
            "matched_item": matched_item_summary,
        })
    db.close()
    return result

@app.put("/api/admin/wishlists/{wish_id}", dependencies=[Depends(verify_api_admin)])
def update_wishlist_status(wish_id: int, update: WishlistStatusUpdate):
    """後台：開啟/關閉許願池協尋"""
    db = SessionLocal()
    wish = db.query(Wishlist).filter(Wishlist.id == wish_id).first()
    if not wish:
        db.close()
        raise HTTPException(status_code=404, detail="Wishlist not found")
    wish.is_active = update.is_active
    db.commit()
    db.close()
    return {"status": "success"}

@app.delete("/api/admin/wishlists/{wish_id}", dependencies=[Depends(verify_api_admin)])
def delete_wishlist(wish_id: int):
    """後台：刪除許願池登記"""
    db = SessionLocal()
    wish = db.query(Wishlist).filter(Wishlist.id == wish_id).first()
    if not wish:
        db.close()
        raise HTTPException(status_code=404, detail="Wishlist not found")
    db.delete(wish)
    db.commit()
    db.close()
    return {"status": "success"}

@app.post("/api/delivery")
def create_delivery_request(req: DeliveryRequestCreate):
    db = SessionLocal()
    item = db.query(LostItem).filter(LostItem.id == req.item_id).first()
    if not item:
        db.close()
        raise HTTPException(status_code=404, detail="Item not found")

    blocked_rule = db.query(DeliveryRule).filter(DeliveryRule.allowed == False).all()
    for rule in blocked_rule:
        if rule.category_keyword in item.category:
            db.close()
            raise HTTPException(status_code=400, detail=rule.reason or f"{rule.category_keyword} 不受理跨站配送")

    existing_active = db.query(DeliveryRequest).filter(
        DeliveryRequest.item_id == req.item_id,
        DeliveryRequest.status.in_(["pending", "delivering"])
    ).first()
    if existing_active:
        db.close()
        raise HTTPException(status_code=400, detail="此物品已有配送申請正在處理中，請勿重複申請")

    new_req = DeliveryRequest(
        item_id=req.item_id,
        customer_name=req.customer_name,
        customer_phone=req.customer_phone,
        customer_email=req.customer_email,
        proof_description=req.proof_description,
        target_station=req.target_station,
        status="pending"
    )
    db.add(new_req)
    db.commit()
    db.close()
    return {"status": "success"}

@app.get("/api/admin/deliveries", dependencies=[Depends(verify_api_admin)])
def get_deliveries():
    db = SessionLocal()
    reqs = db.query(DeliveryRequest).order_by(DeliveryRequest.created_at.desc()).all()
    result = []
    for r in reqs:
        item = db.query(LostItem).filter(LostItem.id == r.item_id).first()
        item_desc = item.description if item else "物品已刪除"
        item_img = item.image_filename if item else ""
        result.append({
            "id": r.id,
            "item_id": r.item_id,
            "customer_name": r.customer_name,
            "customer_phone": r.customer_phone,
            "customer_email": r.customer_email,
            "proof_description": r.proof_description,
            "target_station": r.target_station,
            "status": r.status,
            "verified_by": r.verified_by,
            "verified_at": r.verified_at.strftime("%Y-%m-%d %H:%M") if r.verified_at else None,
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M"),
            "item_desc": item_desc,
            "item_img": item_img
        })
    db.close()
    return result

@app.put("/api/admin/deliveries/{req_id}/status", dependencies=[Depends(verify_api_admin)])
def update_delivery_status(req_id: int, update: DeliveryStatusUpdate, background_tasks: BackgroundTasks):
    db = SessionLocal()
    req = db.query(DeliveryRequest).filter(DeliveryRequest.id == req_id).first()
    if not req:
        db.close()
        raise HTTPException(status_code=404, detail="Request not found")
        
    item = db.query(LostItem).filter(LostItem.id == req.item_id).first()
    if item and not is_valid_transition(item.status, update.status):
        db.close()
        if item.status in ("arrived", "claimed"):
            raise HTTPException(status_code=400, detail="此物品已完成配送或已被領回，這是一筆重複/過期的申請，請取消此筆申請")
        raise HTTPException(status_code=400, detail=f"不合理的狀態轉換：{item.status} → {update.status}")

    req.status = update.status

    if item:
        if update.status == "delivering":
            item.status = "delivering"
        elif update.status == "arrived":
            item.status = "arrived"
            item.current_station = req.target_station
            db.add(ItemTransferLog(item_id=item.id, station=req.target_station, status="arrived", operator="admin"))

    db.commit()

    if update.status == "arrived" and item:
        background_tasks.add_task(send_delivery_notification, req.customer_email, item.description, req.target_station)

    db.close()
    return {"status": "success"}

@app.delete("/api/admin/deliveries/{req_id}", dependencies=[Depends(verify_api_admin)])
def cancel_delivery_request(req_id: int):
    """後台：取消/刪除一筆配送申請（用於清除重複或過期的申請）"""
    db = SessionLocal()
    req = db.query(DeliveryRequest).filter(DeliveryRequest.id == req_id).first()
    if not req:
        db.close()
        raise HTTPException(status_code=404, detail="Request not found")
    db.delete(req)
    db.commit()
    db.close()
    return {"status": "success"}

@app.put("/api/admin/deliveries/{req_id}/verify-pickup", dependencies=[Depends(verify_api_admin)])
def verify_delivery_pickup(req_id: int, background_tasks: BackgroundTasks):
    """後台：旅客到站領取時，站務員核對驗證描述後執行此動作，才會將物品標記為已領回（claimed）。
    這個步驟與「送達並通知」分開，確保物品不會在尚未實際核對領取人身分/驗證描述前就被標記結案。"""
    db = SessionLocal()
    req = db.query(DeliveryRequest).filter(DeliveryRequest.id == req_id).first()
    if not req:
        db.close()
        raise HTTPException(status_code=404, detail="Request not found")

    if req.status != "arrived":
        db.close()
        raise HTTPException(status_code=400, detail="此配送申請尚未送達車站，無法核對領取")

    item = db.query(LostItem).filter(LostItem.id == req.item_id).first()
    if item and not is_valid_transition(item.status, "claimed"):
        db.close()
        raise HTTPException(status_code=400, detail=f"不合理的狀態轉換：{item.status} → claimed")

    req.verified_by = "admin"
    req.verified_at = datetime.utcnow()

    if item:
        item.status = "claimed"
        db.add(ItemTransferLog(item_id=item.id, station=req.target_station, status="claimed", operator="admin"))
        db.commit()

        active_wishes = active_unexpired_wishes_query(db).filter(Wishlist.matched_item_id.is_(None)).all()
        for wish in active_wishes:
            if not wish.keywords:
                continue
            keywords = [k.strip() for k in wish.keywords.split(' ') if k.strip()]
            matched = keyword_match_score(keywords, item) > 0
            if not matched:
                matched = item.id in ai_semantic_match(wish.keywords, [item])
            if matched:
                wish.matched_item_id = item.id
                background_tasks.add_task(send_already_claimed_match_email, wish.user_email, wish.user_name, item)
        db.commit()
    else:
        db.commit()

    db.close()
    return {"status": "success"}

@app.get("/admin", response_class=HTMLResponse)
async def read_admin(request: Request):
    """回傳後台管理網頁"""
    if request.cookies.get("admin_token") != "admin_secret":
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "admin.html", {})

@app.delete("/api/items/{item_id}", dependencies=[Depends(verify_api_admin)])
async def delete_item(item_id: int):
    """刪除遺失物紀錄與照片"""
    db = SessionLocal()
    item = db.query(LostItem).filter(LostItem.id == item_id).first()
    if not item:
        db.close()
        raise HTTPException(status_code=404, detail="Item not found")

    # 刪除實體照片
    try:
        file_path = os.path.join(UPLOAD_DIR, item.image_filename)
        if os.path.exists(file_path):
            os.remove(file_path)
        if item.additional_images:
            for add_img in item.additional_images.split(','):
                if add_img:
                    add_path = os.path.join(UPLOAD_DIR, add_img)
                    if os.path.exists(add_path):
                        os.remove(add_path)
    except Exception as e:
        pass

    # 連帶刪除關聯紀錄，避免日後同一 id 被重用時誤判為「已有進行中配送」等問題
    db.query(DeliveryRequest).filter(DeliveryRequest.item_id == item_id).delete()
    db.query(ItemTransferLog).filter(ItemTransferLog.item_id == item_id).delete()
    db.query(SensitiveAccessLog).filter(SensitiveAccessLog.item_id == item_id).delete()

    db.delete(item)
    db.commit()
    db.close()
    return {"status": "success", "message": "Item deleted"}

@app.put("/api/items/{item_id}/status", dependencies=[Depends(verify_api_admin)])
async def update_item_status(item_id: int, status_update: StatusUpdate, background_tasks: BackgroundTasks):
    """更新遺失物狀態"""
    db = SessionLocal()
    item = db.query(LostItem).filter(LostItem.id == item_id).first()
    if not item:
        db.close()
        raise HTTPException(status_code=404, detail="Item not found")

    old_status = item.status
    item.status = status_update.status
    db.commit()

    new_status = status_update.status
    new_item_id = item.id

    # 物品被標記領回時，找出特徵可能相符的許願池條目，記錄起來供後台「可能已找到」提示，由管理員確認後手動結案
    if new_status == "claimed":
        active_wishes = active_unexpired_wishes_query(db).filter(Wishlist.matched_item_id.is_(None)).all()
        for wish in active_wishes:
            if not wish.keywords:
                continue
            keywords = [k.strip() for k in wish.keywords.split(' ') if k.strip()]
            matched = keyword_match_score(keywords, item) > 0
            if not matched:
                matched = item.id in ai_semantic_match(wish.keywords, [item])
            if matched:
                wish.matched_item_id = item.id
                background_tasks.add_task(send_already_claimed_match_email, wish.user_email, wish.user_name, item)
        db.commit()

    db.close()

    # 退回 pending 等同於「物品重新可被認領」，對許願池來說跟新物品入庫一樣需要重新比對通知
    if new_status == "pending" and old_status != "pending":
        background_tasks.add_task(check_wishlist_and_notify, new_item_id)

    return {"status": "success", "new_status": new_status}

@app.post("/api/items/batch-delete", dependencies=[Depends(verify_api_admin)])
async def batch_delete_items(req: BatchRequest):
    db = SessionLocal()
    items = db.query(LostItem).filter(LostItem.id.in_(req.item_ids)).all()

    for item in items:
        try:
            file_path = os.path.join(UPLOAD_DIR, item.image_filename)
            if os.path.exists(file_path):
                os.remove(file_path)
            if item.additional_images:
                for add_img in item.additional_images.split(','):
                    if add_img:
                        add_path = os.path.join(UPLOAD_DIR, add_img)
                        if os.path.exists(add_path):
                            os.remove(add_path)
        except Exception:
            pass
        db.delete(item)

    db.query(DeliveryRequest).filter(DeliveryRequest.item_id.in_(req.item_ids)).delete(synchronize_session=False)
    db.query(ItemTransferLog).filter(ItemTransferLog.item_id.in_(req.item_ids)).delete(synchronize_session=False)
    db.query(SensitiveAccessLog).filter(SensitiveAccessLog.item_id.in_(req.item_ids)).delete(synchronize_session=False)

    db.commit()
    db.close()
    return {"status": "success", "message": f"Deleted {len(items)} items"}

@app.post("/api/items/batch-status", dependencies=[Depends(verify_api_admin)])
async def batch_update_status(req: BatchStatusRequest, background_tasks: BackgroundTasks):
    db = SessionLocal()
    items = db.query(LostItem).filter(LostItem.id.in_(req.item_ids)).all()
    old_statuses = {item.id: item.status for item in items}
    item_ids = [item.id for item in items]

    for item in items:
        item.status = req.status

    db.commit()

    if req.status == "claimed":
        active_wishes = active_unexpired_wishes_query(db).filter(Wishlist.matched_item_id.is_(None)).all()
        for item in items:
            for wish in active_wishes:
                if wish.matched_item_id is not None or not wish.keywords:
                    continue
                keywords = [k.strip() for k in wish.keywords.split(' ') if k.strip()]
                matched = keyword_match_score(keywords, item) > 0
                if not matched:
                    matched = item.id in ai_semantic_match(wish.keywords, [item])
                if matched:
                    wish.matched_item_id = item.id
                    background_tasks.add_task(send_already_claimed_match_email, wish.user_email, wish.user_name, item)
        db.commit()

    db.close()

    # 退回 pending 等同於「物品重新可被認領」，對許願池來說跟新物品入庫一樣需要重新比對通知
    if req.status == "pending":
        for item_id in item_ids:
            if old_statuses.get(item_id) != "pending":
                background_tasks.add_task(check_wishlist_and_notify, item_id)

    return {"status": "success", "message": f"Updated {len(items)} items"}

@app.post("/api/analyze-image", dependencies=[Depends(verify_api_admin)])
async def analyze_image(image: UploadFile = File(...)):
    """接收圖片並呼叫 Gemini Vision API 產生特徵描述"""
    if not GEMINI_API_KEY:
        return {"status": "error", "message": "請先設定 GEMINI_API_KEY 環境變數才能使用 AI 辨識功能"}
    
    try:
        image_data = await image.read()
        img = Image.open(io.BytesIO(image_data))
        
        model = genai.GenerativeModel('gemini-flash-latest')
        prompt = """你是一個鐵道局的遺失物登記助理。請分析圖片並以 JSON 格式回傳以下欄位：
1. "category": 物品分類（如手機、皮夾、雨傘、水壺等）
2. "tags": 物品特徵標籤，以逗號分隔（如黑色,真皮,短夾）
3. "colors": 物品主要顏色，以逗號分隔
4. "ocr_text": 如果圖片中有身分證、信用卡、學生證等，請提取出姓名或卡號末四碼。如果沒有則回傳空字串。
5. "description": 綜合描述，包含特徵、材質、品牌或型號，30字以內。
請只回傳 JSON 格式字串，不要包含其他說明文字或 markdown 標記。"""
        response = model.generate_content([prompt, img])
        
        import json
        try:
            result_text = response.text.strip()
            if result_text.startswith("```json"):
                result_text = result_text[7:-3]
            elif result_text.startswith("```"):
                result_text = result_text[3:-3]
            result = json.loads(result_text)
            
            is_sensitive = False
            owner_name = ""
            card_last_four = ""
            ocr = result.get("ocr_text", "")
            if ocr:
                import re
                # 簡單的正則提取示意
                card_match = re.search(r'\d{4}', ocr)
                if card_match:
                    is_sensitive = True
                    card_last_four = card_match.group(0)
                if "身分證" in ocr or "姓名" in ocr:
                    is_sensitive = True
                    # 若為身分證，簡單標記
                    owner_name = ocr.replace("姓名", "").strip()[:5]

            fields_suggested = [f for f in ("category", "description", "tags", "colors") if result.get(f)]
            log_db = SessionLocal()
            log_db.add(AIAnalysisLog(fields_suggested=",".join(fields_suggested), ai_suggested_category=result.get("category")))
            log_db.commit()
            log_db.close()

            return {
                "status": "success",
                "description": result.get("description", ""),
                "category": result.get("category", ""),
                "tags": result.get("tags", ""),
                "colors": result.get("colors", ""),
                "ocr_text": ocr,
                "is_sensitive": is_sensitive,
                "owner_name": owner_name,
                "card_last_four": card_last_four
            }
        except json.JSONDecodeError:
            return {"status": "success", "description": response.text.strip()}
    except Exception as e:
        return {"status": "error", "message": gemini_error_message(e)}

@app.post("/api/scan-qr", dependencies=[Depends(verify_api_admin)])
async def scan_qr(req: ScanQRRequest):
    """站務端：掃描 QR Code 更新狀態與儲位"""
    db = SessionLocal()
    item = db.query(LostItem).filter(LostItem.qr_code_id == req.qr_code_id).first()
    if not item:
        db.close()
        raise HTTPException(status_code=404, detail="Item not found")

    if req.new_status and not is_valid_transition(item.status, req.new_status):
        db.close()
        raise HTTPException(status_code=400, detail=f"不合理的狀態轉換：{item.status} → {req.new_status}")

    if req.new_station:
        item.current_station = req.new_station
    if req.new_status:
        item.status = req.new_status

    db.add(ItemTransferLog(item_id=item.id, station=req.new_station, status=req.new_status, operator="admin"))
    db.commit()
    db.refresh(item)
    item_dict = serialize_item(item)
    db.close()
    return {"status": "success", "message": "QR Scan Update Successful", "item": item_dict}

@app.get("/api/admin/items/{item_id}/transfer-logs", dependencies=[Depends(verify_api_admin)])
def get_transfer_logs(item_id: int):
    """後台：查看物品完整移動軌跡"""
    db = SessionLocal()
    logs = db.query(ItemTransferLog).filter(ItemTransferLog.item_id == item_id).order_by(ItemTransferLog.created_at.asc()).all()
    db.close()
    return logs

class AICorrectionCreate(BaseModel):
    field_name: str
    ai_value: str
    corrected_value: str

@app.post("/api/items/{item_id}/ai-correction", dependencies=[Depends(verify_api_admin)])
def create_ai_correction(item_id: int, correction: AICorrectionCreate):
    """後台：記錄站務人員對 AI 辨識結果的人工修正，供未來分析 AI 準確率（依物品最終分類拆分統計）"""
    db = SessionLocal()
    item = db.query(LostItem).filter(LostItem.id == item_id).first()
    log = AICorrectionLog(
        item_id=item_id,
        field_name=correction.field_name,
        ai_value=correction.ai_value,
        corrected_value=correction.corrected_value,
        item_category=item.category if item else None
    )
    db.add(log)
    db.commit()
    db.close()
    return {"status": "success"}

AI_CORRECTION_WARNING_THRESHOLD = 0.5  # 修正率超過此比例時，登錄表單會主動提示該欄位常需人工確認
AI_CORRECTION_MIN_SAMPLES = 5  # 樣本數太少時不下判斷，避免誤導

@app.get("/api/admin/ai-corrections/summary", dependencies=[Depends(verify_api_admin)])
def get_ai_correction_summary():
    """後台：統計各欄位被人工修正的次數與修正率（修正次數 / AI 曾建議該欄位的次數）。
    全域統計（counts/warning_fields）會被不同分類的辨識難易度混在一起稀釋，
    因此額外提供 by_category：依「分類 x 欄位」拆分的修正率，讓警示能精準命中真正常出錯的組合。
    分類維度採用物品最終的台鐵分類（AICorrectionLog.item_category），AIAnalysisLog 沒有對應分類時歸入「未分類」。"""
    db = SessionLocal()
    correction_logs = db.query(AICorrectionLog).all()
    analysis_logs = db.query(AIAnalysisLog).all()
    db.close()

    correction_counts = {}
    category_field_corrections = {}
    for log in correction_logs:
        correction_counts[log.field_name] = correction_counts.get(log.field_name, 0) + 1
        cat = log.item_category or "未分類"
        key = (cat, log.field_name)
        category_field_corrections[key] = category_field_corrections.get(key, 0) + 1

    suggested_counts = {}
    category_field_suggested = {}
    for log in analysis_logs:
        cat = log.ai_suggested_category or "未分類"
        for field in (log.fields_suggested or "").split(","):
            if field:
                suggested_counts[field] = suggested_counts.get(field, 0) + 1
                key = (cat, field)
                category_field_suggested[key] = category_field_suggested.get(key, 0) + 1

    summary = {}
    warning_fields = []
    for field, corrected in correction_counts.items():
        suggested = suggested_counts.get(field, 0)
        rate = (corrected / suggested) if suggested > 0 else None
        summary[field] = corrected
        if suggested >= AI_CORRECTION_MIN_SAMPLES and rate is not None and rate >= AI_CORRECTION_WARNING_THRESHOLD:
            warning_fields.append(field)

    by_category = []
    for (cat, field), corrected in category_field_corrections.items():
        suggested = category_field_suggested.get((cat, field), 0)
        rate = (corrected / suggested) if suggested > 0 else None
        by_category.append({
            "category": cat,
            "field_name": field,
            "corrected": corrected,
            "suggested": suggested,
            "rate": round(rate, 2) if rate is not None else None,
            "is_warning": bool(suggested >= AI_CORRECTION_MIN_SAMPLES and rate is not None and rate >= AI_CORRECTION_WARNING_THRESHOLD)
        })
    by_category.sort(key=lambda x: (x["rate"] is None, -(x["rate"] or 0)))

    return {"counts": summary, "warning_fields": warning_fields, "by_category": by_category}

class DeliveryRuleCreate(BaseModel):
    category_keyword: str
    allowed: bool
    reason: str = None

class DeliveryRuleUpdate(BaseModel):
    allowed: bool
    reason: str = None

@app.get("/api/admin/delivery-rules", dependencies=[Depends(verify_api_admin)])
def get_delivery_rules():
    db = SessionLocal()
    rules = db.query(DeliveryRule).all()
    db.close()
    return rules

@app.post("/api/admin/delivery-rules", dependencies=[Depends(verify_api_admin)])
def create_delivery_rule(rule: DeliveryRuleCreate):
    db = SessionLocal()
    new_rule = DeliveryRule(category_keyword=rule.category_keyword, allowed=rule.allowed, reason=rule.reason)
    db.add(new_rule)
    db.commit()
    db.close()
    return {"status": "success"}

@app.put("/api/admin/delivery-rules/{rule_id}", dependencies=[Depends(verify_api_admin)])
def update_delivery_rule(rule_id: int, update: DeliveryRuleUpdate):
    db = SessionLocal()
    rule = db.query(DeliveryRule).filter(DeliveryRule.id == rule_id).first()
    if not rule:
        db.close()
        raise HTTPException(status_code=404, detail="Rule not found")
    rule.allowed = update.allowed
    rule.reason = update.reason
    db.commit()
    db.close()
    return {"status": "success"}

@app.delete("/api/admin/delivery-rules/{rule_id}", dependencies=[Depends(verify_api_admin)])
def delete_delivery_rule(rule_id: int):
    db = SessionLocal()
    rule = db.query(DeliveryRule).filter(DeliveryRule.id == rule_id).first()
    if not rule:
        db.close()
        raise HTTPException(status_code=404, detail="Rule not found")
    db.delete(rule)
    db.commit()
    db.close()
    return {"status": "success"}