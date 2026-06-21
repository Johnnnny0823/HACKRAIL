import os
import urllib.request
import json

URL = "https://raw.githubusercontent.com/repeat/taiwan-railway-stations/master/station.json"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Ensure static dir exists
os.makedirs(STATIC_DIR, exist_ok=True)

try:
    print("Fetching data from GitHub...")
    req = urllib.request.Request(URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode('utf-8'))
        
    stationsDB = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        name = props.get("站名", "")
        if name.endswith("站"):
            name = name[:-1] + "車站" # 統一成 "XX車站"
        
        lat = props.get("緯度")
        lon = props.get("經度")
        
        if name and lat and lon:
            stationsDB.append({
                "name": name,
                "lat": float(lat),
                "lon": float(lon)
            })
            
    # Output to static/stations.js
    js_content = f"const stationsDB = {json.dumps(stationsDB, ensure_ascii=False, indent=4)};\n"
    
    out_path = os.path.join(STATIC_DIR, "stations.js")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(js_content)
        
    print(f"Successfully wrote {len(stationsDB)} stations to {out_path}")
    
except Exception as e:
    print(f"Error: {e}")
