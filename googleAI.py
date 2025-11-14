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
try:
    credentials, project = google.auth.default()
except Exception as e:
    print(f"[WARNING] Google auth.default() 失敗: {e}")
    credentials = None
    project = None

# 設置專案和位置（從環境變數讀取，如果沒有則使用預設值）
os.environ["GOOGLE_CLOUD_PROJECT"] = os.getenv("GOOGLE_CLOUD_PROJECT", project or "sinuous-origin-454613-h1")
os.environ["GOOGLE_CLOUD_LOCATION"] = os.getenv("GOOGLE_CLOUD_LOCATION", "global")

# 本地開發時強制使用 Vertex AI（不使用 API KEY）
GOOGLE_GENAI_USE_VERTEXAI = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "True").lower() == "true"
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True" if GOOGLE_GENAI_USE_VERTEXAI else "False"


def get_genai_client():
    """獲取配置好的 genai.Client，使用 Application Default Credentials"""
    # 當 GOOGLE_GENAI_USE_VERTEXAI=True 時，genai.Client 會自動使用 ADC
    # 如果需要明確傳遞憑證，可以通過 HttpOptions 傳遞
    try:
        return genai.Client(http_options=HttpOptions(api_version="v1"))
    except Exception as e:
        print(f"[ERROR] 建立 Gemini 客戶端失敗: {e}")
        print("[INFO] 請設置 GOOGLE_APPLICATION_CREDENTIALS 環境變數或執行 gcloud auth application-default login")
        raise

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
    """清理 Markdown 格式的 JSON，並修復常見的格式問題"""
    if not raw_text:
        return None
    
    raw_text = raw_text.strip()
    
    # 方式 1：從 markdown 程式碼塊中提取
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw_text, re.DOTALL)
    if match:
        return match.group(1)
    
    # 方式 2：直接找第一個 { 和最後一個 }
    start = raw_text.find('{')
    end = raw_text.rfind('}')
    if start != -1 and end != -1 and start < end:
        json_str = raw_text[start:end+1]
        
        # 修復常見的 JSON 格式問題
        # 移除未轉義的換行符
        json_str = json_str.replace('\n', ' ').replace('\r', ' ')
        
        # 修復雙引號問題（例如 "names": ["a", "b"] 中間的特殊字符）
        # 使用更寬容的方式：允許在字符串中包含某些特殊字符
        try:
            # 嘗試直接解析
            json.loads(json_str)
            return json_str
        except json.JSONDecodeError:
            # 如果失敗，嘗試修復
            # 移除可能導致問題的控制字符
            json_str = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', json_str)
            return json_str
    
    return raw_text


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
        f"從這份網頁內容中提取「{school_dep}」的名單。\n\n"
        "輸出 JSON（無其他文字）：\n"
        f"找到名單：{{\"success\": true, \"names\": [\"名1\", \"名2\"], \"names_available\": true}}\n"
        f"未找到或是別系所：{{\"success\": false, \"reason\": \"未找到 {school_dep} 名單\"}}\n\n"
        "指示：\n"
        "1. 若網頁明確標示是 {school_dep} 或包含 {school_dep} 的名單→提取\n"
        "2. 若網頁完全沒標示系所，但只有名字+編號，且無任何其他系所標記→視為該系所名單，提取\n"
        "3. 若網頁清晰標示是【其他系所】的名單→拒絕（返回 success: false）\n"
        "4. 聯合招生或共同招生時，若清晰標示招生單位包含此系所，則提取\n"
        "5. 遮蔽符（X、○、●等）轉為星號 *（例：張*睿）\n"
        "6. names_available：若名單含真實人名→true，只有准考證號碼→false\n"
        "7. 只輸出 JSON，無須說明\n\n"
        "網頁內容：\n" + text_content
    )
    
    config = GenerateContentConfig(temperature=0.0)
    
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=config
        )
        
        # 檢查 response 物件和 text 屬性
        if not hasattr(response, 'text') or response.text is None:
            return {"error": f"API 回應格式無效: {type(response)}"}
        
        raw_content = response.text.strip()
        
        # 檢查回應是否為空
        if not raw_content:
            return {"error": "API 回應為空"}

        # 清理 markdown 格式並解析 JSON
        clean_json_str = clean_markdown_json(raw_content)
        
        if not clean_json_str:
            return {"error": "無法解析 API 回應"}
        
        parsed = json.loads(clean_json_str)

        # 檢查是否是失敗回應
        if not parsed.get('success'):
            reason = parsed.get('reason', '未知原因')
            return {"error": reason}
        
        if isinstance(parsed.get('names'), list):
            has_names = parsed.get('names_available', True)
            return {"success": True, "names": parsed['names'], "has_names": has_names}
        else:
            return {"error": f"回傳格式不符合預期。收到: {json.dumps(parsed, ensure_ascii=False)[:200]}"}
        
    except json.JSONDecodeError as e:
        return {
            "error": f"JSON parse failed: {str(e)}",
            "raw_content": raw_content[:500] if 'raw_content' in locals() and raw_content else "(empty)",
            "cleaned": clean_json_str[:500] if 'clean_json_str' in locals() and clean_json_str else "(not cleaned)"
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

    # 初始化回應變數和 raw_content
    response = None
    raw_content = None

    # === Step 1: 處理 PDF 類型 ===
    if ext == ".pdf":
        try:
            # 直接從記憶體中提取 PDF 文字層
            file_bytes.seek(0)
            extracted_text = extract_text(file_bytes)
            print(extracted_text)
            if not extracted_text.strip():
                return {"error": "PDF 無文字層或為掃描圖片"}
            
            # PDF 只傳送純文字給 Gemini
            prompt = (
                f"從這份 PDF 文字中提取「{school_dep}」的名單。\n\n"
                "輸出 JSON（無其他文字）：\n"
                f"找到名單：{{\"success\": true, \"names\": [\"名1\", \"名2\"], \"names_available\": true}}\n"
                f"未找到或是別系所：{{\"success\": false, \"reason\": \"未找到 {school_dep} 名單\"}}\n\n"
                "指示：\n"
                "1. 若文件明確標示是 {school_dep} 或包含 {school_dep} 的名單→提取\n"
                "2. 若文件完全沒標示系所，但只有名字+編號，且無任何其他系所標記→視為該系所名單，提取\n"
                "3. 若文件清晰標示是【其他系所】的名單→拒絕（返回 success: false）\n"
                "4. 聯合招生或共同招生時，若清晰標示招生單位包含此系所，則提取\n"
                "5. 遮蔽符（X、○、●等）轉為星號 *（例：張*睿）\n"
                "6. names_available：若名單含真實人名→true，只有准考證號碼→false\n"
                "7. 只輸出 JSON，無須說明\n\n"
                f"PDF 文字：\n{extracted_text}"
            )
            
            config = GenerateContentConfig(temperature=0.0)
            
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=config
            )
            
        except Exception as e:
            return {"error": f"PDF 文字層提取失敗或 API 呼叫失敗: {str(e)}"}

    else:
        # 非 PDF 類型（圖片、Excel），轉成 base64 並傳給 Gemini
        file_data = base64.standard_b64encode(file_content).decode("utf-8")
        
        prompt = (
            f"從這份資料中提取「{school_dep}」的名單。\n\n"
            "輸出 JSON（無其他文字）：\n"
            f"找到名單：{{\"success\": true, \"names\": [\"名1\", \"名2\"], \"names_available\": true}}\n"
            f"未找到或是別系所：{{\"success\": false, \"reason\": \"未找到 {school_dep} 名單\"}}\n\n"
            "指示：\n"
            "1. 若資料明確標示是 {school_dep} 或包含 {school_dep} 的名單→提取\n"
            "2. 若資料完全沒標示系所，但只有名字+編號，且無任何其他系所標記→視為該系所名單，提取\n"
            "3. 若資料清晰標示是【其他系所】的名單→拒絕（返回 success: false）\n"
            "4. 聯合招生或共同招生時，若清晰標示招生單位包含此系所，則提取\n"
            "5. 遮蔽符（X、○、●等）轉為星號 *（例：張*睿）\n"
            "6. names_available：若名單含真實人名→true，只有准考證號碼→false\n"
            "7. 只輸出 JSON，無須說明"
        )
        
        config = GenerateContentConfig(temperature=0.0)
        
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

    print(response)
    # === Step 2: 處理回應（PDF 和非 PDF 共用） ===
    if not response:
        return {"error": "未收到 API 回應"}
    
    try:
        # 檢查 response 物件和 text 屬性
        if not hasattr(response, 'text') or response.text is None:
            return {"error": f"API 回應格式無效: {type(response)}"}
        
        raw_content = response.text.strip()
        print(raw_content)
        print("=====")
        # 檢查回應是否為空
        if not raw_content:
            return {"error": "API 回應為空"}

        # 清理 markdown 格式並解析 JSON
        clean_json_str = clean_markdown_json(raw_content)
        print(clean_json_str)
        # 再次檢查清理後是否為空
        if not clean_json_str:
            return {"error": "無法解析 API 回應"}
        
        parsed = json.loads(clean_json_str)
        print(3)
        # 驗證回應格式
        if not isinstance(parsed, dict):
            return {"error": "回傳的 JSON 不是物件格式"}
        
        # 檢查是否是失敗回應
        if not parsed.get('success'):
            reason = parsed.get('reason', '未知原因')
            return {"error": reason}
        
        # 提取成功回應的資料
        if isinstance(parsed.get('names'), list):
            has_names = parsed.get('names_available', False)
            return {"success": True, "names": parsed['names'], "has_names": has_names}
        else:
            return {"error": f"回傳格式不符合預期。收到: {json.dumps(parsed, ensure_ascii=False)[:200]}"}

    except json.JSONDecodeError as e:
        return {
            "error": f"JSON parse failed: {str(e)}",
            "hint": "Gemini 可能返回了非標準 JSON 格式。請檢查以下內容：",
            "raw_content": raw_content[:300] if 'raw_content' in locals() and raw_content else "(empty)",
            "cleaned": clean_json_str[:300] if 'clean_json_str' in locals() and clean_json_str else "(not cleaned)",
            "parse_error": str(e)
        }
    except Exception as e:
        return {
            "error": f"處理回應失敗: {str(e)}",
            "raw_content": raw_content[:500] if 'raw_content' in locals() and raw_content else "(empty)"
        }


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
