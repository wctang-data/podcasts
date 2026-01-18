# /// script
# dependencies = [
#   "google-api-python-client",
#   "google-auth-oauthlib",
#   "mutagen",
# ]
# ///

import io
import os
import csv
import datetime
from email.utils import formatdate
# 新增 parse 用於讀取舊檔
from xml.etree.ElementTree import Element, SubElement, tostring, parse
from xml.dom import minidom

# Google API 相關庫
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# 音訊處理庫
from mutagen.mp3 import MP3

# --- 全域設定區 (連結與圖片維持固定，或依需求修改) ---
PODCAST_LINK = "https://example.com"  # 您的網站連結
PODCAST_IMAGE = "https://example.com/cover.jpg" # 封面圖片連結 (建議 1400x1400)

# 權限範圍
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

def get_credentials():
    """處理 OAuth 2.0 授權"""
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return creds

def get_folder_metadata(service, folder_id):
    """取得資料夾名稱作為 Podcast 標題"""
    try:
        file = service.files().get(fileId=folder_id, fields='name').execute()
        folder_name = file.get('name', 'My Google Drive Podcast')
        return folder_name
    except Exception as e:
        print(f"無法取得資料夾資訊: {e}")
        return "我的 Google Drive Podcast"

def parse_existing_xml(xml_file):
    """
    讀取現有的 XML 檔案，回傳 {guid: duration} 的字典。
    目的：避免重複下載分析已存在的音訊檔案。
    """
    cache = {}
    if not os.path.exists(xml_file):
        return cache

    print(f"發現現有 XML: {xml_file}，正在讀取快取資料...")
    try:
        tree = parse(xml_file)
        root = tree.getroot()

        # 定義 XML Namespace 以便正確搜尋標籤
        namespaces = {
            'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd'
        }

        # 遍歷所有 item
        for item in root.findall(".//item"):
            try:
                guid = item.find("guid").text
                # 嘗試讀取 itunes:duration
                dur_tag = item.find("itunes:duration", namespaces)

                if guid and dur_tag is not None:
                    cache[guid] = dur_tag.text
            except Exception:
                continue

        print(f"成功載入 {len(cache)} 筆歷史紀錄。")
    except Exception as e:
        print(f"解析舊 XML 失敗 (將重新全量掃描): {e}")

    return cache

def get_audio_duration(service, file_id):
    """
    下載檔案前 128KB 來解析 MP3 Header 獲取時長。
    回傳格式: MM:SS (若超過一小時則是 HH:MM:SS)
    """
    try:
        request = service.files().get_media(fileId=file_id)
        file_io = io.BytesIO()
        downloader = MediaIoBaseDownload(file_io, request)

        # 僅下載開頭部分
        downloader.next_chunk()

        file_io.seek(0)
        audio = MP3(file_io)
        length_sec = int(audio.info.length)

        hours, remainder = divmod(length_sec, 3600)
        minutes, seconds = divmod(remainder, 60)

        if hours > 0:
            return f"{hours:02}:{minutes:02}:{seconds:02}"
        else:
            return f"{minutes:02}:{seconds:02}"
    except Exception as e:
        print(f"  [警告] 無法讀取時長 (ID: {file_id}): {e}")
        return "00:00"

def parse_podcast_date(filename, created_time_str):
    """
    解析發布日期 (pubDate)：
    1. 優先規則：檢查檔名是否為 'YYYYMMDD_' 開頭 (例如 20250128_節目.mp3)
    2. 次要規則：使用檔案的建立時間 (createdTime)
    回傳：RFC 822 格式的時間字串
    """
    # 嘗試從檔名解析: 20250128_
    if len(filename) >= 9 and filename[8] == '_':
        try:
            date_part = filename[:8]  # 取前8碼
            # 解析 YYYYMMDD
            dt = datetime.datetime.strptime(date_part, "%Y%m%d")
            # 設定預設時間為中午 12:00 UTC，避免時區問題導致日期跳動
            dt = dt.replace(hour=12, minute=0, second=0, tzinfo=datetime.timezone.utc)
            return formatdate(dt.timestamp(), usegmt=True)
        except ValueError:
            pass  # 解析失敗，並非日期格式，繼續往下執行

    # 使用 Google Drive 檔案建立時間 (Format: 2023-10-27T10:00:00.000Z)
    try:
        # 處理 ISO 8601 字串
        if '.' in created_time_str:
            created_time_str = created_time_str.split('.')[0] + 'Z'

        # 將 'Z' 替換為 '+0000' 以符合 Python strptime 的 %z 格式
        dt = datetime.datetime.strptime(created_time_str.replace('Z', '+0000'), "%Y-%m-%dT%H:%M:%S%z")
        return formatdate(dt.timestamp(), usegmt=True)
    except Exception as e:
        print(f"日期解析錯誤 ({filename}): {e}，使用當前時間。")
        return formatdate(usegmt=True)

def generate_xml(file_data_list, podcast_title, podcast_desc, output_filename):
    """生成符合 iTunes 標準的 RSS XML"""

    # 定義 Namespace
    rss = Element('rss', {
        'version': '2.0',
        'xmlns:itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd',
        'xmlns:content': 'http://purl.org/rss/1.0/modules/content/'
    })

    channel = SubElement(rss, 'channel')
    SubElement(channel, 'title').text = podcast_title
    SubElement(channel, 'description').text = podcast_desc
    SubElement(channel, 'link').text = PODCAST_LINK
    SubElement(channel, 'language').text = "zh-tw"

    # 頻道封面
    SubElement(channel, 'itunes:image', {'href': PODCAST_IMAGE})

    print(f"正在生成 XML，共 {len(file_data_list)} 個項目...")

    for file in file_data_list:
        item = SubElement(channel, 'item')

        # 標題 (去除副檔名)
        SubElement(item, 'title').text = file['title_clean']

        # GUID
        SubElement(item, 'guid', {'isPermaLink': 'false'}).text = file['id']

        # 描述 (使用完整檔名)
        SubElement(item, 'description').text = file['name']

        # 音訊連結
        dl_link = f"https://drive.google.com/uc?export=download&id={file['id']}"
        SubElement(item, 'enclosure', {
            'url': dl_link,
            'length': str(file['size']),
            'type': 'audio/mpeg'
        })

        # iTunes 時長
        SubElement(item, 'itunes:duration').text = file['duration']

        # 發布時間 (解析後的日期)
        SubElement(item, 'pubDate').text = file['pubDate']

    # 排版美化並寫入檔案
    xml_str = minidom.parseString(tostring(rss)).toprettyxml(indent="  ", newl="\r")

    with open(output_filename, "w", encoding="utf-8") as f:
        f.write(xml_str)

    print(f"成功！XML 已儲存為: {output_filename}")

def process_folder(service, folder_id, xml_filename):
    """處理單一資料夾的邏輯"""
    print(f"\n--- 開始處理資料夾 ID: {folder_id} -> {xml_filename} ---")

    folder_name = get_folder_metadata(service, folder_id)
    print(f"Podcast 名稱: {folder_name}")

    # 1. 先讀取舊的 XML 建立快取 (針對目前的 xml_filename)
    duration_cache = parse_existing_xml(xml_filename)

    print("正在掃描音訊檔案...")
    # 加入 createdTime 欄位以供日期排序與解析使用
    query = f"'{folder_id}' in parents and mimeType = 'audio/mpeg' and trashed = false"
    results = service.files().list(
        q=query,
        fields="files(id, name, size, createdTime)",
        pageSize=100
    ).execute()

    files = results.get('files', [])

    if not files:
        print(f"在資料夾 {folder_name} 中沒有找到 MP3 檔案。")
        return

    processed_files = []
    for f in files:
        f_id = f['id']
        f_name = f['name']
        print(f"處理中: {f_name}...")

        # 2. 檢查是否有快取
        if f_id in duration_cache:
            print(f"  -> 命中快取，使用紀錄中的時長。")
            duration = duration_cache[f_id]
        else:
            print(f"  -> 新檔案，下載解析時長...")
            duration = get_audio_duration(service, f_id)

        # 3. 重新計算 Meta
        title_clean = os.path.splitext(f_name)[0]
        pub_date = parse_podcast_date(f_name, f.get('createdTime', ''))

        processed_files.append({
            'id': f_id,
            'name': f_name,
            'title_clean': title_clean,
            'size': f.get('size', 0),
            'duration': duration,
            'pubDate': pub_date
        })

    # 傳入目前的 xml_filename
    generate_xml(processed_files, folder_name, folder_name, xml_filename)

def main():
    creds = get_credentials()
    service = build('drive', 'v3', credentials=creds)

    csv_file = 'drive.csv'
    if not os.path.exists(csv_file):
        print(f"錯誤: 找不到設定檔 '{csv_file}'。請建立該檔案，格式為每行: FOLDER_ID, XML_FILENAME")
        return

    # 讀取 CSV 並迴圈處理
    with open(csv_file, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        count = 0
        for row in reader:
            if len(row) < 2:
                continue

            # 去除空白
            folder_id = row[0].strip()
            xml_filename = row[1].strip()

            if not folder_id or not xml_filename:
                continue

            process_folder(service, folder_id, xml_filename)
            count += 1

    if count == 0:
        print("未執行任何任務，請檢查 drive.csv 內容是否正確。")

if __name__ == '__main__':
    main()
