import io
import json
import os
import re
import time
import requests
import base64
import random
import tempfile
from google.cloud import vision
from google import genai
from google.genai.types import HttpOptions, GenerateContentConfig
from bs4 import BeautifulSoup
from urllib.parse import urlparse

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = r"F:\Codings\Python_Codes\Python工作區\專案區\研究生好朋友\BackEnd\sinuous-origin-454613-h1-1e815e48efbc.json"
os.environ["GOOGLE_CLOUD_PROJECT"] = "sinuous-origin-454613-h1"
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

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
    client = genai.Client(http_options=HttpOptions(api_version="v1"))
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
    
    client = genai.Client(http_options=HttpOptions(api_version="v1"))
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


def parse_namelist_from_file(file_path, school_dep):
    """從檔案解析名單"""

    client = genai.Client(http_options=HttpOptions(api_version="v1"))
    MODEL_NAME = "gemini-2.5-flash"
    
    try:
        with open(file_path, 'rb') as f:
            file_content = f.read()
    except Exception as e:
        return {"error": f"無法讀取檔案: {str(e)}"}
    
    _, ext = os.path.splitext(file_path)
    ext = ext.lower()
    
    mime_types = {
        '.pdf': 'application/pdf',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        '.xls': 'application/vnd.ms-excel',
    }
    mime_type = mime_types.get(ext, 'application/octet-stream')
    
    prompt = (
        f"請先檢查這份檔案內容有沒有包含「{school_dep}」的名單。\n"
        f"如果檔案未包含「{school_dep}」的名單，請**直接回傳純文字訊息：「檔案未包含{school_dep}名單」**，不要回傳 JSON 或任何其他內容。\n"
        "如果包含，請繼續並**只提取該系所的名單**，忽略檔案中其他系所的資訊。\n\n"
        "如果用戶上傳「聯招」即聯合招生的名單，且名單無明確指出包含之系所，"
        "可以放寬以上系所的限制，若「名單包含該系所」是合理的，就可以繼續。"

        "重要提醒：請檢查名單中是否包含真實的學生人名。\n"
        "- 如果名單主要由 准考證號碼、學號、編號 等組成，而沒有實際的中文人名，請標記為 'names_available: false'。\n"
        "- 如果有實際的人名（即使被隱藏符遮蔽），請標記為 'names_available: true'。\n\n"
        
        "以下是提取名單的具體要求：\n"
        "這是一份台灣大學或研究所的學生名單。\n"
        "請提取所有學生人名或編號。\n"
        "隱私遮蔽符（如 X、O、○、●、□、■、* 等）請一律替換為星號 *。\n"
        "若名單中多數名字包含 * 而少數沒有，請合理推測那些缺少 * 的名字其實也有隱藏符（補上 *）。\n"
        "例如：若名單有 張*睿、林*茹、盧嘉，請再次確認該人名，並推理最後一個視為 盧*嘉是否合理。\n"
        "輸出格式：純 JSON，{'names': ['姓名1','姓名2',...], 'names_available': true/false}。\n"
        "不要解釋，不要加註解，不要 ```json。\n"
    )
    
    config = GenerateContentConfig(temperature=0.0)
    
    try:
        file_data = base64.standard_b64encode(file_content).decode('utf-8')
        
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=[
                {
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
                }
            ],
            config=config
        )
        raw_content = response.text.strip()
        expected_error_message = f"檔案未包含{school_dep}名單"
        
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
