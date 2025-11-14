import io
import json
import os
import re
import time
import requests
import base64
import random
import tempfile
import google.auth
from google.cloud import vision
from google import genai
from google.genai.types import HttpOptions, GenerateContentConfig
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from pdfminer.high_level import extract_text

# 使用 Application Default Credentials (ADC)
# 在 Cloud Run 上會自動使用服務帳戶的憑證，不需要 API KEY
# 在本地開發時，可以通過環境變數 GOOGLE_APPLICATION_CREDENTIALS 或 gcloud auth application-default login 設置
credentials, project = google.auth.default()

# 設置專案和位置（從環境變數讀取，如果沒有則使用預設值）
os.environ["GOOGLE_CLOUD_PROJECT"] = os.getenv("GOOGLE_CLOUD_PROJECT", project or "sinuous-origin-454613-h1")
os.environ["GOOGLE_CLOUD_LOCATION"] = os.getenv("GOOGLE_CLOUD_LOCATION", "global")
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "True")


def get_genai_client():
    """獲取配置好的 genai.Client，使用 Application Default Credentials"""
    # 當 GOOGLE_GENAI_USE_VERTEXAI=True 時，genai.Client 會自動使用 ADC
    # 如果需要明確傳遞憑證，可以通過 HttpOptions 傳遞
    return genai.Client(http_options=HttpOptions(api_version="v1"))

COMMON_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

REQUEST_TIMEOUT = 10
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2
RATE_LIMIT_DELAY = 1


def clean_markdown_json(raw_text):
    """清理 Markdown 格式的 JSON"""
    match = re.search(r'```(?:json)?\s*({.*?})\s*```', raw_text, re.DOTALL)
    if match:
        return match.group(1)
    return raw_text.strip()


def fetch_html_with_retry(url):
    """使用防爬蟲機制從 URL 抓取 HTML"""
    try:
        from urllib.parse import urlparse
        result = urlparse(url)
        if not all([result.scheme, result.netloc]):
            return None
    except:
        return None
    
    for attempt in range(RETRY_ATTEMPTS):
        try:
            user_agent = random.choice(COMMON_USER_AGENTS)
            headers = {
                'User-Agent': user_agent,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            }
            
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            
            if 'charset' not in response.headers.get('content-type', ''):
                response.encoding = 'utf-8'
            
            return response.text
            
        except requests.exceptions.Timeout:
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(RETRY_DELAY)
            else:
                return None
        except requests.exceptions.ConnectionError:
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(RETRY_DELAY)
            else:
                return None
        except requests.exceptions.HTTPError as e:
            if hasattr(e, 'response') and e.response.status_code in [429, 503]:
                if attempt < RETRY_ATTEMPTS - 1:
                    time.sleep(RETRY_DELAY * (attempt + 1))
                else:
                    return None
            else:
                return None
        except Exception:
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(RETRY_DELAY)
            else:
                return None
    
    return None


def extract_text_from_html(html_content):
    """從 HTML 中提取文字內容"""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        for script in soup(["script", "style"]):
            script.decompose()
        
        text = soup.get_text()
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        clean_text = '\n'.join(lines)
        
        return clean_text
    except Exception:
        return None


def read_student_id(image_path):
    """使用 Google Vision API 進行 OCR"""
    try:
        client = vision.ImageAnnotatorClient()
        with io.open(image_path, "rb") as image_file:
            content = image_file.read()
        image = vision.Image(content=content)
        response = client.text_detection(image=image)
        texts = response.text_annotations
        if not texts:
            return None
        return texts[0].description.strip()
    except Exception as e:
        return None


def parse_ocr_with_google_ai(ocr_text):
    """使用 Gemini 解析 OCR 文字"""
    client = get_genai_client()
    MODEL_NAME = "gemini-2.5-flash"
    
    prompt = (
        f"以下是學生證 OCR 結果，請解析成 JSON 格式，欄位為 school, department, name。\n"
        f"要求：JSON 格式必須正確；保持原文字形，不修改文字；若無法識別或不是學生證，回傳 {{'result': 'NOT ID'}}。\n"
        f"請直接輸出純 JSON，不要包在 ```json 區塊中。\n\n"
        f"OCR text:\n{ocr_text}"
    )

    config = GenerateContentConfig(temperature=0.0, max_output_tokens=512)
    
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=config
        )
        raw_content = response.text.strip()
        clean_json_str = clean_markdown_json(raw_content)
        
        parsed = json.loads(clean_json_str)
        return parsed
        
    except json.JSONDecodeError as e:
        return {
            "error": "JSON parse failed",
            "raw": raw_content,
            "cleaned": clean_json_str,
            "parse_error": str(e)
        }
    except Exception as e:
        return {"error": f"API call failed: {str(e)}"}


def parse_namelist_from_url(url, school_dep):
    """從 URL 抓取 HTML 並解析名單"""
    
    html_content = fetch_html_with_retry(url)
    if not html_content:
        return {"error": f"無法從 URL 抓取內容：{url}"}
    
    text_content = extract_text_from_html(html_content)
    if not text_content:
        return {"error": "HTML 內容為空或無法解析"}
    
    client = get_genai_client()
    MODEL_NAME = "gemini-2.5-flash"
    
    prompt = (
        f"請先檢查這份網頁內容有沒有包含「{school_dep}」的名單。\n"
        f"如果內容未包含「{school_dep}」的名單，請**直接回傳純文字訊息：「網頁未包含{school_dep}名單」**，不要回傳 JSON 或任何其他內容。\n"
        "如果包含，請繼續並**只提取該系所的名單**，忽略網頁中其他系所的資訊。\n\n"
        "如果用戶上傳「聯招」即聯合招生的名單，且名單無明確指出包含之系所，"
        "可以放寬以上系所的限制，若「名單包含該系所」是合理的，就可以繼續。"
        
        "重要提醒：請檢查名單中是否包含真實的學生人名。\n"
        "- 如果名單主要由 准考證號碼、學號、編號 等組成，而沒有實際的中文人名，請標記為 'names_available: false'。\n"
        "- 如果有實際的人名（即使被隱藏符遮蔽），請標記為 'names_available: true'。\n\n"
        
        "以下是提取名單的具體要求：\n"
        "這是一份台灣大學或研究所的學生名單（來自網頁）。\n"
        "請提取所有學生人名或編號。\n"
        "隱私遮蔽符（如 X、O、○、●、□、■、* 等）請一律替換為星號 *。\n"
        "若名單中多數名字包含 * 而少數沒有，請合理推測那些缺少 * 的名字其實也有隱藏符（補上 *）。\n"
        "例如：若名單有 張*睿、林*茹、盧嘉，請再次確認該人名，並推理最後一個視為 盧*嘉是否合理。\n"
        "輸出格式：純 JSON，{'names': ['姓名1','姓名2',...], 'names_available': true/false}。\n"
        "不要解釋，不要加註解，不要 ```json。\n\n"
        
        "網頁內容：\n" + text_content
    )
    
    config = GenerateContentConfig(temperature=0.0, max_output_tokens=1024)
    
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=config
        )
        raw_content = response.text.strip()
        expected_error_message = f"網頁未包含{school_dep}名單"
        
        if raw_content == expected_error_message:
            return {"error": raw_content}
        
        clean_json_str = clean_markdown_json(raw_content)
        parsed = json.loads(clean_json_str)
        
        if isinstance(parsed.get('names'), list):
            has_names = parsed.get('names_available', True)
            return {"success": True, "names": parsed['names'], "has_names": has_names}
        else:
            return {"error": "回傳格式不符合 {'names': [...], 'names_available': bool}"}
        
    except json.JSONDecodeError as e:
        return {
            "error": "JSON parse failed",
            "raw": raw_content,
            "cleaned": clean_json_str if 'clean_json_str' in locals() else raw_content,
            "parse_error": str(e)
        }
    except Exception as e:
        return {"error": f"API call failed: {str(e)}"}


def parse_namelist_from_file(file_bytes: io.BytesIO, school_dep: str):
    """從記憶體 BytesIO 檔案解析名單（PDF先轉文字，其他直接傳檔案）"""

    client = get_genai_client()
    MODEL_NAME = "gemini-2.5-flash"

    try:
        # 確保 BytesIO 指標在開頭
        file_bytes.seek(0)
        file_content = file_bytes.read()
    except Exception as e:
        return {"error": f"無法讀取檔案: {str(e)}"}

    # 嘗試從 BytesIO 取得副檔名（若 Flask 傳入 file 物件可附上 filename）
    ext = getattr(file_bytes, "name", None)
    if ext:
        _, ext = os.path.splitext(ext)
    else:
        ext = ""  # 若無檔名，略過副檔名邏輯
    ext = ext.lower()

    mime_types = {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
    }
    mime_type = mime_types.get(ext, "image/jpeg")  # 預設為 jpeg，避免 application/octet-stream

    # === Step 1: 處理 PDF 類型 ===
    if ext == ".pdf":
        try:
            # 直接從記憶體中提取 PDF 文字層
            file_bytes.seek(0)
            extracted_text = extract_text(file_bytes)
            if not extracted_text.strip():
                return {"error": "PDF 無文字層或為掃描圖片"}
            
            # PDF 只傳送純文字給 Gemini
            prompt = (
                f"請先檢查這份 PDF 文字內容是否包含「{school_dep}」的名單。\n"
                f"若內容未包含「{school_dep}」的名單，請**直接回傳純文字訊息：「檔案未包含{school_dep}名單」**。\n"
                "若包含，請繼續並**只提取該系所的名單**，忽略其他系所資訊。\n\n"
                "這份資料是由 PDF 轉換而來的純文字，請容忍排版錯亂或多餘空白，不需理會頁碼、註解、或頁首頁尾。\n"
                "若出現以下任一情況，請直接放寬條件為「包含該系所」並繼續提取名單：\n"
                "1. 名單屬於『聯合招生』或『共同招生』形式。\n"
                "2. PDF 文字中沒有明確標示系所，但若將該系所視為名單的一部分「不算荒謬、不顯得不合理」，則視為包含該系所並繼續提取。\n"
                "重要提醒：請檢查名單中是否包含真實的學生人名。\n"
                "- 若名單主要由 准考證號碼、學號、編號 等組成，而沒有實際的中文人名，請標記 'names_available': false。\n"
                "- 若有實際人名（即使有遮蔽符），請標記 'names_available': true。\n\n"
                "以下是提取名單的具體要求：\n"
                "這是一份台灣大學或研究所的學生名單。\n"
                "請提取所有學生人名或編號。\n"
                "隱私遮蔽符（X、O、○、●、□、■、* 等）一律替換為星號 *。\n"
                "若部分名字未被遮蔽但根據模式可推測應有遮蔽，請補上 *。\n"
                "例如：張*睿、林*茹、盧嘉 → 合理輸出為 張*睿、林*茹、盧*嘉。\n\n"
                "輸出格式：純 JSON，{'names': ['姓名1','姓名2',...], 'names_available': true/false}\n"
                "不要加註解、不要 ```json。\n\n"
                f"PDF 文字內容：\n{extracted_text}"
            )
            
            config = GenerateContentConfig(temperature=0.0, max_output_tokens=2048)
            
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=config
            )
            
        except Exception as e:
            return {"error": f"PDF 文字層提取失敗: {str(e)}"}

    else:
        # 非 PDF 類型（圖片、Excel），轉成 base64 並傳給 Gemini
        file_data = base64.standard_b64encode(file_content).decode("utf-8")
        
        prompt = (
            f"請先檢查這份資料內容是否包含「{school_dep}」的名單。\n"
            f"若資料未包含「{school_dep}」的名單，請**直接回傳純文字訊息：「檔案未包含{school_dep}名單」**。\n"
            "若包含，請繼續並**只提取該系所的名單**，忽略其他系所資訊。\n\n"
            "若出現以下任一情況，請直接放寬條件為「包含該系所」並繼續提取名單：\n"
            "1. 名單屬於『聯合招生』或『共同招生』形式。\n"
            "2. PDF 文字中沒有明確標示系所，但若將該系所視為名單的一部分「不算荒謬、不顯得不合理」，則視為包含該系所並繼續提取。\n"
            "重要提醒：請檢查名單中是否包含真實的學生人名。\n"
            "- 若名單主要由 准考證號碼、學號、編號 等組成，而沒有實際的中文人名，請標記 'names_available': false。\n"
            "- 若有實際人名（即使有遮蔽符），請標記 'names_available': true。\n\n"
            "以下是提取名單的具體要求：\n"
            "這是一份台灣大學或研究所的學生名單。\n"
            "請提取所有學生人名或編號。\n"
            "隱私遮蔽符（X、O、○、●、□、■、* 等）一律替換為星號 *。\n"
            "若部分名字未被遮蔽但根據模式可推測應有遮蔽，請補上 *。\n"
            "例如：張*睿、林*茹、盧嘉 → 合理輸出為 張*睿、林*茹、盧*嘉。\n\n"
            "輸出格式：純 JSON，{'names': ['姓名1','姓名2',...], 'names_available': true/false}\n"
            "不要加註解、不要 ```json。\n"
        )
        
        config = GenerateContentConfig(temperature=0.0, max_output_tokens=2048)
        
        try:
            contents = [{
                "role": "user",
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": file_data,
                        }
                    },
                    {"text": prompt}
                ]
            }]

            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=contents,
                config=config
            )
        except Exception as e:
            return {"error": f"API call failed: {str(e)}"}

    # === Step 2: 處理回應（PDF 和非 PDF 共用） ===
    try:
        raw_content = response.text.strip()
        expected_error_message = f"檔案未包含{school_dep}名單"

        if raw_content == expected_error_message:
            return {"error": raw_content}

        clean_json_str = clean_markdown_json(raw_content)
        parsed = json.loads(clean_json_str)

        if isinstance(parsed.get('names'), list):
            has_names = parsed.get('names_available', False)
            return {"success": True, "names": parsed['names'], "has_names": has_names}
        else:
            return {"error": "回傳格式不符合 {'names': [...], 'names_available': bool}"}

    except json.JSONDecodeError as e:
        return {
            "error": "JSON parse failed",
            "raw": raw_content,
            "cleaned": clean_json_str if 'clean_json_str' in locals() else raw_content,
            "parse_error": str(e)
        }
    except Exception as e:
        return {"error": f"API call failed: {str(e)}"}


def parse_namelist_with_source(source_type, source_value, school_dep):
    """統一的名單解析函數，支援檔案或 URL"""
    if source_type == 'file':
        return parse_namelist_from_file(source_value, school_dep)
    elif source_type == 'url':
        return parse_namelist_from_url(source_value, school_dep)
    else:
        return {"error": f"不支援的來源類型：{source_type}"}


def validate_name_in_namelist(user_name, namelist_str):
    """驗證使用者名稱是否在名單中"""
    if not namelist_str or not user_name:
        return False, None
    
    names = [n.strip() for n in namelist_str.split(',') if n.strip()]
    
    for name in names:
        if user_name == name:
            return True, name
        
        if '*' in name:
            if len(user_name) != len(name):
                continue
            
            match = True
            for user_c, name_c in zip(user_name, name):
                if name_c != '*' and user_c != name_c:
                    match = False
                    break
            
            if match:
                return True, name
    
    return False, None


if __name__ == "__main__":
    is_valid, matched = validate_name_in_namelist("張德睿", "張*睿,林*茹,王小明")
    print(f"驗證結果：{is_valid}, 匹配名字：{matched}")
