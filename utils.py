def send_mail(email_address, mail_type, necessary_content):
    import os
    import smtplib
    from email.mime.text import MIMEText

    # Gmail SMTP 設定
    SMTP_SERVER = "smtp.gmail.com"
    SMTP_PORT = 587
    SMTP_LOGIN = "tw.graduated.friends@gmail.com"
    SMTP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")  # 你剛建立的應用程式密碼

    if mail_type == "captcha":
        subject = "臺灣研究所透明平台 註冊驗證碼"
        body = f"您好，您的驗證碼為：{necessary_content}，請在五分鐘內使用。"

        msg = MIMEText(body, "html", "utf-8")
        msg["From"] = SMTP_LOGIN
        msg["To"] = email_address
        msg["Subject"] = subject

        try:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_LOGIN, SMTP_PASSWORD)
                server.sendmail(SMTP_LOGIN, email_address, msg.as_string())
            return True
        except Exception as e:
            print("寄信失敗:", e)
            return False
        


def verify_token(SECRET_KEY):
    from flask import request
    import jwt
    token = request.cookies.get('token')
    if not token:
        return None
    try:
        decoded = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return decoded["email"]
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None