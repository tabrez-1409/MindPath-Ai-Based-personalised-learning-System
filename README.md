# LearnTwin 🧠
### AI-Based Digital Twin for Adaptive Student Learning
**A Computer Science & Engineering Platform with Real Multi-User Authentication & Adaptive Cognitive Modeling**

---

## 🌟 What is LearnTwin?
Unlike generic quiz websites where every student sees the same static questions and fake scores, **LearnTwin** creates an authentic **Digital Twin**—a real-time computational model of each student's evolving cognitive performance, retention consistency, and answering latency.

Every student registers their own account, starts with a completely unpolluted zero-state profile, takes a diagnostic **Initial Assessment** to calibrate their cognitive map, and receives topic-tailored adaptive quizzes and AI misconception analysis.

---

## 🎯 Key Features

### 1. 🔐 Secure Real-User Authentication & Multi-Tenancy
- **Real Student Accounts**: Secure registration with name, email, degree/course (B.Tech CSE, BCA, MCA), academic year, and learning goals.
- **Password Security**: Passwords hashed with Werkzeug's secure hashing algorithm (`scrypt` / `pbkdf2`). Plaintext passwords are never stored.
- **Session Protection**: Session fixation prevention, `HttpOnly` and `SameSite='Lax'` cookies, configurable HTTPS secure cookies.
- **CSRF Defense**: Session-bound cryptographic tokens protecting all state-changing `POST` requests.
- **Brute-Force Rate Limiting**: In-memory throttle blocking repeated failed login attempts (max 5 failed attempts per 5-minute window).
- **Strict Data Isolation**: Centralized access control ensures students can only access their own dashboard, quiz submissions, and `/api/twin/<user_id>` data.

### 2. 🧠 Explainable Digital Twin Computational Engine
- **Zero-Fabrication Baseline**: Fresh accounts start with unpolluted metrics (`--` or `0.0%`), not fake scores.
- **Comprehensive Diagnostic Assessment**: 6-question balanced baseline covering C Programming, Data Structures, and DBMS to calibrate initial student competency.
- **Adaptive Item Selection**: Dynamically matches question difficulty to the student's mastery:
  - 🔴 **WEAK (<50%)**: Serves foundational easy questions to rebuild confidence.
  - 🟡 **AVERAGE (50%–75%)**: Balanced easy and medium questions.
  - 🟢 **STRONG (>75%)**: Advanced medium and hard questions.
- **Speed Index**: Calibrated against a 30-second benchmark per question.
- **Cognitive Consistency**: Calculated using standard deviation and score variance across 2+ quiz attempts.
- **Improvement Delta**: Quantifies performance trajectory between earlier and recent attempts (requires 3+ attempts).

### 3. 🤖 AI Misconception Feedback & Personalized Roadmaps
- **Google Gemini API (`gemini-1.5-flash`)**: Dynamically diagnoses *why* a chosen wrong answer was chosen, clearing up conceptual misunderstandings.
- **Offline Heuristic Fallback Engine**: If no API key is configured or network is offline, intelligent rule-based tutoring steps in automatically.
- **Privacy First**: Sensitive student PII (email, passwords) is never transmitted to external AI endpoints.

### 4. ✨ User-Defined Subjects, Topics & Dynamic AI Quiz Generation
- **Free-Form Subjects & Topics**: Students can enter any subject and topic freely (e.g., *Operating Systems: Deadlocks*, *Computer Networks: TCP Handshake*, *Machine Learning: Gradient Descent*). No restrictions to fixed lists.
- **Auto-Suggestions via Datalists**: Suggests existing subjects and topics without restricting new input.
- **Configurable Challenge & Length**: Choose between **Easy**, **Medium**, **Hard**, or **Adaptive (Twin-Tailored)**, and pick question counts (3, 5, or 10 questions).
- **Strict Question Schema Validation**: Generates 4 distinct options, unambiguous single correct answer, difficulty tier, and conceptual explanation.
- **Zero Unrelated Substitution**: If an unseen topic has 0 questions and AI generation is unavailable (e.g., missing API key), the platform provides an honest, clear prompt to configure `GEMINI_API_KEY`, never substituting unrelated topics (e.g., C pointers for an OS quiz).
- **Persistent Curriculum & Twin Assimilation**: User-defined topics are permanently stored with case-insensitive unique indexes (`(LOWER(subject), LOWER(name))`) and immediately incorporated into the student's Digital Twin radar chart, bar chart, and weak topic analysis.

---

## 📐 Digital Twin Computational Formulas (Viva Reference)

For examination and college project viva, the Digital Twin calculations are completely explainable:

1. **Topic & Overall Accuracy**:
   $$\text{Accuracy (\%)} = \left(\frac{\text{Total Correct Answers}}{\text{Total Questions Attempted}}\right) \times 100$$

2. **Cognitive Speed Index** (Benchmark $T_{\text{bench}} = 30\text{s}$):
   $$\text{Speed Score} = \max\left(10, \min\left(100, 100 - (\bar{T}_{\text{resp}} - 15.0) \times 1.5\right)\right)$$

3. **Consistency Score** (Score variance across attempts, $N \ge 2$):
   $$\sigma = \sqrt{\frac{1}{N}\sum_{i=1}^N (P_i - \bar{P})^2}, \quad \text{Consistency} = \max(40, \min(95, 95 - (\sigma \times 1.8)))$$

4. **Improvement Rate** (Historical growth delta, $N \ge 3$):
   $$\Delta_{\text{growth}} = \bar{P}_{\text{recent half}} - \bar{P}_{\text{early half}}$$

5. **Composite Dynamic Twin Score**:
   $$\text{Learning Score} = (0.50 \times \text{Accuracy}) + (0.25 \times \text{Speed}) + (0.25 \times \text{Consistency})$$

---

## 🚀 Installation & Setup

### 1. Prerequisites
- Python 3.10+ installed
- PowerShell, Command Prompt, or Bash terminal

### 2. Set Up Virtual Environment (Recommended)
```bash
# Create virtual environment
python -m venv venv

# Activate on Windows PowerShell:
.\venv\Scripts\Activate.ps1

# Or activate on Command Prompt:
.\venv\Scripts\activate.bat
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables (Optional)
Copy `.env.example` to `.env` or set environment variables in your terminal:
```bash
# Flask Session Key
$env:FLASK_SECRET_KEY="your-random-secret-key"

# Optional Gemini API Key for online AI tutoring:
$env:GEMINI_API_KEY="AIzaSy..."
```
*(Note: If no Gemini API key is provided, LearnTwin automatically uses its built-in fallback engine!)*

### 5. Run the Application
```bash
python app.py
```
Open your browser at:
```
http://127.0.0.1:5000
```

---

## 🧪 Running Automated Tests

LearnTwin includes an automated test suite verifying all 33 security, digital twin, custom quiz, and AI diagnostics requirements using an **isolated temporary database** (leaving the production `learntwin.db` untouched):

```bash
python test_learntwin.py
```

### Tested Scenarios:
1. `test_01`: Student registration and account creation.
2. `test_02`: Duplicate email rejection.
3. `test_03`: Invalid registration data rejection (mismatched/short password).
4. `test_04`: Password hashing verification (no plaintext stored).
5. `test_05`: Successful student login.
6. `test_06`: Invalid credentials rejection with generic error message.
7. `test_07`: CSRF-protected logout clears authentication session.
8. `test_08`: Unauthenticated access redirect for protected pages.
9. `test_09`: Newly registered student starts with zero fabricated history.
10. `test_10`: Multi-user data protection (403 Forbidden on foreign student profiles).
11. `test_11`: Quiz submission updates strictly the authenticated student's profile.
12. `test_12`: Initial assessment flow and baseline establishment.
13. `test_13`: Adaptive difficulty quiz flow.
14. `test_14`: Clean dashboard rendering with zero history.
15. `test_15`: Offline AI heuristic fallback without Gemini API key.
16. `test_16`: Shared educational topics and questions integrity.
17. `test_17`: `get_or_create_topic` normalization, whitespace trimming, and unique index constraint.
18. `test_18`: AI question schema validation (4 distinct options, valid key letter, difficulty tier, fence stripping).
19. `test_19`: Custom Quiz GET endpoint authorization and datalist template rendering.
20. `test_20`: Dynamic AI question generation and database persistence for unseen topics.
21. `test_21`: Honest error handling on unseen topics when API key is missing (zero unrelated topic substitution).
22. `test_22`: Multi-user data isolation across custom topic quiz attempts.
23. `test_23`: Custom topic integration into Digital Twin overview, accuracy score, and dashboard charts.
24. `test_24`: Reusing existing cached questions without redundant AI generation calls.
25. `test_25`: `.env` file loader parses and populates `os.environ` correctly.
26. `test_26`: API key sanitization strictly prevents key leakage in error messages and URLs.
27. `test_27`: Gemini missing key diagnostics (`error_type='missing_key'`).
28. `test_28`: Gemini invalid key (HTTP 403 / 400) diagnostics.
29. `test_29`: Quota exceeded (HTTP 429) diagnostics.
30. `test_30`: Model not found (404) and service unavailable (503) fallback and diagnostics.
31. `test_31`: Network timeout and malformed JSON recovery.
32. `test_32`: Custom Quiz UI flash messaging for distinct error categories.

---

## 🗄️ Database Architecture

The SQLite database (`learntwin.db`) contains:
- `users`: Registered student profiles (`id`, `name`, `email`, `password_hash`, `course`, `academic_year`, `learning_goal`, `role`, `created_at`).
- `topics`: Core computer science curriculum domains (C Pointers, Recursion, Linked Lists, Stacks & Queues, Relational Model, Normalization).
- `questions`: Curated question bank categorized by difficulty (`easy`, `medium`, `hard`) with detailed explanations.
- `quiz_attempts`: Session-level history logs tracking student score, time taken, and `quiz_type` (`assessment` or `adaptive`).
- `quiz_answers`: Question-level student choices, correctness, and time spent.
- `student_twin_profile`: Topic-level continuous cognitive state model per student.

---

## 🔒 Security Architecture & Limitations

- **Session Cookies**: Default to `HttpOnly` and `SameSite='Lax'`. Enable `SESSION_COOKIE_SECURE=True` in production HTTPS deployments.
- **Rate Limiting**: Uses an in-memory lock table suitable for single-process deployments. For multi-node distributed production environments, Redis-backed rate limiting (e.g., Flask-Limiter) would be recommended.
- **CSRF Defense**: Cryptographic session tokens validated on all state-changing POST requests.
- **Authorization**: API endpoints strictly check `session['user_id'] == requested_user_id`.
