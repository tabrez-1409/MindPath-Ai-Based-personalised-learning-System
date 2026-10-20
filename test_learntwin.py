import os
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import sqlite3
from urllib.parse import urlparse

# Set test database path BEFORE importing database or app
test_db_fd, test_db_path = tempfile.mkstemp(suffix='.db')
os.close(test_db_fd)
os.environ['LEARNTWIN_DB_PATH'] = test_db_path

import database
from app import app
from twin_engine import (
    get_student_twin_overview,
    get_adaptive_quiz_questions,
    get_initial_assessment_questions,
    calculate_mastery
)
import ai_service

class LearnTwinAuthAndTwinTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False

    def setUp(self):
        self.app = app
        self.client = self.app.test_client()
        # Initialize fresh schema & educational content in isolated test DB
        if os.path.exists(test_db_path):
            os.remove(test_db_path)
        database.init_db(test_db_path)
        database.seed_educational_content(test_db_path)

    def tearDown(self):
        pass

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(test_db_path):
            try:
                os.remove(test_db_path)
            except Exception:
                pass

    def _post(self, url, data=None, follow_redirects=False):
        """Helper to inject session CSRF token into form post."""
        with self.client.session_transaction() as sess:
            if '_csrf_token' not in sess:
                sess['_csrf_token'] = 'test-token-12345'
            token = sess['_csrf_token']
        post_data = dict(data or {})
        post_data['csrf_token'] = token
        return self.client.post(url, data=post_data, follow_redirects=follow_redirects)

    def _register_student(self, name="Kavya Iyer", email="kavya@test.edu", password="Password123#"):
        return self._post('/register', data={
            'name': name,
            'email': email,
            'password': password,
            'confirm_password': password,
            'course': 'B.Tech CSE',
            'academic_year': '3rd Year',
            'learning_goal': 'Prepare for DBMS and DSA exams'
        }, follow_redirects=False)

    # 1. A new student can register successfully
    def test_01_student_registration_success(self):
        res = self._register_student("Arjun Rao", "arjun@test.edu", "SecretPass123!")
        self.assertEqual(res.status_code, 302)
        self.assertIn('/dashboard', res.headers.get('Location', ''))
        
        user = database.get_user_by_email("arjun@test.edu")
        self.assertIsNotNone(user)
        self.assertEqual(user['name'], "Arjun Rao")
        self.assertEqual(user['role'], 'student')

    # 2. Duplicate email registration is rejected
    def test_02_duplicate_email_rejected(self):
        self._register_student("Student One", "same@test.edu", "SecretPass123!")
        # Try registering same email again
        res = self._post('/register', data={
            'name': "Student Two",
            'email': "same@test.edu",
            'password': "SecretPass123!",
            'confirm_password': "SecretPass123!",
            'course': 'BCA',
            'academic_year': '1st Year'
        })
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"An account with this email address already exists", res.data)

    # 3. Invalid registration data is rejected
    def test_03_invalid_registration_data_rejected(self):
        # Mismatched passwords
        res1 = self._post('/register', data={
            'name': "Bad User",
            'email': "bad1@test.edu",
            'password': "Password123!",
            'confirm_password': "MismatchPass456!",
        })
        self.assertEqual(res1.status_code, 200)
        self.assertIn(b"Passwords do not match", res1.data)

        # Password too short (<8 chars)
        res2 = self._post('/register', data={
            'name': "Bad User",
            'email': "bad2@test.edu",
            'password': "short1!",
            'confirm_password': "short1!",
        })
        self.assertEqual(res2.status_code, 200)
        self.assertIn(b"at least 8 characters", res2.data)

    # 4. Passwords are stored as hashes, not plaintext
    def test_04_passwords_stored_as_secure_hashes(self):
        plain_password = "SecretPassword99!"
        self._register_student("Security Test", "security@test.edu", plain_password)
        
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT password_hash FROM users WHERE email = 'security@test.edu'")
        stored_hash = cursor.fetchone()[0]
        conn.close()

        self.assertNotEqual(stored_hash, plain_password)
        self.assertTrue(stored_hash.startswith("scrypt:") or stored_hash.startswith("pbkdf2:"))

    # 5. A registered student can log in
    def test_05_registered_student_can_login(self):
        self._register_student("Login Test", "logintest@test.edu", "MySecretPass123#")
        # Clear session
        with self.client.session_transaction() as sess:
            sess.clear()

        res = self._post('/login', data={
            'email': "logintest@test.edu",
            'password': "MySecretPass123#"
        })
        self.assertEqual(res.status_code, 302)
        self.assertIn('/dashboard', res.headers.get('Location', ''))

    # 6. Invalid login credentials are rejected
    def test_06_invalid_login_credentials_rejected(self):
        self._register_student("Valid User", "valid@test.edu", "CorrectPass123#")
        with self.client.session_transaction() as sess:
            sess.clear()

        res = self._post('/login', data={
            'email': "valid@test.edu",
            'password': "WrongPassword999!"
        })
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Invalid email or password", res.data)

    # 7. Logout clears authentication
    def test_07_logout_clears_authentication(self):
        self._register_student("Logout User", "logout@test.edu", "SecretPass123#")
        
        # Logout via POST with CSRF
        res = self._post('/logout')
        self.assertEqual(res.status_code, 302)
        self.assertIn('/login', res.headers.get('Location', ''))

        with self.client.session_transaction() as sess:
            self.assertNotIn('user_id', sess)

    # 8. Unauthenticated users cannot access protected pages
    def test_08_unauthenticated_access_redirects_to_login(self):
        with self.client.session_transaction() as sess:
            sess.clear()

        protected_urls = ['/dashboard', '/quizzes', '/quiz/1', '/assessment']
        for url in protected_urls:
            res = self.client.get(url)
            self.assertEqual(res.status_code, 302, f"Expected redirect on {url}")
            self.assertIn('/login', res.headers.get('Location', ''))

    # 9. Newly registered student starts without fabricated performance history
    def test_09_new_student_zero_fabricated_history(self):
        self._register_student("Fresh User", "fresh@test.edu", "FreshPass123#")
        user = database.get_user_by_email("fresh@test.edu")
        
        twin = get_student_twin_overview(user['id'])
        self.assertIsNotNone(twin)
        self.assertTrue(twin['is_new_student'])
        self.assertEqual(twin['learning_score'], 0.0)
        self.assertEqual(twin['overall_accuracy'], 0.0)
        self.assertIsNone(twin['consistency_score'])
        self.assertIsNone(twin['improvement_rate'])
        self.assertIsNone(twin['primary_weak'])
        self.assertEqual(twin['prediction']['risk_level'], 'PENDING')

    # 10. Students cannot access another student's API data or profile
    def test_10_multi_user_access_control(self):
        # Register Student 1
        self._register_student("Student One", "student1@test.edu", "StudentPass123#")
        user1 = database.get_user_by_email("student1@test.edu")

        # Register Student 2 (session is now Student 2)
        self._register_student("Student Two", "student2@test.edu", "StudentPass123#")
        user2 = database.get_user_by_email("student2@test.edu")

        # Student 2 tries to access Student 1's API data
        res = self.client.get(f"/api/twin/{user1['id']}")
        self.assertEqual(res.status_code, 403)
        self.assertIn(b"Forbidden", res.data)

        # Student 2 can access their own API data
        res_own = self.client.get(f"/api/twin/{user2['id']}")
        self.assertEqual(res_own.status_code, 200)

    # 11. Quiz submission updates only the authenticated student's Digital Twin
    def test_11_quiz_submission_updates_only_authenticated_student(self):
        self._register_student("User Alpha", "alpha@test.edu", "AlphaPass123#")
        user_alpha = database.get_user_by_email("alpha@test.edu")

        self._register_student("User Beta", "beta@test.edu", "BetaPass123#")
        user_beta = database.get_user_by_email("beta@test.edu")

        # Submit quiz as User Beta
        post_data = {
            'time_taken_seconds': '40',
            'question_1': 'B',  # correct for Q1 (& operator)
            'question_2': 'C',  # correct for Q2 (calloc)
        }
        res = self._post('/quiz/1/submit', data=post_data)
        self.assertEqual(res.status_code, 200)

        # Check Beta's twin is updated
        twin_beta = get_student_twin_overview(user_beta['id'])
        self.assertFalse(twin_beta['is_new_student'])
        self.assertEqual(twin_beta['overall_accuracy'], 100.0)

        # Check Alpha's twin remained untouched
        twin_alpha = get_student_twin_overview(user_alpha['id'])
        self.assertTrue(twin_alpha['is_new_student'])
        self.assertEqual(twin_alpha['overall_accuracy'], 0.0)

    # 12. The initial assessment works
    def test_12_initial_assessment_flow(self):
        self._register_student("Assessment Tester", "assessment@test.edu", "AssessPass123#")
        user = database.get_user_by_email("assessment@test.edu")

        # GET assessment page
        res = self.client.get('/assessment')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Initial Cognitive Assessment", res.data)

        # Submit assessment with 6 answers
        submit_data = {
            'time_taken_seconds': '120',
            'question_1': 'B',  # Topic 1 Correct
            'question_4': 'B',  # Topic 2 Correct
            'question_7': 'A',  # Topic 3 Correct
            'question_10': 'B', # Topic 4 Correct
            'question_13': 'C', # Topic 5 Correct
            'question_16': 'A'  # Topic 6 Incorrect (B is correct)
        }
        submit_res = self._post('/assessment/submit', data=submit_data)
        self.assertEqual(submit_res.status_code, 200)
        self.assertIn(b"Diagnostic Assessment Completed", submit_res.data)

        # Verify direct DB table persistence for attempt, answers, and profiles
        conn = sqlite3.connect(test_db_path, timeout=30.0)
        cursor = conn.cursor()
        cursor.execute("SELECT id, score, total_questions, quiz_type FROM quiz_attempts WHERE user_id = ?", (user['id'],))
        attempt_row = cursor.fetchone()
        self.assertIsNotNone(attempt_row)
        attempt_id = attempt_row[0]
        self.assertEqual(attempt_row[1], 5)  # 5 correct out of 6
        self.assertEqual(attempt_row[2], 6)
        self.assertEqual(attempt_row[3], 'assessment')

        cursor.execute("SELECT COUNT(*) FROM quiz_answers WHERE attempt_id = ?", (attempt_id,))
        answers_count = cursor.fetchone()[0]
        self.assertEqual(answers_count, 6)

        cursor.execute("SELECT COUNT(*) FROM student_twin_profile WHERE user_id = ?", (user['id'],))
        profiles_count = cursor.fetchone()[0]
        self.assertEqual(profiles_count, 6)  # all 6 topics calibrated
        conn.close()

        # Verify twin overview was updated across assessed topics
        twin = get_student_twin_overview(user['id'])
        self.assertFalse(twin['is_new_student'])
        self.assertGreater(twin['overall_accuracy'], 0)
        self.assertEqual(twin['total_attempts_count'], 1)

    # 17. Regression: Assessment persistence, token deduplication & no database lock
    def test_17_assessment_persistence_and_deduplication_regression(self):
        self._register_student("Dedupe Tester", "dedupe@test.edu", "DedupePass123#")
        user = database.get_user_by_email("dedupe@test.edu")

        # Submit first time with token
        token = "test-token-unique-xyz"
        submit_data = {
            'assessment_token': token,
            'time_taken_seconds': '90',
            'question_1': 'B',
            'question_4': 'B',
            'question_7': 'A',
        }
        res1 = self._post('/assessment/submit', data=submit_data)
        self.assertEqual(res1.status_code, 200)

        # Immediate retry/double-submit with the exact same token
        res2 = self._post('/assessment/submit', data=submit_data)
        # Should redirect to dashboard and avoid duplicate attempt creation
        self.assertEqual(res2.status_code, 302)
        self.assertIn('/dashboard', res2.headers.get('Location', ''))

        # Verify database has strictly 1 attempt record for this user
        conn = sqlite3.connect(test_db_path, timeout=30.0)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM quiz_attempts WHERE user_id = ?", (user['id'],))
        attempts_count = cursor.fetchone()[0]
        conn.close()
        self.assertEqual(attempts_count, 1)

    # 13. The existing adaptive quiz flow still works
    def test_13_adaptive_quiz_flow(self):
        self._register_student("Adaptive Tester", "adaptive@test.edu", "AdaptPass123#")
        res = self.client.get('/quiz/1')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Pointers", res.data)

        questions = get_adaptive_quiz_questions(topic_id=1, user_id=1, num_questions=3)
        self.assertGreaterEqual(len(questions), 1)

    # 14. Dashboard charts render without errors when there is no quiz history
    def test_14_dashboard_renders_cleanly_with_zero_history(self):
        self._register_student("Dashboard Zero", "zero@test.edu", "ZeroPass123#")
        res = self.client.get('/dashboard')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"MindPath Setup", res.data)
        self.assertIn(b"Start Initial Assessment", res.data)
        self.assertIn(b"is_new_student", res.data)

    # 15. The application works without a Gemini API key
    def test_15_application_works_without_gemini_api_key(self):
        # Ensure key is empty
        old_key = os.environ.get('GEMINI_API_KEY')
        os.environ['GEMINI_API_KEY'] = ''
        try:
            explanation = ai_service.get_ai_wrong_answer_explanation(
                question_text="What does & do?",
                correct_option_text="B: Memory address",
                selected_option_text="A: Value",
                base_explanation="Address of operator"
            )
            self.assertIn("Misconception Note", explanation)

            roadmap = ai_service.get_personalized_study_roadmap(
                user_name="Student",
                weak_topic_name="Normalization",
                weak_subject="DBMS",
                accuracy=40.0,
                speed_score=50.0
            )
            self.assertIn("Targeted Core Revision", roadmap)
        finally:
            if old_key:
                os.environ['GEMINI_API_KEY'] = old_key

    # 16. Existing shared questions and topics remain available
    def test_16_shared_topics_and_questions_remain_available(self):
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM topics")
        topics_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM questions")
        questions_count = cursor.fetchone()[0]
        conn.close()

        self.assertEqual(topics_count, 6)
        self.assertEqual(questions_count, 18)

    # 17. get_or_create_topic normalization and unique constraint
    def test_17_get_or_create_topic_normalization(self):
        t1 = database.get_or_create_topic("Operating Systems", "Deadlocks", db_path=test_db_path)
        self.assertIsNotNone(t1['id'])
        self.assertEqual(t1['subject'], "Operating Systems")
        self.assertEqual(t1['name'], "Deadlocks")

        # Duplicate with different casing and whitespace should return the exact same record
        t2 = database.get_or_create_topic("  operating systems  ", " deadlocks ", db_path=test_db_path)
        self.assertEqual(t1['id'], t2['id'])

        # Listing subjects and topics should include new topic
        data = database.get_all_subjects_and_topics(db_path=test_db_path)
        self.assertIn("Operating Systems", data['subjects'])
        topic_names = [t['name'] for t in data['topics']]
        self.assertIn("Deadlocks", topic_names)

    # 18. AI question schema validation and code fence stripping
    def test_18_ai_question_schema_validation(self):
        # 1. Clean fenced JSON
        fenced = "```json\n[{\"test\": 1}]\n```"
        self.assertEqual(ai_service.clean_json_response(fenced), '[{"test": 1}]')

        # 2. Valid question
        valid_q = {
            "question_text": "What is thrashing in OS?",
            "option_a": "High paging activity with low CPU utilization",
            "option_b": "CPU overheating",
            "option_c": "Hard drive disk head collision",
            "option_d": "RAM memory bus failure",
            "correct_option": "A",
            "difficulty": "medium",
            "explanation": "Thrashing occurs when the operating system spends more time paging than executing instructions."
        }
        res = ai_service.validate_question_dict(valid_q)
        self.assertIsNotNone(res)
        self.assertEqual(res['correct_option'], 'A')
        self.assertEqual(res['difficulty'], 'medium')

        # 3. Invalid: duplicate options
        invalid_dup = dict(valid_q)
        invalid_dup['option_b'] = invalid_dup['option_a']
        self.assertIsNone(ai_service.validate_question_dict(invalid_dup))

        # 4. Invalid: missing question_text
        invalid_empty = dict(valid_q)
        invalid_empty['question_text'] = ""
        self.assertIsNone(ai_service.validate_question_dict(invalid_empty))

        # 5. Invalid: wrong correct_option letter
        invalid_letter = dict(valid_q)
        invalid_letter['correct_option'] = "Z"
        self.assertIsNone(ai_service.validate_question_dict(invalid_letter))

    # 19. Custom Quiz creation page GET requires authentication
    def test_19_custom_quiz_get(self):
        # Unauthenticated redirects to login
        res = self.client.get('/quiz/custom')
        self.assertEqual(res.status_code, 302)
        self.assertIn('/login', res.headers.get('Location', ''))

        # Authenticated student can view form
        self._register_student(name="Ananya Roy", email="ananya@test.edu")
        res = self.client.get('/quiz/custom')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Create Custom Quiz", res.data)
        self.assertIn(b"subject-suggestions", res.data)

    # 20. Dynamic AI question generation and database saving for unseen topic
    def test_20_custom_quiz_dynamic_generation_and_saving(self):
        self._register_student(name="Vikram Joshi", email="vikram@test.edu")

        mock_generated_questions = [
            {
                "question_text": "Which condition is NOT necessary for a deadlock to occur (Coffman conditions)?",
                "option_a": "Mutual Exclusion",
                "option_b": "Hold and Wait",
                "option_c": "Preemption allowed",
                "option_d": "Circular Wait",
                "correct_option": "C",
                "difficulty": "medium",
                "explanation": "No preemption is the necessary condition. If preemption is allowed, deadlocks can be broken."
            },
            {
                "question_text": "Banker's Algorithm is primarily used for which OS strategy?",
                "option_a": "Deadlock Avoidance",
                "option_b": "Deadlock Detection",
                "option_c": "Deadlock Prevention",
                "option_d": "Deadlock Recovery",
                "correct_option": "A",
                "difficulty": "medium",
                "explanation": "Banker's algorithm tests for safety before granting resource requests to avoid entering an unsafe state."
            },
            {
                "question_text": "What graph algorithm can detect deadlock in a single-instance resource system?",
                "option_a": "Breadth-First Search for shortest path",
                "option_b": "Cycle detection in Resource Allocation Graph",
                "option_c": "Dijkstra's shortest path",
                "option_d": "Kruskal's Minimum Spanning Tree",
                "correct_option": "B",
                "difficulty": "hard",
                "explanation": "In a single-instance resource allocation graph, a cycle is necessary and sufficient condition for a deadlock."
            }
        ]

        with patch('app.generate_ai_questions', return_value=mock_generated_questions):
            res = self._post('/quiz/custom', data={
                'subject': 'Operating Systems',
                'topic': 'Deadlock Handling',
                'difficulty': 'medium',
                'count': '3'
            }, follow_redirects=False)

            self.assertEqual(res.status_code, 302)
            redirect_url = res.headers.get('Location', '')
            self.assertIn('/quiz/', redirect_url)
            self.assertIn('difficulty=medium', redirect_url)

            # Verify questions were saved in the test database under the new topic
            conn = sqlite3.connect(test_db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM topics WHERE name = 'Deadlock Handling'")
            topic_row = cursor.fetchone()
            self.assertIsNotNone(topic_row)
            topic_id = topic_row[0]

            cursor.execute("SELECT COUNT(*) FROM questions WHERE topic_id = ?", (topic_id,))
            q_count = cursor.fetchone()[0]
            conn.close()
            self.assertEqual(q_count, 3)

            # Follow redirect to take quiz and verify question rendering
            quiz_res = self.client.get(redirect_url)
            self.assertEqual(quiz_res.status_code, 200)
            self.assertIn(b"Deadlock Handling", quiz_res.data)
            self.assertIn(b"Banker", quiz_res.data)

    # 21. Honest error handling when API key is missing on unseen topic (Zero Unrelated Substitution)
    def test_21_unseen_topic_zero_unrelated_substitution(self):
        self._register_student(name="Farhan Akhtar", email="farhan@test.edu")

        # Mock generate_ai_questions returning None (simulating missing API key or offline failure)
        with patch('app.generate_ai_questions', return_value=None):
            res = self._post('/quiz/custom', data={
                'subject': 'Quantum Computing',
                'topic': 'Shor Algorithm',
                'difficulty': 'hard',
                'count': '3'
            }, follow_redirects=True)

            self.assertEqual(res.status_code, 200)
            # Must show helpful error instructing user about GEMINI_API_KEY
            self.assertIn(b"GEMINI_API_KEY", res.data)
            self.assertIn(b"Create Custom Quiz", res.data)
            # Must NOT silently substitute unrelated questions or start a quiz
            self.assertNotIn(b"Question 1 of", res.data)
            self.assertNotIn(b"unary operator", res.data)
            self.assertNotIn(b"Floyd", res.data)

    # 22. Multi-user isolation on custom topic quiz attempts
    def test_22_custom_topic_student_isolation(self):
        # Register Student 1
        self._register_student(name="Student One", email="student1@test.edu")
        topic = database.get_or_create_topic("Compiler Design", "Lexical Analysis", db_path=test_db_path)
        database.save_generated_questions(topic['id'], [
            {
                "question_text": "What tool generates lexical analyzers?",
                "option_a": "Lex/Flex",
                "option_b": "Yacc/Bison",
                "option_c": "LLVM",
                "option_d": "GCC",
                "correct_option": "A",
                "difficulty": "easy",
                "explanation": "Lex generates scanners."
            }
        ], db_path=test_db_path)

        # Student 1 takes quiz
        quiz_res = self.client.get(f"/quiz/{topic['id']}")
        with self.client.session_transaction() as sess:
            quiz_token = sess.get(f"quiz_token_{topic['id']}")

        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM questions WHERE topic_id = ?", (topic['id'],))
        q_id = cursor.fetchone()[0]
        conn.close()

        self._post(f"/quiz/{topic['id']}/submit", data={
            'quiz_token': quiz_token,
            'time_taken_seconds': '15',
            f'question_{q_id}': 'A'
        })

        # Register Student 2
        self.client.post('/logout')
        self._register_student(name="Student Two", email="student2@test.edu")

        # Fetch Student 2 twin overview
        user2 = database.get_user_by_email("student2@test.edu", db_path=test_db_path)
        twin_data2 = get_student_twin_overview(user2['id'])

        # Verify Student 2 has 0 attempts on this custom topic
        topic_profile2 = [t for t in twin_data2['topics'] if t['topic_id'] == topic['id']]
        self.assertEqual(len(topic_profile2), 1)
        self.assertEqual(topic_profile2[0]['total_attempted'], 0)
        self.assertEqual(topic_profile2[0]['mastery_status'], 'UNASSESSED')

    # 23. Custom topic integrated into Twin overview, accuracy, and charts
    def test_23_custom_topic_integrated_into_twin(self):
        self._register_student(name="Priya Sharma", email="priya@test.edu")
        user = database.get_user_by_email("priya@test.edu", db_path=test_db_path)

        topic = database.get_or_create_topic("Software Engineering", "Agile Scrum", db_path=test_db_path)
        database.save_generated_questions(topic['id'], [
            {
                "question_text": "What is the recommended duration of a standard Scrum sprint?",
                "option_a": "1 to 4 weeks",
                "option_b": "6 months",
                "option_c": "1 year",
                "option_d": "1 day only",
                "correct_option": "A",
                "difficulty": "easy",
                "explanation": "Sprints are fixed-length iterations of 1 to 4 weeks."
            }
        ], db_path=test_db_path)

        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM questions WHERE topic_id = ?", (topic['id'],))
        q_id = cursor.fetchone()[0]
        conn.close()

        # Submit quiz
        self.client.get(f"/quiz/{topic['id']}")
        with self.client.session_transaction() as sess:
            quiz_token = sess.get(f"quiz_token_{topic['id']}")

        self._post(f"/quiz/{topic['id']}/submit", data={
            'quiz_token': quiz_token,
            'time_taken_seconds': '20',
            f'question_{q_id}': 'A'
        })

        # Check Twin Overview
        twin = get_student_twin_overview(user['id'])
        self.assertFalse(twin['is_new_student'])
        self.assertEqual(twin['overall_accuracy'], 100.0)

        # Topic profile in twin overview
        scrum_profile = [t for t in twin['topics'] if t['topic_name'] == 'Agile Scrum'][0]
        self.assertEqual(scrum_profile['accuracy_rate'], 100.0)
        self.assertEqual(scrum_profile['mastery_status'], 'STRONG')

        # Check dashboard render
        dash_res = self.client.get('/dashboard')
        self.assertEqual(dash_res.status_code, 200)
        self.assertIn(b"Agile Scrum", dash_res.data)

    # 24. Existing topic questions are reused without redundant AI generation
    def test_24_reuse_existing_questions_without_redundant_generation(self):
        self._register_student(name="Rohan Mehra", email="rohan@test.edu")

        # Pointers & Memory already has 3 questions in database
        with patch('app.generate_ai_questions') as mock_gen:
            res = self._post('/quiz/custom', data={
                'subject': 'C Programming',
                'topic': 'Pointers & Memory',
                'difficulty': 'adaptive',
                'count': '3'
            }, follow_redirects=False)

            self.assertEqual(res.status_code, 302)
            # Verify AI generator was NOT invoked because 3 questions already exist
            mock_gen.assert_not_called()

            redirect_url = res.headers.get('Location', '')
            self.assertIn('/quiz/1', redirect_url)

    # 25. .env file loader parses and populates os.environ correctly
    def test_25_env_file_loader(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_env = os.path.join(tmpdir, '.env')
            with open(test_env, 'w', encoding='utf-8') as f:
                f.write("# Comment line\n")
                f.write("TEST_LEARNTWIN_KEY_123=secret_value_abc\n")
                f.write("TEST_LEARNTWIN_QUOTED=\"quoted_val\"\n")
            
            # Clean up before testing
            os.environ.pop('TEST_LEARNTWIN_KEY_123', None)
            os.environ.pop('TEST_LEARNTWIN_QUOTED', None)

            loaded = ai_service.load_env_file(project_root=tmpdir)
            self.assertTrue(loaded)
            self.assertEqual(os.environ.get('TEST_LEARNTWIN_KEY_123'), 'secret_value_abc')
            self.assertEqual(os.environ.get('TEST_LEARNTWIN_QUOTED'), 'quoted_val')

    # 26. API key sanitization strictly prevents key leakage
    def test_26_api_key_sanitization_strict_no_leak(self):
        fake_secret_key = "AIzaSyFakeSecretKey_9876543210_XYZ"
        raw_error = f"Error 400 occurred: request failed for key={fake_secret_key} with message: invalid key {fake_secret_key}"
        clean_msg = ai_service.sanitize_api_error(raw_error, api_key=fake_secret_key)

        # Guarantees key is never present
        self.assertNotIn(fake_secret_key, clean_msg)
        self.assertIn("[REDACTED_API_KEY]", clean_msg)

        # Verify pattern redaction for unprovided other keys in URLs
        other_error = "GET https://example.com/api?token=SuperSecretToken12345"
        clean_other = ai_service.sanitize_api_error(other_error)
        self.assertNotIn("SuperSecretToken12345", clean_other)
        self.assertIn("key=[REDACTED]", clean_other)

    # 27. Gemini missing key diagnostics
    def test_27_gemini_missing_key_diagnostics(self):
        with patch.object(ai_service, 'get_gemini_api_key', return_value=""):
            questions, diag = ai_service.generate_ai_questions(
                subject="Algorithms",
                topic="Dynamic Programming",
                return_diagnostics=True
            )
            self.assertIsNone(questions)
            self.assertFalse(diag['success'])
            self.assertEqual(diag['error_type'], 'missing_key')
            self.assertIn("not configured", diag['error_message'])

    # 28. Gemini invalid key (HTTP 403 / 400) diagnostics
    def test_28_gemini_invalid_key_403(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.json.return_value = {
            "error": {
                "code": 403,
                "message": "API_KEY_INVALID: The provided API key is invalid.",
                "status": "PERMISSION_DENIED"
            }
        }
        mock_resp.text = "API_KEY_INVALID"

        with patch.object(ai_service, 'get_gemini_api_key', return_value="AIzaFakeKey123"):
            with patch('requests.post', return_value=mock_resp):
                questions, diag = ai_service.generate_ai_questions(
                    subject="Networks",
                    topic="Subnetting",
                    return_diagnostics=True
                )
                self.assertIsNone(questions)
                self.assertFalse(diag['success'])
                self.assertEqual(diag['error_type'], 'invalid_key')
                self.assertEqual(diag['status_code'], 403)
                self.assertNotIn("AIzaFakeKey123", diag['error_message'])

    # 29. Gemini quota exceeded (HTTP 429) diagnostics
    def test_29_gemini_quota_exceeded_429(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.json.return_value = {
            "error": {
                "code": 429,
                "message": "Resource has been exhausted (e.g. check quota).",
                "status": "RESOURCE_EXHAUSTED"
            }
        }
        mock_resp.text = "Resource exhausted"

        with patch.object(ai_service, 'get_gemini_api_key', return_value="AIzaFakeKey123"):
            with patch('requests.post', return_value=mock_resp):
                questions, diag = ai_service.generate_ai_questions(
                    subject="AI",
                    topic="Search",
                    return_diagnostics=True
                )
                self.assertIsNone(questions)
                self.assertFalse(diag['success'])
                self.assertEqual(diag['error_type'], 'quota_exceeded')
                self.assertEqual(diag['status_code'], 429)

    # 30. Gemini model not found (404) and service unavailable (503) diagnostics
    def test_30_gemini_model_not_found_and_503(self):
        mock_404 = MagicMock()
        mock_404.status_code = 404
        mock_404.json.return_value = {"error": {"code": 404, "message": "Model not found."}}
        mock_404.text = "Model not found"

        with patch.object(ai_service, 'get_gemini_api_key', return_value="AIzaFakeKey123"):
            with patch.dict(os.environ, {"GEMINI_MODEL": "non-existent-model-xyz"}):
                with patch('requests.post', return_value=mock_404):
                    questions, diag = ai_service.generate_ai_questions(
                        subject="OS",
                        topic="Threads",
                        return_diagnostics=True
                    )
                    self.assertIsNone(questions)
                    self.assertFalse(diag['success'])
                    self.assertEqual(diag['error_type'], 'model_not_found')
                    self.assertEqual(diag['status_code'], 404)

        mock_503 = MagicMock()
        mock_503.status_code = 503
        mock_503.json.return_value = {"error": {"code": 503, "message": "High demand spike."}}
        mock_503.text = "High demand spike"

        with patch.object(ai_service, 'get_gemini_api_key', return_value="AIzaFakeKey123"):
            with patch.dict(os.environ, {"GEMINI_MODEL": "overloaded-model"}):
                with patch('requests.post', return_value=mock_503):
                    questions, diag = ai_service.generate_ai_questions(
                        subject="OS",
                        topic="Threads",
                        return_diagnostics=True
                    )
                    self.assertIsNone(questions)
                    self.assertEqual(diag['error_type'], 'service_unavailable')
                    self.assertEqual(diag['status_code'], 503)

    # 31. Gemini network timeout and malformed JSON diagnostics
    def test_31_gemini_network_timeout_and_malformed_json(self):
        import requests
        with patch.object(ai_service, 'get_gemini_api_key', return_value="AIzaFakeKey123"):
            with patch('requests.post', side_effect=requests.exceptions.Timeout("Request timed out")):
                questions, diag = ai_service.generate_ai_questions(
                    subject="DB",
                    topic="Indexing",
                    return_diagnostics=True
                )
                self.assertIsNone(questions)
                self.assertEqual(diag['error_type'], 'network_timeout')

        # Malformed JSON
        mock_200_bad = MagicMock()
        mock_200_bad.status_code = 200
        mock_200_bad.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "Sorry, I cannot answer as JSON right now."}]}}]
        }
        with patch.object(ai_service, 'get_gemini_api_key', return_value="AIzaFakeKey123"):
            with patch('requests.post', return_value=mock_200_bad):
                questions, diag = ai_service.generate_ai_questions(
                    subject="DB",
                    topic="Indexing",
                    return_diagnostics=True
                )
                self.assertIsNone(questions)
                self.assertEqual(diag['error_type'], 'malformed_json')

    # 32. Custom Quiz UI flash messaging for distinct error categories
    def test_32_custom_quiz_error_flash_messaging(self):
        self._register_student(name="Ayesha Khan", email="ayesha@test.edu")

        # 1. Missing Key flash
        with patch('app.generate_ai_questions', return_value=(None, {"error_type": "missing_key", "error_message": "Key missing"})):
            res = self._post('/quiz/custom', data={'subject': 'Math', 'topic': 'Graph Theory'}, follow_redirects=True)
            self.assertEqual(res.status_code, 200)
            self.assertIn(b"Google Gemini API key is missing", res.data)

        # 2. Quota exceeded flash
        with patch('app.generate_ai_questions', return_value=(None, {"error_type": "quota_exceeded", "status_code": 429})):
            res = self._post('/quiz/custom', data={'subject': 'Math', 'topic': 'Set Theory'}, follow_redirects=True)
            self.assertEqual(res.status_code, 200)
            self.assertIn(b"rate limit or quota exceeded", res.data)

        # 3. Invalid key flash
        with patch('app.generate_ai_questions', return_value=(None, {"error_type": "invalid_key", "status_code": 403, "error_message": "Invalid Key"})):
            res = self._post('/quiz/custom', data={'subject': 'Math', 'topic': 'Number Theory'}, follow_redirects=True)
            self.assertEqual(res.status_code, 200)
            self.assertIn(b"authentication failed", res.data)

    # 33. Robust JSON Parser, Truncation Repair, and Auto-Recovery
    def test_33_robust_json_parser_and_repair(self):
        # 1. Truncated JSON with unclosed string at token limit (e.g. char 983)
        truncated_json = """[
          {
            "question_text": "What is Round Robin?",
            "option_a": "Time sliced",
            "option_b": "Non-preemptive",
            "option_c": "Batch only",
            "option_d": "Real time",
            "correct_option": "A",
            "difficulty": "easy",
            "explanation": "Round robin uses time slices."
          },
          {
            "question_text": "What is FCFS?",
            "option_a": "First come first served",
            "option_b": "Shortest job",
            "option_c": "Priority",
            "option_d": "Round robin",
            "correct_option": "A",
            "difficulty": "easy",
            "explanation": "FCFS serves proce
        """
        data, err = ai_service.robust_parse_json_questions(truncated_json)
        self.assertIsNotNone(data)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['question_text'], "What is Round Robin?")

        # 2. Control characters / raw newlines inside string
        control_char_json = """[
          {
            "question_text": "Line 1\\nLine 2\\twith tab",
            "option_a": "A",
            "option_b": "B",
            "option_c": "C",
            "option_d": "D",
            "correct_option": "A",
            "difficulty": "medium",
            "explanation": "Valid explanation."
          }
        ]"""
        data2, err2 = ai_service.robust_parse_json_questions(control_char_json)
        self.assertIsNotNone(data2)
        self.assertEqual(len(data2), 1)

        # 3. Auto-recovery during question generation when first attempt is malformed
        mock_bad = MagicMock()
        mock_bad.status_code = 200
        mock_bad.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": truncated_json}]}}]
        }

        with patch.object(ai_service, 'get_gemini_api_key', return_value="AIzaFakeKey123"):
            with patch('requests.post', return_value=mock_bad):
                questions, diag = ai_service.generate_ai_questions(
                    subject="Operating Systems",
                    topic="Process Scheduling",
                    difficulty="easy",
                    count=1,
                    return_diagnostics=True
                )
                self.assertIsNotNone(questions)
                self.assertEqual(len(questions), 1)
                self.assertTrue(diag['success'])

    # 34. Gemini 429 quota error auto-fallback to next available model
    def test_34_gemini_429_auto_fallback_to_next_model(self):
        mock_429 = MagicMock()
        mock_429.status_code = 429
        mock_429.json.return_value = {"error": {"code": 429, "message": "Resource exhausted"}}
        mock_429.text = "Resource exhausted"

        mock_200 = MagicMock()
        mock_200.status_code = 200
        mock_200.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": json.dumps([{
                            "question_text": "What is virtual memory?",
                            "option_a": "Storage allocation scheme",
                            "option_b": "CPU cache",
                            "option_c": "Hard drive sector",
                            "option_d": "RAM chip",
                            "correct_option": "A",
                            "difficulty": "easy",
                            "explanation": "Virtual memory maps virtual to physical addresses."
                        }])
                    }]
                }
            }]
        }

        with patch.object(ai_service, 'get_gemini_api_key', return_value="AIzaFakeKey123"):
            # First model returns 429 quota exceeded, second model returns 200 OK
            with patch('requests.post', side_effect=[mock_429, mock_200]) as mock_post:
                questions, diag = ai_service.generate_ai_questions(
                    subject="Operating Systems",
                    topic="Virtual Memory",
                    difficulty="easy",
                    count=1,
                    return_diagnostics=True
                )
                self.assertIsNotNone(questions)
                self.assertEqual(len(questions), 1)
                self.assertTrue(diag['success'])
                self.assertEqual(diag['status_code'], 200)
                self.assertEqual(diag['model'], "gemini-flash-lite-latest")
                self.assertEqual(mock_post.call_count, 2)

    # 35. Temporary server error (503, 500) and timeout fallbacks
    def test_35_server_error_and_timeout_fallbacks(self):
        mock_503 = MagicMock()
        mock_503.status_code = 503
        mock_503.json.return_value = {"error": {"code": 503, "message": "High demand spike."}}
        mock_503.text = "High demand"

        mock_500 = MagicMock()
        mock_500.status_code = 500
        mock_500.json.return_value = {"error": {"code": 500, "message": "Internal server error."}}
        mock_500.text = "Internal error"

        mock_200 = MagicMock()
        mock_200.status_code = 200
        mock_200.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": json.dumps([{
                            "question_text": "What are semaphores in OS?",
                            "option_a": "Synchronization tool",
                            "option_b": "CPU register",
                            "option_c": "Hard drive cache",
                            "option_d": "Network card",
                            "correct_option": "A",
                            "difficulty": "medium",
                            "explanation": "A semaphore is a synchronization primitive."
                        }])
                    }]
                }
            }]
        }

        with patch.object(ai_service, 'get_gemini_api_key', return_value="AIzaFakeKey123"):
            # Model 1 fails 503, Model 2 fails 500, Model 3 (gemini-3.5-flash) succeeds 200
            with patch('requests.post', side_effect=[mock_503, mock_500, mock_200]) as mock_post:
                questions, diag = ai_service.generate_ai_questions(
                    subject="Operating Systems",
                    topic="Semaphores",
                    difficulty="medium",
                    count=1,
                    return_diagnostics=True
                )
                self.assertIsNotNone(questions)
                self.assertEqual(len(questions), 1)
                self.assertTrue(diag['success'])
                self.assertEqual(diag['status_code'], 200)
                self.assertEqual(diag['model'], "gemini-3.5-flash")
                self.assertEqual(mock_post.call_count, 3)

    # 36. Quiz generation across Adaptive, Easy, Medium, and Hard difficulties
    def test_36_difficulty_modes_adaptive_easy_medium_hard(self):
        def make_mock_200(diff):
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = {
                "candidates": [{
                    "content": {
                        "parts": [{
                            "text": json.dumps([{
                                "question_text": f"Sample {diff} question?",
                                "option_a": "Option 1",
                                "option_b": "Option 2",
                                "option_c": "Option 3",
                                "option_d": "Option 4",
                                "correct_option": "A",
                                "difficulty": diff if diff != "adaptive" else "medium",
                                "explanation": "Explanation here."
                            }])
                        }]
                    }
                }]
            }
            return m

        with patch.object(ai_service, 'get_gemini_api_key', return_value="AIzaFakeKey123"):
            for diff in ("adaptive", "easy", "medium", "hard"):
                with patch('requests.post', return_value=make_mock_200(diff)):
                    questions, diag = ai_service.generate_ai_questions(
                        subject="Databases",
                        topic="ACID Properties",
                        difficulty=diff,
                        count=1,
                        return_diagnostics=True
                    )
                    self.assertIsNotNone(questions)
                    self.assertTrue(diag['success'])
                    if diff in ("easy", "medium", "hard"):
                        self.assertEqual(questions[0]['difficulty'], diff)

    # 37. Zero fake questions when all candidate models fail (honest failure)
    def test_37_zero_fake_questions_when_all_models_fail(self):
        mock_503 = MagicMock()
        mock_503.status_code = 503
        mock_503.json.return_value = {"error": {"code": 503, "message": "High demand."}}
        mock_503.text = "High demand"

        with patch.object(ai_service, 'get_gemini_api_key', return_value="AIzaFakeKey123"):
            with patch('requests.post', return_value=mock_503):
                questions, diag = ai_service.generate_ai_questions(
                    subject="Computer Graphics",
                    topic="Ray Tracing",
                    difficulty="hard",
                    count=3,
                    return_diagnostics=True
                )
                # Must be None, zero fake/fabricated questions
                self.assertIsNone(questions)
                self.assertFalse(diag['success'])
                self.assertEqual(diag['error_type'], 'service_unavailable')
                self.assertEqual(diag['status_code'], 503)

    # 38. Succeeding model logging and diagnostics
    def test_38_succeeding_model_logging_and_diagnostics(self):
        mock_200 = MagicMock()
        mock_200.status_code = 200
        mock_200.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": json.dumps([{
                            "question_text": "What does TCP stand for?",
                            "option_a": "Transmission Control Protocol",
                            "option_b": "Total Communication Path",
                            "option_c": "Terminal Control Packet",
                            "option_d": "Time Calculation Port",
                            "correct_option": "A",
                            "difficulty": "easy",
                            "explanation": "TCP is Transmission Control Protocol."
                        }])
                    }]
                }
            }]
        }

        with patch.object(ai_service, 'get_gemini_api_key', return_value="AIzaFakeKey123"):
            with patch.dict(os.environ, {"GEMINI_MODEL": "gemini-3.5-flash-lite"}):
                with patch('requests.post', return_value=mock_200):
                    questions, diag = ai_service.generate_ai_questions(
                        subject="Networking",
                        topic="TCP/IP",
                        difficulty="easy",
                        count=1,
                        return_diagnostics=True
                    )
                    self.assertIsNotNone(questions)
                    self.assertTrue(diag['success'])
                    self.assertEqual(diag['model'], "gemini-3.5-flash-lite")

if __name__ == '__main__':
    unittest.main()


