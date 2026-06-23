# 鐵道尋蹤 — 鐵路遺失物管理系統

一套以 FastAPI 打造的鐵路遺失物登錄、協尋與配送管理系統，整合 Gemini AI 影像辨識／語意比對與 QR Code 流向追蹤，提供旅客查詢協尋與站務人員後台管理兩種介面。

## 系統架構

- **後端框架**：FastAPI（[main.py](main.py)）
- **資料庫**：SQLite，透過 SQLAlchemy ORM 存取（`lost_found.db`）。新增欄位採用啟動時自動偵測並執行 `ALTER TABLE` 補欄位（`_ensure_columns_exist()`），因為 SQLAlchemy 的 `create_all()` 只會建立全新的表，不會替既有表補欄位
- **前端**：Jinja2 模板渲染的靜態 HTML（[index.html](index.html)、[login.html](login.html)、[admin.html](admin.html)）
- **樣式**：Tailwind CSS 採**預編譯**模式（非 CDN 即時編譯），原始輸入在 `static/css/tailwind-input.css`，設定檔為 [tailwind.config.js](tailwind.config.js)，編譯輸出至 `static/css/tailwind.css`，HTML 異動到任何 Tailwind class 後需重新執行 `npm run build:css`
- **AI 服務**：Google Gemini（`gemini-flash-latest`）用於：
  - 後台 AI 輔助登錄（圖片 → 分類/標籤/顏色/描述/OCR），並記錄每次辨識建議了哪些欄位（`AIAnalysisLog`），用於計算修正率
  - 旅客端以圖找圖（圖片 → 特徵關鍵字 → 比對資料庫）
  - 許願池語意比對（描述用詞不同但同義，如「水壺」≈「保溫杯」，一次請求比對所有候選物品，不逐筆呼叫以節省額度；比對前會先排除已關閉或已逾期的許願池登記，避免隨資料量增長每次都全表掃描）
- **通知服務**：Gmail SMTP 寄送協尋配對通知、配送到達通知、「物品已被領回請確認」通知（未設定 Gmail 帳密時靜默跳過，不影響其他功能）
- **檔案儲存**：上傳照片與 QR Code 圖檔存放於 `uploads/`，靜態資源於 `static/`

## 資料模型

| 資料表 | 用途 |
|---|---|
| `LostItem` | 遺失物主資料：分類（台鐵官方 6 類）、物品名稱、描述、拾獲地點/時間、車次/車廂座位、狀態、QR Code、目前所在車站、是否為敏感物品（證件/卡片）、台鐵公告格式欄位（拾獲方式、車站代碼、保管單位、服務電話、物品代號）等 |
| `Wishlist` | 旅客「許願池」協尋登記：姓名/Email/電話 + 特徵關鍵字，**不依賴分類比對**（站務員與旅客對分類認知常不同）。新物品入庫時自動以關鍵字+AI語意比對並寄信通知；物品被標記領回時也會反向掃描比對，標記 `matched_item_id` 供後台「可能已找到」提示，並主動寄信請旅客確認是否為本人領取。登記後 90 天（`WISHLIST_EXPIRY_DAYS`）自動逾期，逾期後不會被刪除，僅跳過比對 |
| `DeliveryRequest` | 跨站配送申請：旅客指定目標車站領取物品，並追蹤配送狀態（pending → delivering → arrived），同一物品不可同時有兩筆進行中的申請。到站後需站務員核對「驗證描述」（`verify-pickup`）才會記錄 `verified_by`/`verified_at` 並將物品標記為已領回，不是人到了就直接結案 |
| `ItemTransferLog` | 物品移動／狀態變更歷史紀錄（QR 掃描、配送狀態更新、核對領取時寫入） |
| `SensitiveAccessLog` | 敏感物品（含個資）查看紀錄，記錄誰在何時查看過完整姓名/卡號 |
| `AICorrectionLog` | AI 辨識結果被人工修正的紀錄，用於統計各欄位被修正次數；同時記錄物品最終的台鐵分類（`item_category`），讓修正率可拆到「分類 × 欄位」維度 |
| `AIAnalysisLog` | 每次成功呼叫 AI 輔助登錄時，記錄 AI 建議了哪些欄位，作為計算「修正率」的分母（修正次數 ÷ AI 建議次數），避免單看修正次數而誤判；同時記錄 AI 當時建議的分類（`ai_suggested_category`），供分類維度統計使用 |
| `DeliveryRule` | 可配置的配送限制規則（例如 3C 電子產品不受理跨站配送），取代寫死在程式碼裡的判斷 |
| `ImageSearchFeedback` | 旅客回報「這不是我的物品」的負向回饋紀錄 |

### 物品狀態機

```
pending → claimed（站務員直接標記，例如旅客未經配送直接到站領取）
pending → delivering → arrived → claimed（站務員核對驗證描述後執行 verify-pickup 才會到這步）
arrived / claimed → pending（退回）
```

狀態轉換由 `is_valid_transition()` 集中驗證，**只有站務員確認旅客已實際到站領取後手動標記，物品才會變成 `claimed`**；配送流程中的 `delivering`／`arrived` 都不等於「已領取」，避免後台/首頁過早顯示「已領回」造成誤解。物品送達車站（`arrived`）後，站務員必須先核對旅客填寫的驗證描述（`PUT /api/admin/deliveries/{id}/verify-pickup`），才能將狀態推進到 `claimed`；在核對完成前，後台會顯示「📦 已送達，待核對領取」而非直接視為完成，避免在沒有實際核對身分/描述的情況下就結案。

任何狀態退回 `pending`（核對失敗、旅客未出現、誤標記等）都會視為「物品重新可被認領」，自動重新觸發許願池比對並通知等待中的旅客，邏輯與新物品入庫時一致，避免退回後物品其實還在等人領取，卻沒有人收到通知。

## 主要功能

### 旅客端（[index.html](index.html)）
- 瀏覽遺失物公開清冊，依台鐵官方 6 類分類（現金/3C/證件/一般物品/貴重品/其他特殊物品）篩選。**已領回物品預設不顯示**，避免清冊無限累積已結案紀錄、降低不必要的個資曝光時間；清冊上方有「顯示已領回物品」開關，開啟後才會看到已領回物品並附上「✅ 已經由失主領回」遮罩，計數文字也會提示目前隱藏了多少筆
- **引導式搜尋**：點「我要找東西」進入問答式流程（情境 → 時間範圍 → 關鍵字或上傳照片），也保留進階搜尋模式供已知精確車次/日期的旅客使用
- **以圖找圖**：上傳照片，由 Gemini 萃取特徵關鍵字後比對資料庫描述/標籤（不比對分類），結果依信心分數分組顯示「高度符合」/「可能相關」，並標示百分比；若比對到已被領回的物品仍會顯示在結果中（讓旅客能確認「啊這是我的，已經領走了」），但會加上「已經由失主領回」遮罩且隱藏「這不是我的物品」按鈕，避免誤以為是可認領的物品
- **許願池協尋**：登記姓名/Email/電話 + 特徵關鍵字，送出當下若資料庫已有可能相符的物品會立即彈出確認視窗讓旅客直接確認；若當下沒有，之後系統每次有新物品入庫都會自動重新比對並寄信通知，不需旅客自行回來檢查。若比對到的物品其實已被領回（例如旅客本人忘了登記就直接領走，或被別人誤領），系統會主動寄信請旅客確認，而非由站務員單方面判斷後直接結案
- **跨站配送認領**：物品詳情頁點「我要認領」會先關閉詳情視窗再彈出配送申請表單（不會疊加），填寫聯絡方式、驗證描述、目標車站送出；填寫驗證描述時若內容過短或為常見泛用詞（如「黑色包包」），會即時提示補充更具體特徵（顏色/材質/內容物/損傷處等），協助站務員後續核對；3C 電子產品依規定不受理跨站配送（規則可由後台配置）；同一物品不可重複建立進行中的配送申請
- 所有彈出視窗皆支援按 **Esc 鍵關閉**

### 站務後台（[admin.html](admin.html)，需登入）
- **新增遺失物**：手動填寫或上傳照片由 AI 輔助登錄，欄位含物品名稱、分類、描述、拾獲地點/方式、保管單位、服務電話、車站代碼（對應台鐵官方公告格式），自動生成物品代號
- **AI 輔助登錄**：Gemini Vision 自動產生分類建議、標籤、顏色、描述，並嘗試 OCR 證件/卡號末四碼（自動標記為敏感物品並遮蔽顯示，需點擊「查看完整資訊」才會顯示明碼，同時留下查看紀錄）
- **AI 修正回饋**：站務員修改 AI 建議的欄位時自動記錄差異，後台總覽可看到「AI 辨識校正統計」；若某欄位的修正率（修正次數 ÷ AI 建議次數）超過 50%（且樣本數 ≥5），新增物品表單會主動顯示警示，提醒該欄位常需人工確認，把累積資料變成即時的登錄輔助而非單純事後報表。修正率同時拆分到「分類 × 欄位」維度（例如「一般物品」分類的「顏色」欄位修正率），避免不同分類的辨識難易度混在一起被全域統計稀釋；表單已選擇分類時，警示文字會優先顯示該分類專屬的提醒
- **物品狀態管理**：單筆/批次更新狀態、批次刪除（刪除時會連帶清除該物品的配送申請/轉移紀錄/存取紀錄，避免日後 id 重用造成誤判）；狀態徽章依 PENDING/DELIVERING/ARRIVED/CLAIMED 各自正確顯示
- **物流配送管理**：審核配送申請、更新狀態（pending → delivering → arrived），狀態推進到「已送達」時會自動寄信通知旅客（主旨【鐵道遺失物寄送】您的遺失物已送達ＯＯ車站！，內文含物品名稱、特徵、照片連結、物品代號、服務電話，並依台鐵規範提醒攜帶身分證件於營業時間內到場領取，逾期未領取依規定處理）；物品送達車站後須由站務員核對旅客填寫的「驗證描述」並執行「核對驗證描述並結案」，才會將物品標記為已領回並記錄核對人員與時間，確保不是人到了就直接放行；配送列表將「物品原始描述」與「旅客驗證描述」並排顯示，不需另外點開即可直接比對核實；可取消重複/過期的申請；**配送限制規則**可在後台新增/編輯/刪除（不再寫死於程式碼）
- **智慧許願池管理**：列表顯示所有許願池登記，若比對到的物品已被領回會顯示「🔔 可能已找到」提示（同時系統已主動寄信請旅客確認），站務員確認後可按「結案」關閉該筆許願；逾期（超過 90 天）未結案的登記會標示「⏰ 已逾期，停止比對」
- **QR Code 流向追蹤**：每件物品生成專屬 QR Code，掃描後可更新物品所在車站與狀態，並寫入完整移動歷史，可在物品詳情查看時間軸
- 所有彈出視窗（新增物品、QR 掃描、敏感資訊、照片放大等）皆支援按 **Esc 鍵關閉**，轉場動畫統一為 200ms
- 「總覽儀表板」「智慧許願池管理」「物流配送管理」三個主要分頁皆採用相同的版面結構（`flex-1 overflow-y-auto`），內容超出視窗高度時整個面板（含標題、設定區塊、表格）一起滾動，而非只滾動表格內部

### 登入與權限（[login.html](login.html)）
- 簡易帳密登入（預設 `admin` / `admin123`），登入後以 Cookie（`admin_token`）驗證後台 API 權限
- 後台頁面與管理類 API 皆需通過 `verify_api_admin` 驗證

## API 路由總覽

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/` | 旅客首頁 |
| GET/POST | `/login` | 登入頁面與登入處理 |
| GET | `/logout` | 登出 |
| GET | `/admin` | 後台管理頁（需登入） |
| GET | `/api/items` | 取得所有遺失物 |
| POST | `/api/items` | 新增遺失物（含圖片、QR Code 生成、物品代號生成） |
| DELETE | `/api/items/{id}` | 刪除單筆遺失物（連帶清除關聯紀錄） |
| PUT | `/api/items/{id}/status` | 更新單筆狀態（標記 claimed 時自動比對許願池） |
| POST | `/api/items/batch-delete` / `batch-status` | 批次刪除/更新狀態 |
| GET | `/api/search` | 多維度搜尋（關鍵字、車次、日期區間） |
| POST | `/api/search-by-image` | 以圖找圖，回傳信心分數與分組 |
| POST | `/api/search-feedback` | 回報「這不是我的物品」 |
| POST | `/api/analyze-image` | AI 圖片特徵辨識（後台用） |
| POST | `/api/items/{id}/ai-correction` | 記錄 AI 辨識被人工修正的差異（後台用） |
| GET | `/api/admin/ai-corrections/summary` | AI 修正統計，含全域與「分類 × 欄位」拆分（後台用） |
| GET | `/api/admin/items/{id}/sensitive` | 查看敏感物品完整資訊（留下存取紀錄，後台用） |
| GET | `/api/admin/items/{id}/access-logs` | 敏感物品查看紀錄（後台用） |
| GET | `/api/admin/items/{id}/transfer-logs` | 物品移動歷史（後台用） |
| POST | `/api/wishlist` | 登記許願池協尋，回傳目前可能相符的物品清單 |
| GET | `/api/admin/wishlists` | 取得所有許願池登記，附帶可能相符物品摘要（後台用） |
| PUT | `/api/admin/wishlists/{id}` | 開啟/關閉（結案）許願池登記（後台用） |
| DELETE | `/api/admin/wishlists/{id}` | 刪除許願池登記（後台用） |
| POST | `/api/delivery` | 申請跨站配送（同物品不可重複申請） |
| GET | `/api/admin/deliveries` | 取得所有配送申請（後台用） |
| PUT | `/api/admin/deliveries/{id}/status` | 更新配送狀態 pending → delivering → arrived（後台用） |
| PUT | `/api/admin/deliveries/{id}/verify-pickup` | 核對驗證描述，將物品標記為已領回 claimed（後台用） |
| DELETE | `/api/admin/deliveries/{id}` | 取消配送申請（後台用） |
| GET/POST/PUT/DELETE | `/api/admin/delivery-rules` | 配送限制規則 CRUD（後台用） |
| POST | `/api/scan-qr` | 掃描 QR Code 更新車站/狀態（後台用） |

## 環境變數設定

需於專案根目錄建立 `.env` 檔案：

```
GEMINI_API_KEY=你的 Gemini API 金鑰
GMAIL_ACCOUNT=寄信用 Gmail 帳號
GMAIL_APP_PASSWORD=Gmail 應用程式密碼
SITE_URL=http://127.0.0.1:8000
```

未設定 `GEMINI_API_KEY` 時，AI 辨識、以圖找圖、許願池語意比對相關功能會回傳錯誤或自動降級為純關鍵字比對；未設定 Gmail 帳密時，email 通知會靜默跳過。`SITE_URL` 用於組成配送到站通知信中的物品照片絕對網址連結，部署到正式網域後務必設定為實際對外網址，否則信件中的照片連結會指向 `127.0.0.1`，收件人無法開啟。

## 啟動方式

```bash
# 1. 安裝 Python 依賴
pip install fastapi uvicorn sqlalchemy jinja2 python-multipart google-generativeai pillow qrcode python-dotenv

# 2.（首次或更動過 HTML 內的 Tailwind class 時）編譯前端樣式
npm install
npm run build:css

# 3. 啟動伺服器
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

啟動後於瀏覽器開啟 `http://127.0.0.1:8000/`（旅客端）與 `http://127.0.0.1:8000/admin`（站務後台，預設帳密 `admin` / `admin123`）。

## 關於資料庫與測試資料

`lost_found.db`（SQLite 資料庫）與 `uploads/`（物品照片、QR Code 圖檔）皆已加入版本控制，**clone 此 repo 後即附帶一份示範資料（約 20 筆遺失物紀錄與對應照片）**，不需要額外匯入步驟即可直接啟動並瀏覽。若要從全新空白資料庫開始，刪除 `lost_found.db` 後重啟伺服器即可（`_ensure_columns_exist()` 與 `Base.metadata.create_all()` 會自動建立空表結構）；`uploads/` 內的舊照片與 QR Code 檔案不會自動清除，需手動刪除。
