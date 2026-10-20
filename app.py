import os
import json
import re
import secrets
import time
from functools import wraps
from urllib.parse import urlparse, urljoin
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, flash, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from database import (
    init_db, migrate_db, seed_educational_content,
    get_connection, get_user_by_email, get_user_by_id, create_user,
    get_or_create_topic, get_all_subjects_and_topics, save_generated_questions
)
from twin_engine import (
    get_student_twin_overview,
    get_adaptive_quiz_questions,
    get_initial_assessment_questions,
    update_digital_twin,
    calculate_mastery
)
from ai_service import (
    load_env_file,
    get_ai_wrong_answer_explanation,
    get_personalized_study_roadmap,
    generate_ai_questions
)

# Load environment configuration from .env in project root
load_env_file()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "learntwin-production-secret-key-cse-2026")

# Secure Session Cookie Configuration
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "False").lower() in ("true", "1")
)

# Initialize and migrate DB on startup
with app.app_context():
    init_db()
    migrate_db()
    seed_educational_content()

# In-Memory Rate Limiting for Login Protection
FAILED_LOGINS = {}  # key: ip_or_email -> list of timestamps
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_PERIOD_SECONDS = 300  # 5 minutes

def is_rate_limited(key: str) -> bool:
    now = time.time()
    attempts = [t for t in FAILED_LOGINS.get(key, []) if now - t < LOCKOUT_PERIOD_SECONDS]
    FAILED_LOGINS[key] = attempts
    return len(attempts) >= MAX_FAILED_ATTEMPTS

def record_failed_login(key: str):
    now = time.time()
    attempts = [t for t in FAILED_LOGINS.get(key, []) if now - t < LOCKOUT_PERIOD_SECONDS]
    attempts.append(now)
    FAILED_LOGINS[key] = attempts

def clear_failed_logins(key: str):
    FAILED_LOGINS.pop(key, None)

# CSRF Protection
def generate_csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(32)
    return session['_csrf_token']

app.jinja_env.globals['csrf_token'] = generate_csrf_token

def is_safe_url(target):
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc and not target.startswith('//')

@app.before_request
def csrf_protect():
    if request.method == "POST":
        # Check CSRF token for all state-changing POST requests
        token = request.form.get('csrf_token') or request.headers.get('X-CSRFToken')
        session_token = session.get('_csrf_token')
        if not token or not session_token or not secrets.compare_digest(token, session_token):
            flash("Security check failed (CSRF token missing or expired). Please try again.", "danger")
            # If request is API or expects JSON, return JSON 400
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({"error": "CSRF validation failed"}), 400
            return redirect(request.referrer or url_for('login'))

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please sign in to access this page.", "warning")
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated_function

def get_current_user():
    user_id = session.get('user_id')
    if not user_id:
        return None
    return get_user_by_id(user_id)

@app.context_processor
def inject_user():
    return dict(current_user=get_current_user())

# --- Authentication Routes ---

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET' and 'user_id' in session:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        course = request.form.get('course', '').strip()
        academic_year = request.form.get('academic_year', '').strip()
        learning_goal = request.form.get('learning_goal', '').strip()

        # Validation
        if not name or not email or not password or not confirm_password:
            flash("All required fields must be completed.", "danger")
            return render_template('register.html', form=request.form)

        # Basic email format check
        email_regex = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
        if not re.match(email_regex, email):
            flash("Please enter a valid email address.", "danger")
            return render_template('register.html', form=request.form)

        # Password rules
        if len(password) < 8:
            flash("Password must be at least 8 characters long.", "danger")
            return render_template('register.html', form=request.form)

        if not any(c.isdigit() or not c.isalnum() for c in password):
            flash("Password must contain at least one number or special character.", "danger")
            return render_template('register.html', form=request.form)

        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template('register.html', form=request.form)

        # Check existing user
        existing_user = get_user_by_email(email)
        if existing_user:
            flash("An account with this email address already exists. Please log in.", "danger")
            return render_template('register.html', form=request.form)

        # Hash password and create user strictly with 'student' role
        password_hash = generate_password_hash(password)
        new_user_id = create_user(
            name=name,
            email=email,
            password_hash=password_hash,
            course=course,
            academic_year=academic_year,
            learning_goal=learning_goal,
            role='student'
        )

        # Regenerate session securely
        session.clear()
        session['user_id'] = new_user_id
        session['_csrf_token'] = secrets.token_hex(32)

        flash(f"Welcome to MindPath, {name}! Your student account is active.", "success")
        return redirect(url_for('dashboard'))

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET' and 'user_id' in session:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        client_ip = request.remote_addr or 'unknown'
        rate_key = f"{client_ip}:{email}"

        if is_rate_limited(rate_key):
            flash("Too many failed login attempts. Please wait 5 minutes before trying again.", "danger")
            return render_template('login.html', email=email)

        user = get_user_by_email(email)
        # Generic message on bad credentials
        if not user or not check_password_hash(user['password_hash'], password):
            record_failed_login(rate_key)
            flash("Invalid email or password.", "danger")
            return render_template('login.html', email=email)

        # Successful login: reset rate limit & regenerate session to avoid session fixation
        clear_failed_logins(rate_key)
        user_id = user['id']
        session.clear()
        session['user_id'] = user_id
        session['_csrf_token'] = secrets.token_hex(32)

        flash(f"Welcome back, {user['name']}!", "success")

        next_url = request.args.get('next')
        if next_url and is_safe_url(next_url):
            return redirect(next_url)
        return redirect(url_for('dashboard'))

    return render_template('login.html')

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    flash("You have been signed out safely.", "info")
    return redirect(url_for('login'))

# --- Protected Application Routes ---

@app.route('/')
@app.route('/dashboard')
@login_required
def dashboard():
    user = get_current_user()
    twin_data = get_student_twin_overview(user['id'])

    # Get personalized AI roadmap for weakest topic if available
    weak_topic_name = twin_data['primary_weak']['topic_name'] if twin_data.get('primary_weak') else None
    weak_subject = twin_data['primary_weak']['subject'] if twin_data.get('primary_weak') else None
    weak_accuracy = twin_data['primary_weak']['accuracy_rate'] if twin_data.get('primary_weak') else 0.0

    ai_roadmap = get_personalized_study_roadmap(
        user_name=user['name'],
        weak_topic_name=weak_topic_name,
        weak_subject=weak_subject,
        accuracy=weak_accuracy,
        speed_score=twin_data['speed_score']
    )

    # Data structure for Chart.js
    chart_json = json.dumps({
        'subjects': twin_data['subjects'],
        'topics': twin_data['topics'],
        'is_new_student': twin_data['is_new_student']
    })

    return render_template(
        'dashboard.html',
        current_user=user,
        twin_data=twin_data,
        ai_roadmap=ai_roadmap,
        chart_json=chart_json
    )

@app.route('/assessment', endpoint='assessment')
@login_required
def assessment():
    """Initial Diagnostic Assessment across all subjects to calibrate the Digital Twin."""
    user = get_current_user()
    assessment_token = secrets.token_hex(16)
    session['assessment_token'] = assessment_token
    questions = get_initial_assessment_questions(num_per_topic=1)
    return render_template(
        'assessment.html',
        current_user=user,
        questions=questions,
        assessment_token=assessment_token
    )

@app.route('/assessment/submit', methods=['POST'])
@login_required
def submit_assessment():
    """Handles submission of the multi-topic initial diagnostic assessment with idempotency."""
    user = get_current_user()

    # Prevent accidental double-submission / retry duplication
    submitted_token = request.form.get('assessment_token')
    if submitted_token and session.get('last_submitted_assessment_token') == submitted_token:
        flash("Assessment has already been recorded. Viewing updated dashboard.", "info")
        return redirect(url_for('dashboard'))

    time_taken = int(request.form.get('time_taken_seconds', 60))
    if time_taken <= 0:
        time_taken = 60

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM questions")
        questions_pool = {q['id']: dict(q) for q in cursor.fetchall()}
    finally:
        conn.close()

    submitted_answers = []
    score = 0
    total_q = 0
    review = []

    for key, val in request.form.items():
        if key.startswith('question_'):
            try:
                q_id = int(key.replace('question_', ''))
            except ValueError:
                continue

            if q_id in questions_pool:
                total_q += 1
                q_data = questions_pool[q_id]
                is_correct = (val.upper() == q_data['correct_option'].upper())
                if is_correct:
                    score += 1

                option_map = {
                    'A': q_data['option_a'],
                    'B': q_data['option_b'],
                    'C': q_data['option_c'],
                    'D': q_data['option_d']
                }

                ai_explanation = ""
                if not is_correct:
                    ai_explanation = get_ai_wrong_answer_explanation(
                        question_text=q_data['question_text'],
                        correct_option_text=f"{q_data['correct_option']}: {option_map.get(q_data['correct_option'], '')}",
                        selected_option_text=f"{val.upper()}: {option_map.get(val.upper(), '')}",
                        base_explanation=q_data['explanation']
                    )

                per_q_time = round(time_taken / max(1, total_q), 1)
                submitted_answers.append({
                    'question_id': q_id,
                    'topic_id': q_data['topic_id'],
                    'selected_option': val.upper(),
                    'is_correct': is_correct,
                    'time_spent': per_q_time
                })

                review.append({
                    'question_text': q_data['question_text'],
                    'difficulty': q_data['difficulty'],
                    'selected_option': val.upper(),
                    'selected_text': option_map.get(val.upper(), ''),
                    'correct_option': q_data['correct_option'],
                    'correct_text': option_map.get(q_data['correct_option'], ''),
                    'is_correct': is_correct,
                    'explanation': q_data['explanation'],
                    'ai_explanation': ai_explanation
                })

    # Update student's digital twin atomically across all assessed topics
    twin_update = update_digital_twin(
        user_id=user['id'],
        topic_id=None,  # multi-topic
        score=score,
        total_questions=total_q,
        time_taken_seconds=time_taken,
        answers_detail=submitted_answers,
        quiz_type='assessment'
    )

    if submitted_token:
        session['last_submitted_assessment_token'] = submitted_token

    assessment_topic = {
        'name': 'Initial Diagnostic Assessment',
        'subject': 'CSE Foundation (C, DSA, DBMS)'
    }

    return render_template(
        'quiz_result.html',
        current_user=user,
        topic=assessment_topic,
        score=score,
        total_questions=total_q,
        time_taken_seconds=time_taken,
        twin_update=twin_update,
        review=review,
        is_assessment=True
    )

@app.route('/quizzes')
@login_required
def select_quiz():
    user = get_current_user()
    twin_data = get_student_twin_overview(user['id'])

    # Merge topic info with twin status
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, subject, name, description FROM topics ORDER BY subject COLLATE NOCASE ASC, name COLLATE NOCASE ASC")
        raw_topics = [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()

    topic_dict = {t['topic_id']: t for t in twin_data['topics']}
    
    enriched_topics = []
    for t in raw_topics:
        t_id = t['id']
        twin_info = topic_dict.get(t_id, {})
        enriched_topics.append({
            'id': t_id,
            'subject': t['subject'],
            'name': t['name'],
            'description': t['description'],
            'accuracy_rate': twin_info.get('accuracy_rate', 0.0),
            'avg_time_per_question': twin_info.get('avg_time_per_question', 0.0),
            'mastery_status': twin_info.get('mastery_status', 'UNASSESSED'),
            'total_attempted': twin_info.get('total_attempted', 0)
        })

    return render_template('quiz_select.html', current_user=user, topics=enriched_topics)

@app.route('/quiz/custom', methods=['GET', 'POST'])
@login_required
def custom_quiz():
    user = get_current_user()
    data_suggestions = get_all_subjects_and_topics()

    if request.method == 'GET':
        prefill_subj = request.args.get('subject', '').strip()
        prefill_topic = request.args.get('topic', '').strip()
        prefill_diff = request.args.get('difficulty', 'adaptive').strip().lower()
        prefill_count = request.args.get('count', '3').strip()
        return render_template(
            'quiz_custom.html',
            current_user=user,
            subjects=data_suggestions['subjects'],
            topics=data_suggestions['topics'],
            selected_subject=prefill_subj,
            selected_topic=prefill_topic,
            selected_difficulty=prefill_diff,
            selected_count=prefill_count
        )

    # POST handling
    subject = request.form.get('subject', '').strip()
    topic_name = request.form.get('topic', '').strip()
    difficulty = request.form.get('difficulty', 'adaptive').strip().lower()
    count_raw = request.form.get('count', '3').strip()

    if not subject or len(subject) < 2:
        flash("Please provide a valid subject name (at least 2 characters).", "danger")
        return render_template(
            'quiz_custom.html',
            current_user=user,
            subjects=data_suggestions['subjects'],
            topics=data_suggestions['topics'],
            selected_subject=subject,
            selected_topic=topic_name,
            selected_difficulty=difficulty,
            selected_count=count_raw
        )

    if not topic_name or len(topic_name) < 2:
        flash("Please provide a valid topic name (at least 2 characters).", "danger")
        return render_template(
            'quiz_custom.html',
            current_user=user,
            subjects=data_suggestions['subjects'],
            topics=data_suggestions['topics'],
            selected_subject=subject,
            selected_topic=topic_name,
            selected_difficulty=difficulty,
            selected_count=count_raw
        )

    if difficulty not in ('easy', 'medium', 'hard', 'adaptive'):
        difficulty = 'adaptive'

    try:
        count = int(count_raw)
        if count not in (3, 5, 10):
            count = 3
    except ValueError:
        count = 3

    # Dynamically find or create the topic
    topic_rec = get_or_create_topic(subject, topic_name)
    topic_id = topic_rec['id']

    # Check how many questions currently exist for this topic
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM questions WHERE topic_id = ?", (topic_id,))
        existing_q_count = cursor.fetchone()[0]
    finally:
        conn.close()

    if existing_q_count < count:
        needed_count = count - existing_q_count
        gen_count = max(needed_count, 3)
        ai_res = generate_ai_questions(
            subject=topic_rec['subject'],
            topic=topic_rec['name'],
            difficulty=difficulty,
            count=gen_count,
            return_diagnostics=True
        )

        if isinstance(ai_res, tuple):
            generated_questions, diag = ai_res
        else:
            generated_questions = ai_res
            diag = {}

        if generated_questions:
            save_generated_questions(topic_id, generated_questions)
            flash(f"✨ Successfully generated {len(generated_questions)} AI questions for '{topic_rec['name']}'!", "success")
        else:
            # If generation fails, check if existing questions can be served
            if existing_q_count > 0:
                flash(f"Serving {existing_q_count} existing question(s) for '{topic_rec['name']}' (AI generation unavailable).", "info")
            else:
                err_type = diag.get('error_type') if isinstance(diag, dict) else None
                status_code = diag.get('status_code') if isinstance(diag, dict) else None
                err_msg = diag.get('error_message', '') if isinstance(diag, dict) else ''

                if err_type == 'missing_key':
                    flash(
                        f"⚠️ Google Gemini API key is missing. Please add your GEMINI_API_KEY in the .env file or environment variables to generate questions for new topics.",
                        "danger"
                    )
                elif err_type == 'invalid_key':
                    flash(
                        f"⚠️ Gemini API authentication failed (HTTP {status_code or 400}: {err_msg or 'Invalid API Key'}). Please check your GEMINI_API_KEY in .env.",
                        "danger"
                    )
                elif err_type == 'quota_exceeded':
                    flash(
                        f"⚠️ Gemini API rate limit or quota exceeded (HTTP 429). Please wait a moment before trying again.",
                        "danger"
                    )
                elif err_type == 'model_not_found':
                    flash(
                        f"⚠️ Configured Gemini model not found (HTTP 404). Please verify GEMINI_MODEL in your .env file.",
                        "danger"
                    )
                elif err_type == 'service_unavailable':
                    flash(
                        f"⚠️ Gemini AI service is temporarily experiencing high demand (HTTP 503). Please try again shortly.",
                        "warning"
                    )
                elif err_type in ('network_timeout', 'connection_error'):
                    flash(
                        f"⚠️ Network error connecting to Gemini API: {err_msg}. Please check your internet connection.",
                        "danger"
                    )
                elif err_type in ('malformed_json', 'invalid_schema'):
                    flash(
                        f"⚠️ AI question formatting issue: {err_msg}. Please try generating again.",
                        "warning"
                    )
                else:
                    detail = f" (HTTP {status_code}: {err_msg})" if status_code else (f": {err_msg}" if err_msg else "")
                    flash(
                        f"⚠️ Could not generate questions for new topic '{topic_rec['name']}'{detail}. Please verify that your GEMINI_API_KEY environment variable is configured and valid, or check network connectivity.",
                        "danger"
                    )

                return render_template(
                    'quiz_custom.html',
                    current_user=user,
                    subjects=data_suggestions['subjects'],
                    topics=data_suggestions['topics'],
                    selected_subject=subject,
                    selected_topic=topic_name,
                    selected_difficulty=difficulty,
                    selected_count=count_raw
                )

    return redirect(url_for('take_quiz', topic_id=topic_id, difficulty=difficulty, count=count))

@app.route('/quiz/<int:topic_id>')
@login_required
def take_quiz(topic_id):
    user = get_current_user()
    quiz_token = secrets.token_hex(16)
    session[f'quiz_token_{topic_id}'] = quiz_token

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, subject, name FROM topics WHERE id = ?", (topic_id,))
        topic = cursor.fetchone()

        cursor.execute("SELECT mastery_status FROM student_twin_profile WHERE user_id = ? AND topic_id = ?", (user['id'], topic_id))
        profile = cursor.fetchone()
    finally:
        conn.close()

    if not topic:
        flash("Requested topic not found.", "danger")
        return redirect(url_for('select_quiz'))

    diff_req = request.args.get('difficulty', 'adaptive').strip().lower()
    if diff_req not in ('easy', 'medium', 'hard', 'adaptive'):
        diff_req = 'adaptive'

    count_req = request.args.get('count', '3').strip()
    try:
        count_val = int(count_req)
        if count_val not in (3, 5, 10):
            count_val = 3
    except ValueError:
        count_val = 3

    mastery = profile['mastery_status'] if profile else 'UNASSESSED'
    questions = get_adaptive_quiz_questions(topic_id, user['id'], num_questions=count_val, difficulty=diff_req)

    if not questions:
        flash(f"No questions are currently available for topic '{topic['name']}'. Create custom questions below.", "warning")
        return redirect(url_for('custom_quiz', subject=topic['subject'], topic=topic['name']))

    return render_template(
        'quiz.html',
        current_user=user,
        topic=dict(topic),
        mastery=mastery,
        difficulty_mode=diff_req,
        questions=questions,
        quiz_token=quiz_token
    )

@app.route('/quiz/<int:topic_id>/submit', methods=['POST'])
@login_required
def submit_quiz(topic_id):
    user = get_current_user()

    submitted_token = request.form.get('quiz_token')
    if submitted_token and session.get(f'last_submitted_quiz_token_{topic_id}') == submitted_token:
        flash("Quiz attempt was already recorded. Viewing dashboard.", "info")
        return redirect(url_for('dashboard'))

    time_taken = int(request.form.get('time_taken_seconds', 30))
    if time_taken <= 0:
        time_taken = 30

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, subject, name FROM topics WHERE id = ?", (topic_id,))
        topic_row = cursor.fetchone()
        if not topic_row:
            flash("Invalid topic submission.", "danger")
            return redirect(url_for('select_quiz'))

        topic = dict(topic_row)

        # Get all questions for this topic
        cursor.execute("SELECT * FROM questions WHERE topic_id = ?", (topic_id,))
        questions_pool = {q['id']: dict(q) for q in cursor.fetchall()}
    finally:
        conn.close()

    submitted_answers = []
    score = 0
    total_q = 0
    review = []

    for key, val in request.form.items():
        if key.startswith('question_'):
            try:
                q_id = int(key.replace('question_', ''))
            except ValueError:
                continue

            if q_id in questions_pool:
                total_q += 1
                q_data = questions_pool[q_id]
                is_correct = (val.upper() == q_data['correct_option'].upper())
                if is_correct:
                    score += 1

                option_map = {
                    'A': q_data['option_a'],
                    'B': q_data['option_b'],
                    'C': q_data['option_c'],
                    'D': q_data['option_d']
                }

                ai_explanation = ""
                if not is_correct:
                    ai_explanation = get_ai_wrong_answer_explanation(
                        question_text=q_data['question_text'],
                        correct_option_text=f"{q_data['correct_option']}: {option_map.get(q_data['correct_option'], '')}",
                        selected_option_text=f"{val.upper()}: {option_map.get(val.upper(), '')}",
                        base_explanation=q_data['explanation']
                    )

                per_q_time = round(time_taken / max(1, total_q), 1)
                submitted_answers.append({
                    'question_id': q_id,
                    'selected_option': val.upper(),
                    'is_correct': is_correct,
                    'time_spent': per_q_time
                })

                review.append({
                    'question_text': q_data['question_text'],
                    'difficulty': q_data['difficulty'],
                    'selected_option': val.upper(),
                    'selected_text': option_map.get(val.upper(), ''),
                    'correct_option': q_data['correct_option'],
                    'correct_text': option_map.get(q_data['correct_option'], ''),
                    'is_correct': is_correct,
                    'explanation': q_data['explanation'],
                    'ai_explanation': ai_explanation
                })

    # Update student's digital twin strictly under authenticated student
    twin_update = update_digital_twin(
        user_id=user['id'],
        topic_id=topic_id,
        score=score,
        total_questions=total_q,
        time_taken_seconds=time_taken,
        answers_detail=submitted_answers,
        quiz_type='adaptive'
    )

    return render_template(
        'quiz_result.html',
        current_user=user,
        topic=topic,
        score=score,
        total_questions=total_q,
        time_taken_seconds=time_taken,
        twin_update=twin_update,
        review=review,
        is_assessment=False
    )

# --- Student MindPath API Endpoints (Access Controlled) ---

@app.route('/api/twin')
@app.route('/api/mindpath')
@login_required
def api_twin_self():
    """REST API endpoint returning the authenticated student's MindPath state."""
    user = get_current_user()
    data = get_student_twin_overview(user['id'])
    return jsonify(data)

@app.route('/api/twin/<int:user_id>')
@app.route('/api/mindpath/<int:user_id>')
@login_required
def api_twin(user_id):
    """
    REST API endpoint for a specific user ID.
    Enforces authorization: ordinary students may strictly access only their own ID.
    """
    logged_in_user = get_current_user()
    if logged_in_user['id'] != user_id and logged_in_user.get('role') != 'admin':
        return jsonify({'error': 'Forbidden: You are not authorized to view another student\'s MindPath.'}), 403

    data = get_student_twin_overview(user_id)
    if not data:
        return jsonify({'error': 'Student not found'}), 404
    return jsonify(data)

# Error handlers
@app.errorhandler(403)
def forbidden(e):
    return render_template('base.html', error_title="403 Forbidden", error_message="You do not have permission to view this resource."), 403

@app.errorhandler(404)
def not_found(e):
    return render_template('base.html', error_title="404 Not Found", error_message="The requested page could not be found."), 404

if __name__ == '__main__':
    print("🚀 Starting MindPath on http://127.0.0.1:5000")
    app.run(debug=True, port=5000)
