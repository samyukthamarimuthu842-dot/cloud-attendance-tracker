from flask import Flask, render_template, request, redirect, session
import cv2
import numpy as np
import base64
from PIL import Image
import io
import pymongo
from datetime import datetime
from geopy.distance import geodesic
import bcrypt
import yagmail
import os

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallbacksecretkey")

# ==============================
# DATABASE
# ==============================

mongo_uri = os.getenv("MONGO_URI")

if not mongo_uri:
    raise ValueError("MONGO_URI environment variable not set")

client = pymongo.MongoClient(mongo_uri)
db = client["attendance_db"]

students = db["students"]
attendance = db["attendance"]
admins = db["admins"]

# ==============================
# FACE CASCADE
# ==============================

cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
face_cascade = cv2.CascadeClassifier(cascade_path)

if face_cascade.empty():
    raise Exception("Failed to load Haar Cascade")

# ==============================
# COLLEGE LOCATION
# ==============================

COLLEGE_LOCATION = (8.5241, 76.9366)
ALLOWED_RADIUS = 50

# ==============================
# EMAIL WARNING FUNCTION
# ==============================

def check_shortage(student):

    total = attendance.count_documents({"rollno": student["rollno"]})

    present = attendance.count_documents({
        "rollno": student["rollno"],
        "status": "Present"
    })

    if total > 0:

        percent = (present / total) * 100

        if percent < 75:

            email_user = os.getenv("EMAIL_USER")
            email_pass = os.getenv("EMAIL_PASS")

            if not email_user or not email_pass:
                print("Email credentials not set")
                return

            try:
                yag = yagmail.SMTP(email_user, email_pass)

                yag.send(
                    to=student["parent_email"],
                    subject="Attendance Shortage Alert",
                    contents=f"""
Your ward {student['name']} attendance is below 75%.
Current Attendance: {percent:.2f}%
"""
                )

            except Exception as e:
                print("Email failed:", e)

# ==============================
# HOME
# ==============================

@app.route("/")
def index():
    return render_template("index.html")

# ==============================
# ADMIN LOGIN PAGE
# ==============================

@app.route("/admin_login_page")
def admin_login_page():
    return render_template("admin_login.html")

# ==============================
# ADMIN LOGIN
# ==============================

@app.route("/admin_login", methods=["POST"])
def admin_login():

    username = request.form["username"]
    password = request.form["password"]

    if username == "admin" and password == "admin123":

        session["admin"] = username
        return redirect("/admin_dashboard")

    return "Invalid Admin Login"

# ==============================
# ADMIN DASHBOARD
# ==============================

@app.route("/admin_dashboard")
def admin_dashboard():

    if "admin" not in session:
        return redirect("/admin_login_page")

    records = attendance.find()

    return render_template("admin_dashboard.html", records=records)

# ==============================
# ADMIN LOGOUT
# ==============================

@app.route("/admin_logout")
def admin_logout():

    session.pop("admin", None)

    return redirect("/admin_login_page")

# ==============================
# STUDENT REGISTER PAGE
# ==============================

@app.route("/student_register_page")
def student_register_page():

    return render_template("student_register.html")

# ==============================
# STUDENT REGISTER
# ==============================

@app.route("/register", methods=["POST"])
def register():

    name = request.form["name"]
    rollno = request.form["rollno"]
    parent_email = request.form["parent_email"]
    password = request.form["password"]
    image_data = request.form["image"]

    if students.find_one({"rollno": rollno}):
        return "Already Registered"

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())

    image_data = image_data.split(",")[1]
    image_bytes = base64.b64decode(image_data)

    image = Image.open(io.BytesIO(image_bytes))
    image_np = np.array(image)

    gray = cv2.cvtColor(image_np, cv2.COLOR_BGR2GRAY)

    faces = face_cascade.detectMultiScale(gray, 1.3, 5)

    if len(faces) == 0:
        return "No Face Detected"

    (x, y, w, h) = faces[0]

    face = gray[y:y+h, x:x+w]
    face = cv2.resize(face, (200, 200))

    students.insert_one({
        "name": name,
        "rollno": rollno,
        "parent_email": parent_email,
        "password": hashed,
        "face": face.tolist()
    })

    return redirect("/student_login_page")

# ==============================
# STUDENT LOGIN PAGE
# ==============================

@app.route("/student_login_page")
def student_login_page():

    return render_template("student_login.html")

# ==============================
# STUDENT LOGIN
# ==============================

@app.route("/student_login", methods=["POST"])
def student_login():

    rollno = request.form["rollno"]
    password = request.form["password"]

    student = students.find_one({"rollno": rollno})

    if student and bcrypt.checkpw(password.encode(), student["password"]):

        session["student"] = rollno
        return redirect("/student_dashboard")

    return "Invalid Login"

# ==============================
# STUDENT DASHBOARD
# ==============================

@app.route("/student_dashboard")
def student_dashboard():

    if "student" not in session:
        return redirect("/student_login_page")

    return render_template("student_dashboard.html")

# ==============================
# STUDENT REPORT
# ==============================

@app.route("/student_report")
def student_report():

    if "student" not in session:
        return redirect("/student_login_page")

    rollno = session["student"]

    records = list(attendance.find({"rollno": rollno}))

    return render_template("student_report.html", records=records)

# ==============================
# MARK ATTENDANCE
# ==============================

@app.route("/mark_attendance", methods=["GET","POST"])
def mark_attendance():

    if "student" not in session:
        return redirect("/student_login_page")

    rollno = session["student"]

    subject = request.form["subject"]

    latitude = float(request.form["latitude"])
    longitude = float(request.form["longitude"])

    image_data = request.form["image"]

    student = students.find_one({"rollno": rollno})

    if not student:
        return "Student not found"

    image_data = image_data.split(",")[1]

    image_bytes = base64.b64decode(image_data)

    image = Image.open(io.BytesIO(image_bytes))

    image_np = np.array(image)

    gray = cv2.cvtColor(image_np, cv2.COLOR_BGR2GRAY)

    faces = face_cascade.detectMultiScale(gray, 1.3, 5)

    status = "Absent"

    if len(faces) > 0:

        (x, y, w, h) = faces[0]

        face = gray[y:y+h, x:x+w]

        face = cv2.resize(face, (200, 200))

        saved_face = np.array(student["face"], dtype=np.uint8)

        diff = np.mean(cv2.absdiff(face, saved_face))

        distance = geodesic(
            COLLEGE_LOCATION,
            (latitude, longitude)
        ).meters

        if diff < 50 and distance <= ALLOWED_RADIUS:
            status = "Present"

    attendance.insert_one({

        "rollno": rollno,
        "subject": subject,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "status": status

    })

    check_shortage(student)

    return redirect("/student_dashboard")

# ==============================
# LOGOUT
# ==============================

@app.route("/logout")
def logout():

    session.clear()

    return redirect("/")

# ==============================
# RUN SERVER
# ==============================

if __name__ == "__main__":

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))