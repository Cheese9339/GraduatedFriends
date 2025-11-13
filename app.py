from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine, text
from werkzeug.security import generate_password_hash, check_password_hash
import numpy as np
import datetime
from utils import send_mail
from dotenv import load_dotenv
import os
import jwt
import tempfile
import json
import googleAI

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE_DIR, '.env')
load_dotenv(env_path)
SECRET_KEY = os.getenv("SECRET_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

app = Flask(__name__)
CORS(app, origins=[
    "https://storage.googleapis.com"
], supports_credentials=True)
engine = create_engine(
    DATABASE_URL,
    connect_args={"sslmode": "require"},  # 確保 SSL
    pool_pre_ping=True                     # 自動檢查連線是否可用
)

# >>>>>>>>>>>>>>> register >>>>>>>>>>>>>>> #
@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json()
    username = data.get('name')
    email = data.get('email')
    password = data.get('password')
    hashed_pw = generate_password_hash(password)  # 密碼 hash
    create_time = datetime.datetime.now(datetime.timezone.utc)
    verify_result, verify_result_code = register_verify_email(data)

    if verify_result_code != 200:
        return verify_result, verify_result_code
    else:
    # 插入資料
        with engine.begin() as conn:
            try:
                # 將解析出的 school/department 一併儲存到 users 表（若表格有對應欄位）
                school = data.get('school')
                department = data.get('department')
                create_user_sql = text("""
                    INSERT INTO "users" (username, email, password_hash, school, department, created_at)
                    VALUES (:username, :email, :password_hash, :school, :department, :created_at)
                """)
                conn.execute(create_user_sql, {
                    "username": username,
                    "email": email,
                    "password_hash": hashed_pw,
                    "school": school,
                    "department": department,
                    "created_at": create_time
                })
            except Exception as e:
                return jsonify({"success": False, "message": f"註冊失敗: {str(e)}"}), 400

    return jsonify({"success": True, "message": "註冊成功！"}), 201

@app.route('/api/register_captcha_apply', methods=['POST'])
def register_captcha_apply():
    data = request.get_json()
    email = data.get('email')
    create_time = datetime.datetime.now(datetime.timezone.utc)

    verification_code = str(np.random.randint(100000, 999999))
    # 插入資料
    with engine.begin() as conn:
        exist_sql = text("""
            SELECT * 
            FROM email_verifications
            WHERE email = :email
            ORDER BY created_at DESC
            LIMIT 1
        """)
        exist = conn.execute(exist_sql, {"email": email}).mappings().fetchone()
        
        if exist:
            if exist["used"] == True:
                return jsonify({"success": False, "message": "此電子郵件已經驗證過，將自動跳轉至登入頁面"}), 409
            
            else:
                update_emailverification_sql = text("""
                    UPDATE email_verifications
                    SET verification_code = :verification_code,
                        expires_at = :expires_at,
                        created_at = :created_at
                    WHERE email = :email
                """)
                conn.execute(update_emailverification_sql, {
                    "email": email,
                    "verification_code": verification_code,
                    "expires_at": create_time + datetime.timedelta(minutes=5),
                    "created_at": create_time
                })

                if send_mail(email, "captcha", verification_code) == True:
                    return jsonify({"success": True, "message": "驗證碼已寄出，請在五分鐘內驗證"}), 201
                else:
                    return jsonify({"success": False, "message": "驗證碼寄出失敗"}), 400

        else:
            create_emailverification_sql = text("""
                    INSERT INTO email_verifications (email, verification_code, expires_at, used, created_at)
                    VALUES (:email, :verification_code, :expires_at, :used, :created_at)
                """)
            conn.execute(create_emailverification_sql, {
                "email": email,
                "verification_code": verification_code,
                "expires_at": create_time + datetime.timedelta(minutes=5),
                "used": False,
                "created_at": create_time
            })
            if send_mail(email, "captcha", verification_code) == True:
                return jsonify({"success": True, "message": "驗證碼已寄出，請在五分鐘內驗證"}), 201
            else:
                return jsonify({"success": False, "message": "驗證碼寄出失敗"}), 400


@app.route('/api/parse_id', methods=['POST'])
def api_parse_id():
    """接收上傳的學生證影像，呼叫 googleAI 做 OCR + 解析，回傳 school/department/name"""
    if 'file' not in request.files:
        return jsonify({"success": False, "message": "請上傳檔案 (file)"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False, "message": "未提供檔案名稱"}), 400

    # 儲存暫存檔並交給 googleAI 處理
    tmp = None
    try:
        suffix = os.path.splitext(file.filename)[1] or '.jpg'
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        file.save(tmp.name)
        tmp.close()

        # googleAI 提供 read_student_id(path) -> ocr_text
        ocr_text = googleAI.read_student_id(tmp.name)
        # parse_ocr_with_google_ai 返回 dict 或 error 資訊
        parsed = googleAI.parse_ocr_with_google_ai(ocr_text)

        if isinstance(parsed, dict) and parsed.get('school') and parsed.get('department') and parsed.get('name'):
            return jsonify({"success": True, "result": parsed}), 200
        else:
            return jsonify({"success": False, "message": "解析失敗", "raw": parsed}), 400
    except Exception as e:
        return jsonify({"success": False, "message": f"解析錯誤: {str(e)}"}), 500
    finally:
        try:
            if tmp:
                os.unlink(tmp.name)
        except:
            pass

def register_verify_email(data):
    email = data.get('email')
    captcha = data.get('captcha')
    verify_time = datetime.datetime.now(datetime.timezone.utc)
    with engine.begin() as conn:
        verification = conn.execute(text("""
            SELECT * 
            FROM email_verifications
            WHERE email = :email
            ORDER BY created_at DESC
            LIMIT 1
        """), {"email": email}).mappings().fetchone()


        if not verification:
            return jsonify({"success": False, "message": "此電子郵件尚未申請驗證碼"}), 400
        
        if verification["used"] == True:
                return jsonify({"success": False, "message": "此電子郵件已註冊，將自動跳轉至登入頁面"}), 409
        
        expires_at_aware = verification['expires_at'].replace(tzinfo=datetime.timezone.utc)
        if verify_time > expires_at_aware:
            return jsonify({"success": False, "message": "驗證碼過期，請重新申請"}), 400
        
        
        if verification['verification_code'] == captcha:
            # 驗證成功：更新 used
            update_verification = text("""
                UPDATE email_verifications
                SET used = TRUE
                WHERE id = :id
            """)
            conn.execute(update_verification, {"id": verification['id']})
        return jsonify({"success": True, "message": "驗證成功"}), 200
# <<<<<<<<<<<<<<< register <<<<<<<<<<<<<<< #

# >>>>>>>>>>>>>>> login >>>>>>>>>>>>>>> #
@app.route('/api/login', methods=['POST'])
def login():
    import jwt
    data = request.get_json()
    email = data.get('email')
    entered_password = data.get('password')
    login_time = datetime.datetime.now(datetime.timezone.utc)
    with engine.begin() as conn:
        exist_sql = text("""
            SELECT * 
            FROM users
            WHERE email = :email
            ORDER BY created_at DESC
            LIMIT 1
        """)
        exist = conn.execute(exist_sql, {"email": email}).mappings().fetchone()

        if not exist:
            return jsonify({"success": False, "message": "此電子郵件尚未註冊，將自動跳轉至註冊頁面"}), 404
        
        sql_password = exist["password_hash"]
        if not check_password_hash(sql_password, entered_password):
            return jsonify({"success": False, "message": "密碼錯誤"}), 400
        
        user_id = exist["user_id"]
        username = exist["username"]
        payload = {
            "user_id": user_id,
            "name": username,
            "email": email,
            "exp": login_time + datetime.timedelta(hours=2),
            "iat": login_time
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
        response = jsonify({"success": True, "message": "登入成功"})
        response.set_cookie("token", token, httponly=True, max_age=2*60*60, samesite="None", secure=True)
        return response, 200
# <<<<<<<<<<<<<<< login <<<<<<<<<<<<<<< #

# >>>>>>>>>>>>>>> token verification >>>>>>>>>>>>>>> #
@app.route('/api/verify_token', methods=['GET'])
def verify_token():
    token = request.cookies.get('token')
    if not token:
        return jsonify({"success": False, "message": "未登入"}), 401
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return jsonify({
            "success": True,
            "user": {
                "user_id": payload["user_id"],
                "name": payload["name"],
                "email": payload["email"]
            }
        }), 200
    except jwt.ExpiredSignatureError:
        response = jsonify({"success": False, "message": "登入已過期"}), 401
        response[0].delete_cookie('token')
        return response
    except jwt.InvalidTokenError:
        response = jsonify({"success": False, "message": "無效的登入狀態"}), 401
        response[0].delete_cookie('token')
        return response

def token_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get('token')
        if not token:
            return jsonify({"success": False, "message": "請先登入"}), 401
        try:
            jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            response = jsonify({"success": False, "message": "登入已過期"}), 401
            response[0].delete_cookie('token')
            return response
        except:
            response = jsonify({"success": False, "message": "無效的登入狀態"}), 401
            response[0].delete_cookie('token')
            return response
        return f(*args, **kwargs)
    return decorated

# >>>>>>>>>>>>>>> schools / departments / lookup >>>>>>>>>>>>>>> #
@app.route('/api/schools', methods=['GET'])
@token_required
def api_get_schools():
    """回傳學校清單，從 schools 表取 distinct school 欄位，格式: [{id, name}, ...]"""
    try:
        with engine.begin() as conn:
            sql = text("""
                SELECT DISTINCT school
                FROM schools
                ORDER BY school
            """)
            rows = conn.execute(sql).mappings().all()
            schools = [{ 'id': r['school'], 'name': r['school'] } for r in rows]
        return jsonify(schools), 200
    except Exception as e:
        return jsonify({"success": False, "message": f"取得學校清單失敗: {str(e)}"}), 500


@app.route('/api/schools/<school_id>/departments', methods=['GET'])
@token_required
def api_get_departments(school_id):
    """回傳指定學校的系所清單，格式: [{id: '學校名/系所名', name: '系所名'}, ...]"""
    try:
        with engine.begin() as conn:
            sql = text("""
                SELECT DISTINCT dep_name
                FROM schools
                WHERE school = :school_id
                ORDER BY dep_name
            """)
            rows = conn.execute(sql, {"school_id": school_id}).mappings().all()
            # id 使用 "學校名/系所名" 的格式，確保唯一性
            depts = [{ 
                'id': f"{school_id}/{r['dep_name']}", 
                'name': r['dep_name'] 
            } for r in rows]
        return jsonify(depts), 200
    except Exception as e:
        return jsonify({"success": False, "message": f"取得系所清單失敗: {str(e)}"}), 500


@app.route('/api/degrees', methods=['GET'])
@token_required
def api_get_degrees():
    """若沒傳 school/dep query，回傳全部學制去重；
       若傳了 school & dep，回傳該校該系的學制（若找不到回傳空陣列）。
    """
    school_q = request.args.get('school')
    dep_q = request.args.get('dep')
    try:
        if school_q and dep_q:
            # 取得特定系的 degree 欄位
            with engine.begin() as conn:
                sql = text("""
                    SELECT degree
                    FROM schools
                    WHERE school = :school_id AND dep_name = :dep_name
                    LIMIT 1
                """)
                row = conn.execute(sql, {"school_id": school_q, "dep_name": dep_q}).mappings().fetchone()
                if not row or not row.get('degree'):
                    return jsonify([]), 200
                deg = row.get('degree')
                parts = [p.strip() for p in deg.split(',') if p.strip()]
                return jsonify(parts), 200
        else:
            degrees_set = set()
            with engine.begin() as conn:
                sql = text('SELECT degree FROM schools')
                rows = conn.execute(sql).mappings().all()
                for r in rows:
                    deg = r.get('degree')
                    if not deg:
                        continue
                    parts = [p.strip() for p in deg.split(',') if p.strip()]
                    for p in parts:
                        degrees_set.add(p)
            degrees = sorted(list(degrees_set))
            return jsonify(degrees), 200
    except Exception as e:
        return jsonify({"success": False, "message": f"取得學制清單失敗: {str(e)}"}), 500


@app.route('/api/check_namelist', methods=['GET'])
@token_required
def api_check_namelist():
    """檢查指定系所是否有名單，以及名單狀態。
       Query params: school, department
       回傳：{ has_namelist: true/false, namelist: "..." }
    """
    school = request.args.get('school')
    department = request.args.get('department')
    
    if not school or not department:
        return jsonify({"success": False, "message": "需要提供 school 和 department 參數"}), 400
    
    try:
        with engine.begin() as conn:
            sql = text("""
                SELECT namelist
                FROM schools
                WHERE school = :school AND dep_name = :department
                LIMIT 1
            """)
            row = conn.execute(sql, {"school": school, "department": department}).mappings().fetchone()
            
            if not row:
                return jsonify({
                    "success": True,
                    "has_namelist": False,
                    "message": "該系所暫無名單，請上傳"
                }), 200
            
            namelist = row['namelist']
            if not namelist or namelist.strip() == '':
                return jsonify({
                    "success": True,
                    "has_namelist": False,
                    "message": "該系所暫無名單，請上傳"
                }), 200
            
            return jsonify({
                "success": True,
                "has_namelist": True,
                "namelist": namelist
            }), 200
            
    except Exception as e:
        return jsonify({"success": False, "message": f"檢查名單失敗: {str(e)}"}), 500


@app.route('/api/upload_namelist', methods=['POST'])
@token_required
def api_upload_namelist():
    """上傳或解析名單，支援三種方式：
       1. 上傳檔案（PDF/圖片/Excel）- POST params: file (multipart), school, department, degree
       2. 提供 URL - POST JSON: {url, school, department, degree}
       3. 手動輸入名單 - POST JSON: {names: [...], school, department, degree}
       
       namelist 欄位儲存為 JSON dict：{"degree1": "name1,name2", "degree2": "name3,name4"}
    """
    school = request.form.get('school') or (request.get_json() or {}).get('school')
    department = request.form.get('department') or (request.get_json() or {}).get('department')
    degree = request.form.get('degree') or (request.get_json() or {}).get('degree')
    
    if not school or not department or not degree:
        return jsonify({"success": False, "message": "需要提供 school、department 和 degree 參數"}), 400
    
    result = None
    tmp = None
    
    try:
        # 方式 1：檔案上傳
        if 'file' in request.files:
            file = request.files['file']
            if file.filename == '':
                return jsonify({"success": False, "message": "未提供檔案名稱"}), 400
            
            suffix = os.path.splitext(file.filename)[1] or '.pdf'
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            file.save(tmp.name)
            tmp.close()
            
            result = googleAI.parse_namelist_from_file(tmp.name, school+department+degree)
        
        # 方式 2：提供 URL
        elif 'url' in (request.get_json() or {}):
            url = request.get_json().get('url')
            if not url:
                return jsonify({"success": False, "message": "URL 不能為空"}), 400
            
            result = googleAI.parse_namelist_from_url(url, school+department+degree)
        
        # 方式 3：手動輸入名單
        elif 'names' in (request.get_json() or {}):
            names = request.get_json().get('names')
            if not isinstance(names, list) or not names:
                return jsonify({"success": False, "message": "names 必須是非空陣列"}), 401
            
            result = {"success": True, "names": names}
        
        else:
            return jsonify({"success": False, "message": "請提供檔案、URL 或手動名單"}), 402
        
        # 檢查結果
        if not result or not result.get('success'):
            return jsonify({
                "success": False,
                "message": result.get('error', '解析名單失敗') if result else "解析失敗"
            }), 403
        
        names = result.get('names', [])
        if not names:
            return jsonify({
                "success": False,
                "message": "未找到人名"
            }), 404
        
        # 檢查是否有實際人名
        has_names = result.get('has_names', True)
        
        # 將名單存入 schools 表的 namelist 欄位（JSON 格式）
        # 名單格式：{"degree": {"names": [...], "has_names": bool}}
        names_str = ','.join(names)
        
        with engine.begin() as conn:
            # 先查詢現有的 namelist
            query_sql = text("""
                SELECT namelist
                FROM schools
                WHERE school = :school AND dep_name = :department
                LIMIT 1
            """)
            row = conn.execute(query_sql, {
                "school": school,
                "department": department
            }).mappings().fetchone()
            
            # 初始化或更新 namelist dict，保留其他 degree
            namelist_dict = {}
            if row and row['namelist']:
                try:
                    namelist_dict = json.loads(row['namelist'])
                except Exception:
                    # 若原本是逗號分隔字串，則存到 '預設' key
                    old_str = row['namelist']
                    if old_str.strip():
                        namelist_dict['預設'] = {"names": old_str, "has_names": True}

            # 只更新指定 degree 的名單（新格式：包含 names 和 has_names）
            namelist_dict[degree] = {
                "names": names_str,
                "has_names": has_names
            }
            namelist_json = json.dumps(namelist_dict, ensure_ascii=False)

            # 更新資料庫
            update_sql = text("""
                UPDATE schools
                SET namelist = :namelist
                WHERE school = :school AND dep_name = :department
            """)
            conn.execute(update_sql, {
                "namelist": namelist_json,
                "school": school,
                "department": department
            })
        
        # 返回結果時也包含 has_names 信息
        msg_suffix = " (⚠️ 此系所名單無提供考生姓名)" if not has_names else ""
        
        return jsonify({
            "success": True,
            "message": f"成功上傳 {degree} 學制的名單，共 {len(names)} 人{msg_suffix}",
            "names_count": len(names),
            "degree": degree,
            "has_names": has_names
        }), 201
        
    except Exception as e:
        return jsonify({"success": False, "message": f"上傳失敗: {str(e)}"}), 500
    finally:
        try:
            if tmp and os.path.exists(tmp.name):
                os.unlink(tmp.name)
        except:
            pass


@app.route('/api/validate_name', methods=['POST'])
@token_required
def api_validate_name():
    """驗證使用者名稱是否在指定系所的指定學制名單中。
       JSON: { school, department, degree, name }
       回傳：{ is_valid: true/false, message: "..." }
    """
    data = request.get_json() or {}
    school = data.get('school')
    department = data.get('department')
    degree = data.get('degree')
    name = data.get('name')
    
    if not all([school, department, degree, name]):
        return jsonify({"success": False, "message": "需要提供 school, department, degree, name"}), 400
    
    try:
        with engine.begin() as conn:
            sql = text("""
                SELECT namelist
                FROM schools
                WHERE school = :school AND dep_name = :department
                LIMIT 1
            """)
            row = conn.execute(sql, {"school": school, "department": department}).mappings().fetchone()
            
            if not row or not row['namelist']:
                return jsonify({
                    "success": True,
                    "is_valid": False,
                    "has_names": True,
                    "message": "該系所尚無名單"
                }), 200
            
            # 從 JSON 字典中取出指定 degree 的名單
            has_names = True
            degree_namelist = ''
            try:
                namelist_dict = json.loads(row['namelist'])
                degree_data = namelist_dict.get(degree)
                
                # 處理新格式：{"names": "...", "has_names": bool}
                if isinstance(degree_data, dict):
                    degree_namelist = degree_data.get('names', '')
                    has_names = degree_data.get('has_names', True)
                else:
                    # 處理舊格式：直接是字符串
                    degree_namelist = degree_data or ''
                    has_names = True
            except:
                # 若無法解析 JSON（舊格式），則直接使用整個字串
                degree_namelist = row['namelist']
                has_names = True
            
            if not degree_namelist:
                return jsonify({
                    "success": True,
                    "is_valid": False,
                    "has_names": has_names,
                    "message": f"該系所的 {degree} 尚無名單"
                }), 200
            
            # 如果名單不含人名信息，直接返回 is_valid = True（無法驗證）
            if not has_names:
                return jsonify({
                    "success": True,
                    "is_valid": True,
                    "has_names": False,
                    "message": "此系所名單無提供考生姓名，無法驗證"
                }), 200
            
            # 使用 googleAI 的驗證函式
            is_valid, matched_name = googleAI.validate_name_in_namelist(name, degree_namelist)
            
            if is_valid:
                return jsonify({
                    "success": True,
                    "is_valid": True,
                    "has_names": True,
                    "message": f"您的名字已在名單中"
                }), 200
            else:
                return jsonify({
                    "success": True,
                    "is_valid": False,
                    "has_names": True,
                    "message": f"您的名字 '{name}' 不在此系所的名單中，無法填寫此志願。"
                }), 200
            
    except Exception as e:
        return jsonify({"success": False, "message": f"驗證失敗: {str(e)}"}), 500


@app.route('/api/user_filled_departments', methods=['GET'])
@token_required
def api_user_filled_departments():
    """取得該登入使用者填報過的所有系所（含學制、排名等）。
       回傳：{ success, departments: [{ school, department, degree, rank }, ...] }
    """
    token = request.cookies.get('token')
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"]) if token else None
        user_id = payload.get('user_id') if payload else None
    except Exception:
        return jsonify({"success": False, "message": "無效的 token"}), 401

    if not user_id:
        return jsonify({"success": False, "message": "未取得 user_id"}), 401

    try:
        with engine.begin() as conn:
            sql = text("""
                SELECT school, department, degree, rank
                FROM user_choices
                WHERE user_id = :user_id
                ORDER BY rank ASC
            """)
            rows = conn.execute(sql, {"user_id": user_id}).mappings().fetchall()

            departments = [
                {
                    'school': r['school'],
                    'department': r['department'],
                    'degree': r['degree'],
                    'rank': r['rank']
                }
                for r in rows
            ]

            return jsonify({
                "success": True,
                "departments": departments
            }), 200

    except Exception as e:
        return jsonify({"success": False, "message": f"取得系所清單失敗: {str(e)}"}), 500


@app.route('/api/user_department_stats', methods=['GET'])
@token_required
def api_user_department_stats():
    """取得該使用者在某個系所的個人排名，以及該系所的全校統計。
       Query params: school, department, degree
       回傳：{ user_rank, total_choices, namelist_count, first_choice, fifth_and_after }
    """
    token = request.cookies.get('token')
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"]) if token else None
        user_id = payload.get('user_id') if payload else None
    except Exception:
        return jsonify({"success": False, "message": "無效的 token"}), 401

    if not user_id:
        return jsonify({"success": False, "message": "未取得 user_id"}), 401

    school = request.args.get('school')
    department = request.args.get('department')
    degree = request.args.get('degree')

    if not all([school, department, degree]):
        return jsonify({"success": False, "message": "需要提供 school, department, degree"}), 400

    try:
        with engine.begin() as conn:
            # 取得該使用者在該系所的排名
            user_rank_sql = text("""
                SELECT rank
                FROM user_choices
                WHERE user_id = :user_id AND school = :school AND department = :department AND degree = :degree
                LIMIT 1
            """)
            user_row = conn.execute(user_rank_sql, {
                "user_id": user_id,
                "school": school,
                "department": department,
                "degree": degree
            }).fetchone()
            user_rank = int(user_row[0]) if user_row and user_row[0] is not None else None

            # 全校統計：該系所此學制的填志願人數
            total_sql = text("""
                SELECT COUNT(*) AS cnt
                FROM user_choices
                WHERE school = :school AND department = :department AND degree = :degree
            """)
            total_row = conn.execute(total_sql, {"school": school, "department": department, "degree": degree}).fetchone()
            total_choices = int(total_row[0]) if total_row and total_row[0] is not None else 0

            # 第一志願人數
            first_sql = text("""
                SELECT COUNT(*)
                FROM user_choices
                WHERE school = :school AND department = :department AND degree = :degree AND rank = 1
            """)
            first_row = conn.execute(first_sql, {"school": school, "department": department, "degree": degree}).fetchone()
            first_choice = int(first_row[0]) if first_row and first_row[0] is not None else 0

            # 第五志願後人數 (rank >= 5)
            fifth_sql = text("""
                SELECT COUNT(*)
                FROM user_choices
                WHERE school = :school AND department = :department AND degree = :degree AND rank >= 5
            """)
            fifth_row = conn.execute(fifth_sql, {"school": school, "department": department, "degree": degree}).fetchone()
            fifth_and_after = int(fifth_row[0]) if fifth_row and fifth_row[0] is not None else 0

            # 名單人數：從 schools.namelist dict 解析
            namelist_count = 0
            sql = text("""
                SELECT namelist
                FROM schools
                WHERE school = :school AND dep_name = :department
                LIMIT 1
            """)
            row = conn.execute(sql, {"school": school, "department": department}).mappings().fetchone()
            if row and row.get('namelist'):
                try:
                    namelist_dict = json.loads(row['namelist'])
                    deg_list_str = namelist_dict.get(degree, '')
                except Exception:
                    deg_list_str = row['namelist']

                if deg_list_str and isinstance(deg_list_str, str):
                    namelist_count = len([n for n in deg_list_str.split(',') if n.strip()])

            return jsonify({
                "success": True,
                "user_rank": user_rank,
                "total_choices": total_choices,
                "namelist_count": namelist_count,
                "first_choice": first_choice,
                "fifth_and_after": fifth_and_after
            }), 200

    except Exception as e:
        return jsonify({"success": False, "message": f"取得統計失敗: {str(e)}"}), 500


@app.route('/api/submit_choices', methods=['POST'])
@token_required
def api_submit_choices():
    """接收前端送出的志願序 choices: 
       { choices: [ { selection: 'school/dep', degree: '碩士班' }, ... ] }
       改成每筆志願獨立存一列 (user_id, rank, school, dep, degree)
    """
    token = request.cookies.get('token')
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"]) if token else None
        user_id = payload.get('user_id') if payload else None
    except Exception:
        return jsonify({"success": False, "message": "無效的 token"}), 401

    if not user_id:
        return jsonify({"success": False, "message": "未取得 user_id"}), 401

    data = request.get_json() or {}
    choices = data.get('choices')
    if not choices or not isinstance(choices, list):
        return jsonify({"success": False, "message": "請提供 choices 陣列"}), 400

    try:
        with engine.begin() as conn:
            # 先刪掉該使用者的所有舊志願，確保更新是原子性的
            conn.execute(
                text("DELETE FROM user_choices WHERE user_id = :user_id"),
                {"user_id": user_id}
            )
            now = datetime.datetime.now(datetime.timezone.utc)
            # 新增所有新的志願
            for i, c in enumerate(choices, start=1):  # rank 從 1 開始
                sel = c.get('selection')
                degree = c.get('degree')
                if not sel or not degree:
                    return jsonify({"success": False, "message": "每筆 choice 必須包含 selection 與 degree"}), 400

                parts = sel.split('/', 1)
                if len(parts) != 2:
                    return jsonify({"success": False, "message": f"無效的 selection 格式: {sel}"}), 400

                school, department = parts[0], parts[1]
                insert_sql = text("""
                    INSERT INTO user_choices (user_id, rank, school, department, degree, created_at)
                    VALUES (:user_id, :rank, :school, :department, :degree, :created_at)
                """)
                conn.execute(insert_sql, {
                    "user_id": user_id,
                    "rank": i,
                    "school": school,
                    "department": department,
                    "degree": degree,
                    "created_at": now
                })

        return jsonify({"success": True, "message": "志願序儲存成功"}), 201

    except Exception as e:
        print(e)
        return jsonify({"success": False, "message": f"儲存失敗: {str(e)}"}), 500


@app.route('/api/get_user_choices', methods=['GET'])
@token_required
def api_get_user_choices():
    """取得使用者已儲存的志願序（前端輸出格式不變）"""
    token = request.cookies.get('token')
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"]) if token else None
        user_id = payload.get('user_id') if payload else None
    except Exception:
        return jsonify({"success": False, "message": "無效的 token"}), 401

    if not user_id:
        return jsonify({"success": False, "message": "未取得 user_id"}), 401

    try:
        with engine.begin() as conn:
            sql = text("""
                SELECT school, department, degree
                FROM user_choices
                WHERE user_id = :user_id
                ORDER BY rank
            """)
            rows = conn.execute(sql, {"user_id": user_id}).mappings().fetchall()

            if not rows:
                return jsonify({"success": True, "choices": []}), 200

            choices = []
            for row in rows:
                choices.append({
                    "selection": f"{row['school']}/{row['department']}",
                    "degree": row['degree']
                })

            return jsonify({"success": True, "choices": choices}), 200

    except Exception as e:
        return jsonify({"success": False, "message": f"取得志願序失敗: {str(e)}"}), 500

# <<<<<<<<<<<<<<< schools / departments / lookup <<<<<<<<<<<<<<< #
if __name__ == "__main__":
    app.run(debug=True)