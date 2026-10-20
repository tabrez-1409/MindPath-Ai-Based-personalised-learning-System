import sqlite3
from database import get_connection

BENCHMARK_TIME_PER_QUESTION = 30.0  # Standard expected time in seconds

def calculate_mastery(accuracy_rate, total_attempted=1):
    """
    Classifies a topic into UNASSESSED, WEAK, AVERAGE, or STRONG based on real performance.
    Requires at least 1 attempt to classify.
    """
    if total_attempted == 0:
        return 'UNASSESSED'
    if accuracy_rate < 50.0:
        return 'WEAK'
    elif accuracy_rate <= 75.0:
        return 'AVERAGE'
    else:
        return 'STRONG'

def get_student_twin_overview(user_id):
    """
    Returns the comprehensive Digital Twin metrics for the student derived strictly from real learning data:
    - Overall Learning Score (composite)
    - Accuracy, Speed, Consistency, Improvement (without fabrication)
    - Subject-wise and topic-wise mastery
    - Primary weak topic alert (only when evidence exists)
    - Performance prediction & onboarding status
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # Get student info
        cursor.execute("SELECT id, name, email, course, academic_year, learning_goal FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            return None

        # Fetch all topic profiles for the student joined with topic details
        cursor.execute('''
        SELECT 
            t.id AS topic_id,
            t.subject,
            t.name AS topic_name,
            COALESCE(p.total_attempted, 0) AS total_attempted,
            COALESCE(p.total_correct, 0) AS total_correct,
            COALESCE(p.accuracy_rate, 0.0) AS accuracy_rate,
            COALESCE(p.avg_time_per_question, 0.0) AS avg_time_per_question,
            COALESCE(p.mastery_status, 'UNASSESSED') AS mastery_status,
            p.last_updated
        FROM topics t
        LEFT JOIN student_twin_profile p ON t.id = p.topic_id AND p.user_id = ?
        ORDER BY t.subject, t.id
        ''', (user_id,))
        topics_profiles = [dict(row) for row in cursor.fetchall()]

        # Fetch quiz attempts history for consistency and improvement rate
        cursor.execute('''
        SELECT id, topic_id, quiz_type, score, total_questions, time_taken_seconds, created_at
        FROM quiz_attempts
        WHERE user_id = ?
        ORDER BY created_at ASC
        ''', (user_id,))
        attempts = [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()

    total_attempted_all = sum(tp['total_attempted'] for tp in topics_profiles)
    total_correct_all = sum(tp['total_correct'] for tp in topics_profiles)
    is_new_student = (len(attempts) == 0 and total_attempted_all == 0)

    # 1. Overall Accuracy
    overall_accuracy = round((total_correct_all / total_attempted_all * 100), 1) if total_attempted_all > 0 else 0.0

    # 2. Speed Index (0 to 100 scale: calibrated around 30s benchmark)
    valid_times = [tp['avg_time_per_question'] for tp in topics_profiles if tp['avg_time_per_question'] > 0]
    if valid_times:
        avg_speed_time = round(sum(valid_times) / len(valid_times), 1)
        speed_score = round(max(10.0, min(100.0, 100.0 - (avg_speed_time - 15.0) * 1.5)), 1)
    else:
        avg_speed_time = 0.0
        speed_score = 0.0

    # 3. Consistency (variance across quiz attempts - strictly not fabricated)
    consistency_score = None
    if len(attempts) >= 2:
        percentages = [(att['score'] / att['total_questions'] * 100) for att in attempts if att['total_questions'] > 0]
        if len(percentages) >= 2:
            mean_p = sum(percentages) / len(percentages)
            variance = sum((p - mean_p) ** 2 for p in percentages) / len(percentages)
            std_dev = variance ** 0.5
            consistency_score = round(max(40.0, min(95.0, 95.0 - (std_dev * 1.8))), 1)

    # 4. Improvement Rate (Delta between later and earlier attempts - strictly not fabricated)
    improvement_rate = None
    if len(attempts) >= 3:
        half_idx = max(1, len(attempts) // 2)
        first_half = attempts[:half_idx]
        second_half = attempts[half_idx:]
        denom_1 = sum(a['total_questions'] for a in first_half)
        denom_2 = sum(a['total_questions'] for a in second_half)
        if denom_1 > 0 and denom_2 > 0:
            avg_early = (sum(a['score'] for a in first_half) / denom_1) * 100
            avg_late = (sum(a['score'] for a in second_half) / denom_2) * 100
            improvement_rate = round(avg_late - avg_early, 1)

    # 5. Composite Learning Score
    if is_new_student:
        learning_score = 0.0
    elif consistency_score is not None:
        learning_score = round((overall_accuracy * 0.5) + (speed_score * 0.25) + (consistency_score * 0.25), 1)
    else:
        learning_score = round((overall_accuracy * 0.7) + (speed_score * 0.3), 1)

    # 6. Group by Subject for Subject-level Strengths
    subjects_summary = {}
    for tp in topics_profiles:
        subj = tp['subject']
        if subj not in subjects_summary:
            subjects_summary[subj] = {
                'subject': subj,
                'total_attempted': 0,
                'total_correct': 0,
                'accuracy': 0.0,
                'status': 'UNASSESSED',
                'topics': []
            }
        subjects_summary[subj]['total_attempted'] += tp['total_attempted']
        subjects_summary[subj]['total_correct'] += tp['total_correct']
        subjects_summary[subj]['topics'].append(tp)

    for subj, data in subjects_summary.items():
        if data['total_attempted'] > 0:
            data['accuracy'] = round((data['total_correct'] / data['total_attempted']) * 100, 1)
            data['status'] = calculate_mastery(data['accuracy'], data['total_attempted'])
        else:
            data['accuracy'] = 0.0
            data['status'] = 'UNASSESSED'

    # 7. Identify Primary Weak Topic (only from topics with actual attempts)
    active_topics = [tp for tp in topics_profiles if tp['total_attempted'] > 0]
    if active_topics:
        sorted_weak = sorted(active_topics, key=lambda x: (x['accuracy_rate'], -x['avg_time_per_question']))
        primary_weak = sorted_weak[0]
    else:
        primary_weak = None

    # 8. Performance Prediction
    if is_new_student:
        prediction = {
            'risk_level': 'PENDING',
            'badge': '⏳ Initial Assessment Needed',
            'message': 'No quiz history yet. Take the Initial Assessment to activate your MindPath model and discover your baseline.'
        }
    elif primary_weak and primary_weak['accuracy_rate'] < 50.0:
        prediction = {
            'risk_level': 'HIGH',
            'badge': '⚠️ Needs Attention',
            'message': f"Conceptual difficulty detected in {primary_weak['topic_name']} ({primary_weak['subject']}). Recommend targeted revision."
        }
    elif primary_weak and primary_weak['accuracy_rate'] <= 70.0:
        prediction = {
            'risk_level': 'MODERATE',
            'badge': '🟡 Developing Stability',
            'message': f"Moderate retention in {primary_weak['topic_name']}. Regular practice will reinforce memory and answering speed."
        }
    else:
        prediction = {
            'risk_level': 'LOW',
            'badge': '🟢 On Track',
            'message': 'Demonstrates solid retention across attempted concepts. Keep challenging yourself with advanced topics.'
        }

    return {
        'user': dict(user),
        'is_new_student': is_new_student,
        'learning_score': learning_score,
        'overall_accuracy': overall_accuracy,
        'speed_score': speed_score,
        'avg_speed_time': avg_speed_time,
        'consistency_score': consistency_score,
        'improvement_rate': improvement_rate,
        'topics': topics_profiles,
        'subjects': list(subjects_summary.values()),
        'primary_weak': primary_weak,
        'prediction': prediction,
        'recent_attempts': attempts[-5:],
        'total_attempts_count': len(attempts)
    }

def get_adaptive_quiz_questions(topic_id, user_id, num_questions=3, difficulty='adaptive'):
    """
    Selects questions dynamically using the student's Digital Twin state or requested difficulty:
    - If difficulty in ('easy', 'medium', 'hard'): prioritizes the chosen difficulty tier.
    - If difficulty is 'adaptive' or unspecified:
        - If user is WEAK: starts with Easy questions (concept reconstruction)
        - If user is AVERAGE / UNASSESSED: balanced Medium & Easy questions
        - If user is STRONG: Hard & Medium questions
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # Check student current mastery for this topic
        cursor.execute('''
        SELECT mastery_status, accuracy_rate 
        FROM student_twin_profile 
        WHERE user_id = ? AND topic_id = ?
        ''', (user_id, topic_id))
        profile = cursor.fetchone()

        mastery = profile['mastery_status'] if profile else 'AVERAGE'

        cursor.execute("SELECT * FROM questions WHERE topic_id = ?", (topic_id,))
        all_questions = [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()

    if not all_questions:
        return []

    easy_q = [q for q in all_questions if q['difficulty'] == 'easy']
    med_q = [q for q in all_questions if q['difficulty'] == 'medium']
    hard_q = [q for q in all_questions if q['difficulty'] == 'hard']

    diff_lower = (difficulty or 'adaptive').strip().lower()
    if diff_lower == 'easy':
        selected = easy_q + med_q + hard_q
    elif diff_lower == 'medium':
        selected = med_q + hard_q + easy_q
    elif diff_lower == 'hard':
        selected = hard_q + med_q + easy_q
    else:
        # Adaptive selection based on digital twin mastery
        if mastery == 'WEAK':
            selected = easy_q + med_q + hard_q
        elif mastery == 'STRONG':
            selected = hard_q + med_q + easy_q
        else:
            selected = med_q + easy_q + hard_q

    return selected[:min(num_questions, len(selected))]

def get_initial_assessment_questions(num_per_topic=1):
    """
    Generates a balanced diagnostic question set selecting 1 question from each available topic.
    Provides a comprehensive baseline across C Programming, Data Structures, and DBMS.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, subject, name FROM topics ORDER BY id")
        topics = [dict(row) for row in cursor.fetchall()]

        assessment_questions = []
        for topic in topics:
            cursor.execute('''
            SELECT * FROM questions 
            WHERE topic_id = ? 
            ORDER BY CASE difficulty WHEN 'medium' THEN 1 WHEN 'easy' THEN 2 ELSE 3 END 
            LIMIT ?
            ''', (topic['id'], num_per_topic))
            questions = [dict(row) for row in cursor.fetchall()]
            for q in questions:
                q['subject'] = topic['subject']
                q['topic_name'] = topic['name']
            assessment_questions.extend(questions)
        return assessment_questions
    finally:
        conn.close()

def update_digital_twin(user_id, topic_id, score, total_questions, time_taken_seconds, answers_detail, quiz_type='adaptive'):
    """
    Transactionally consistent update of the Digital Twin model:
    1. Records quiz_attempt and answers under user_id atomically
    2. Recalculates total_attempted, total_correct, accuracy_rate, avg_time_per_question, and mastery_status
    3. Saves back into student_twin_profile
    Uses BEGIN IMMEDIATE and rollback on error, guaranteeing full consistency.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()

        # 1. Save Attempt
        cursor.execute('''
        INSERT INTO quiz_attempts (user_id, topic_id, quiz_type, score, total_questions, time_taken_seconds)
        VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, topic_id, quiz_type, score, total_questions, time_taken_seconds))
        attempt_id = cursor.lastrowid

        # 2. Save individual answers
        for ans in answers_detail:
            cursor.execute('''
            INSERT INTO quiz_answers (attempt_id, question_id, selected_option, is_correct, time_spent_seconds)
            VALUES (?, ?, ?, ?, ?)
            ''', (attempt_id, ans['question_id'], ans['selected_option'], 1 if ans['is_correct'] else 0, ans.get('time_spent', 10)))

        # 3. Update student_twin_profile for each affected topic
        if topic_id and topic_id > 0:
            topics_to_update = {topic_id: {
                'total_q': total_questions,
                'score': score,
                'time_taken': time_taken_seconds
            }}
        else:
            # Multi-topic assessment: resolve topic_ids in one batch query
            q_ids = [ans['question_id'] for ans in answers_detail]
            q_topic_map = {}
            if q_ids:
                placeholders = ','.join('?' for _ in q_ids)
                cursor.execute(f"SELECT id, topic_id FROM questions WHERE id IN ({placeholders})", q_ids)
                q_topic_map = {row['id']: row['topic_id'] for row in cursor.fetchall()}

            topics_to_update = {}
            for ans in answers_detail:
                t_id = ans.get('topic_id') or q_topic_map.get(ans['question_id'])
                if t_id:
                    if t_id not in topics_to_update:
                        topics_to_update[t_id] = {'total_q': 0, 'score': 0, 'time_taken': 0}
                    topics_to_update[t_id]['total_q'] += 1
                    if ans['is_correct']:
                        topics_to_update[t_id]['score'] += 1
                    topics_to_update[t_id]['time_taken'] += ans.get('time_spent', 10)

        last_update_summary = {}
        for t_id, data in topics_to_update.items():
            cursor.execute('''
            SELECT total_attempted, total_correct, avg_time_per_question 
            FROM student_twin_profile 
            WHERE user_id = ? AND topic_id = ?
            ''', (user_id, t_id))
            row = cursor.fetchone()

            if row:
                curr_attempted = row['total_attempted']
                curr_correct = row['total_correct']
                curr_avg_time = row['avg_time_per_question']

                new_attempted = curr_attempted + data['total_q']
                new_correct = curr_correct + data['score']
                new_avg_time = round(((curr_avg_time * curr_attempted) + data['time_taken']) / max(1, new_attempted), 1)
            else:
                new_attempted = data['total_q']
                new_correct = data['score']
                new_avg_time = round(data['time_taken'] / max(1, data['total_q']), 1)

            new_accuracy = round((new_correct / max(1, new_attempted) * 100), 1)
            new_mastery = calculate_mastery(new_accuracy, new_attempted)

            cursor.execute('''
            INSERT INTO student_twin_profile 
            (user_id, topic_id, total_attempted, total_correct, accuracy_rate, avg_time_per_question, mastery_status, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, topic_id) DO UPDATE SET
                total_attempted = excluded.total_attempted,
                total_correct = excluded.total_correct,
                accuracy_rate = excluded.accuracy_rate,
                avg_time_per_question = excluded.avg_time_per_question,
                mastery_status = excluded.mastery_status,
                last_updated = CURRENT_TIMESTAMP
            ''', (user_id, t_id, new_attempted, new_correct, new_accuracy, new_avg_time, new_mastery))

            last_update_summary = {
                'new_accuracy': new_accuracy,
                'new_mastery': new_mastery,
                'new_avg_time': new_avg_time
            }

        conn.commit()
        return last_update_summary
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
