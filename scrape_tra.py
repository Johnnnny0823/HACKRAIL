import urllib.request
from bs4 import BeautifulSoup
import sqlite3
import datetime

url = "https://www.railway.gov.tw/tra-tip-web/tip/tip00E/tipE11/query?page=319"

print("Fetching URL...")
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
try:
    with urllib.request.urlopen(req) as response:
        html = response.read().decode('utf-8')
except Exception as e:
    print("Error fetching:", e)
    exit(1)

soup = BeautifulSoup(html, 'html.parser')

items = []
# TRA lost item tables usually have class like 'table-th-left' or similar, let's find all trs
trs = soup.find_all('tr')
for tr in trs:
    tds = tr.find_all('td')
    if len(tds) >= 5:
        # Example TRA columns:
        # 1: 日期 (e.g., 2026/05/23)
        # 2: 地點 (e.g., 臺北站)
        # 3: 類別 (e.g., 3C產品-手機)
        # 4: 物品特徵 (e.g., 黑色 iPhone)
        # 5: 狀態 (e.g., 處理中)
        
        date_str = tds[0].get_text(strip=True)
        location = tds[1].get_text(strip=True)
        category_raw = tds[2].get_text(strip=True)
        desc = tds[3].get_text(strip=True)
        
        # map category
        cat = "其他"
        if "3C" in category_raw or "電子" in category_raw or "手機" in category_raw:
            cat = "電子產品"
        elif "證件" in category_raw or "錢包" in category_raw or "票" in category_raw or "皮夾" in category_raw or "現金" in category_raw or "金融" in category_raw:
            cat = "票卡錢包"
        elif "衣" in category_raw or "帽" in category_raw or "傘" in category_raw or "配飾" in category_raw or "袋" in category_raw or "包" in category_raw:
            cat = "衣物配飾"
            
        # Parse date if possible, otherwise use current time
        found_time = datetime.datetime.now().isoformat()
        
        if desc and location and category_raw:
            items.append((cat, location, desc, found_time))

print(f"Parsed {len(items)} items from TRA.")

if items:
    conn = sqlite3.connect('/Users/joshmac/Desktop/teadu/templates/lost_found.db')
    cursor = conn.cursor()
    
    count = 0
    for cat, loc, desc, ftime in items:
        cursor.execute('''
            INSERT INTO lost_items (description, category, found_location, image_filename, found_time, status)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (desc, cat, loc, "", ftime, "pending"))
        count += 1
    
    conn.commit()
    conn.close()
    print(f"Successfully inserted {count} items into the database.")
else:
    print("No items parsed. Let's dump some raw text to see the structure:")
    for tr in trs[:10]:
        print([td.get_text(strip=True) for td in tr.find_all('td')])

